"""需求解析提示词模板

设计原则(不做死特定活动):
- BASE_TEMPLATE 只定义通用抽取规则, 不绑定任何活动类型
- FRAGMENT_MAP 按活动类型提供可选风格片段, 新增类型只需加一个 key
- 分类结果决定拼装哪个片段; 未识别类型 fallback 纯通用模板
"""

import json
from typing import List, Optional

# 通用基础模板: 只管"抽什么、怎么抽", 与活动类型无关
BASE_TEMPLATE = """你是团建活动策划助手的需求解析器。请从用户输入中抽取结构化信息。

抽取规则:
1. people_count: 参与人数, 整数; 未提及则为 null
2. date: 活动开始日期, 规范化为 YYYY-MM-DD; 相对表述(如"10月底""下周末")按当前时间推断出合理具体日期; 完全无法推断则为 null
   date_end: 活动结束日期, 规范化为 YYYY-MM-DD; 仅当用户明确表达多日活动(如"10月1日到3日""玩两天")时填写; 单日或未提及则为 null
3. budget: 总预算, 换算为元(如"3万" -> 30000); 未提及则为 null
4. origin: 出发地/集合地, 提取地名(如"上海周边" -> "上海"); 未提及则为 null
5. destination: 目的地/活动城市; 与出发地同城或未提及则为 null
6. transport_pref: 优先通行方式, 提取明确的偏好表述(如"坐高铁去" -> "高铁", "自驾" -> "自驾"); 未提及则为 null
7. activity_type: 活动类型, 用简短名词归纳(如 团建/生日会/聚餐/户外拓展/年会); 无法判断则为 null

铁律: 不得编造文中没有的信息; 抽不出的字段一律 null, 不要猜。
"""

# 活动类型风格片段: 分类命中后拼接到基础模板之后
# 扩展方式: 新增活动类型只需在此加一个 key -> 片段, 无需改其他代码
FRAGMENT_MAP = {
    "团建": (
        "本次活动类型为「团建」, 解析与后续规划时注意:\n"
        "- 强调协作性与体能分层: 关注参与者的体能差异, 活动强度要有梯度\n"
        "- 优先考虑参与度高、全员可加入的协作类项目\n"
        "- 预算通常覆盖 交通/场地/餐饮/物料 四大块"
    ),
    "生日会": (
        "本次活动类型为「生日会」, 解析与后续规划时注意:\n"
        "- 强调仪式流程: 时间线编排(开场/庆祝/切蛋糕/合影)是重点\n"
        "- 预算向场景布置与餐饮倾斜\n"
        "- 关注寿星偏好与来宾人数构成"
    ),
}

# 分类用关键词(简单可靠的预分类, 不耗 LLM; LLM 侧 activity_type 仍独立抽取)
# 同义词归一到 FRAGMENT_MAP 的 key
_CLASSIFY_KEYWORDS = {
    "团建": ("团建", "team building", "拓展", "凝聚力"),
    "生日会": ("生日", "寿星", "周岁"),
}


def classify_activity(text: str) -> Optional[str]:
    """关键词预分类: 命中返回活动类型 key, 未识别返回 None(fallback 通用模板)"""
    lowered = (text or "").lower()
    for activity_type, keywords in _CLASSIFY_KEYWORDS.items():
        for kw in keywords:
            if kw in lowered:
                return activity_type
    return None


def build_parse_prompt(
    raw_text: str,
    activity_type_hint: Optional[str] = None,
    history_context: Optional[str] = None,
    today: Optional[str] = None,
) -> str:
    """拼装需求解析提示词: 当前日期 + base 通用模板 + (可选)类型片段 + (可选)团队历史 + 用户原文

    today: 服务器当天日期(YYYY-MM-DD), 由调用方注入。BASE_TEMPLATE 要求把
    "10月底""下周末"这类相对表述**按当前时间**推断成具体日期, 但此前从没告诉过
    模型"当前时间"是什么 —— 模型只能拿自己的训练截止日期猜, 年份/月份都会错。
    日期必须由服务端提供, 不能指望用户输入里带(也不该带)。
    缺省 None 时保持旧行为(不注入), 不改变纯离线调用的语义。

    history_context 来自 Store 的团队历史记录(上次活动画像/复盘标签),
    无历史时为 None, 不影响原流程。
    """
    prompt = (f"当前日期: {today}\n" + BASE_TEMPLATE) if today else BASE_TEMPLATE
    fragment = FRAGMENT_MAP.get(activity_type_hint) if activity_type_hint else None
    if fragment:
        prompt += "\n" + fragment + "\n"
    if history_context:
        prompt += "\n" + history_context + "\n"
    prompt += f"\n用户输入:\n{raw_text}"
    return prompt


def build_history_context_block(profile: Optional[dict], retros: Optional[list]) -> Optional[str]:
    """把 Store 里的团队历史记录拼成"上次你们……"上下文块

    profile: 团队画像记录(人数结构/体能/忌口/偏好分布), 无为 None
    retros:  历次活动记录列表(计划报价与预算的关系/rejected 等), 已按时间倒序
    无任何记录时返回 None(首次活动不注入)。

    【措辞纪律, 三处】
    1. 每条记录都带上自己的**活动日**。旧版一律写"更早一次", 而列表第一条
       其实是最近那次 —— 读的人会以为这团队办过两场, 实际同一场活动在上面
       的画像句里已经说过一遍。
    2. 计划阶段的钱**不能叫"总花费"**。记录写于审批通过之后、活动发生之前,
       一分钱没花, 那个数是四路报价的合计。写成"总花费"就是让下一个团队读到
       "上次花了 3 万" —— 把计划说成了事实。
    3. 预算偏差为 0(正好打平)要照实说, 不能因为真假值判断被静默丢掉:
       "正好打平"和"没有预算信息"是两件事。
    """
    lines: List[str] = []
    if profile:
        parts = []
        if profile.get("people_count"):
            parts.append(f"共{profile['people_count']}人")
        if profile.get("fitness_distribution"):
            parts.append(f"体能分布{json.dumps(profile['fitness_distribution'], ensure_ascii=False)}")
        if profile.get("dietary_all"):
            parts.append(f"忌口{', '.join(profile['dietary_all'])}")
        if profile.get("activity_votes"):
            parts.append(f"偏好投票{json.dumps(profile['activity_votes'], ensure_ascii=False)}")
        if parts:
            when = profile.get("last_activity_date")
            head = f"最近一次活动(活动日{when})" if when else "上次你们"
            lines.append(f"- {head}的团队情况: {'; '.join(parts)}")
    for retro in retros or []:
        parts = []
        # 计划阶段 = 只是排了方案、批了方案, 钱还没花; 说清楚, 免得被当成消费记录
        if retro.get("stage") != "planned" and (retro.get("total_cost") is not None):
            # 旧格式记录(字段还叫 total_cost): 按同义处理, 但措辞一律用"计划报价"
            parts.append(f"计划报价合计{float(retro['total_cost']):.0f}元(活动未发生, 无实际花费记录)")
        elif retro.get("plan_total_cost") is not None:
            parts.append(f"计划报价合计{float(retro['plan_total_cost']):.0f}元(活动未发生, 无实际花费记录)")
        dev = retro.get("plan_budget_deviation")
        if dev is None and "budget_deviation" in retro:
            dev = retro.get("budget_deviation")     # 旧格式
        if dev is not None:
            dev = float(dev)
            if dev > 0:
                parts.append(f"计划超预算{dev:.0f}元")
            elif dev < 0:
                parts.append(f"计划结余{abs(dev):.0f}元")
            else:
                parts.append("计划与预算正好打平")
        if retro.get("rejected_reasons"):
            parts.append(f"被否决/回炉原因: {'; '.join(retro['rejected_reasons'][:3])}")
        if parts:
            when = retro.get("activity_date")
            head = f"活动日{when}的计划" if when else "更早一次(未记录活动日)"
            lines.append(f"- {head}: {'; '.join(parts)}")
    if not lines:
        return None
    return (
        "═══ 团队历史记忆(来自该团队过往活动, 供参考延续习惯) ═══\n"
        + "\n".join(lines)
        + "\n解析本次需求时, 若用户未提及人数/忌口等字段, 可合理参考上次记录(仍以用户原文优先)。"
    )


# ============ 任务清单 ============

CHECKLIST_TEMPLATE = """你是团建活动策划助手的执行落地助手。活动方案已经过人工审批通过, 请把它拆解成可直接执行的任务清单。

最终方案(四路报价, JSON):
{proposals_json}

活动信息:
- 参与人数: {people_count} 人
- 活动日期: {date}
- 总预算: {budget}
- 全员忌口: {dietary_all}
- 用户挑选的候选: {selections}

要求:
1. assignments 分工表: 覆盖 集合签到/交通对接/场地对接/活动执行/餐饮对接/物资采购/摄影记录/应急联络 等必要角色,
   owner 用"待指定"或从方案上下文推断的角色名(不得编造具体人名), detail 写清楚该角色要做的事和时间点
2. purchase_list 采购清单: 从方案推导需要提前采购/预订的物资与服务, 每项带数量和预估费用(est_cost),
   并标明 category = 它出自哪一路提案(transport/venue/flow/food)**之一**,
   费用合计不要超过所属提案的报价(程序会按 category 分组核对, 判不出归属的项过不了这一关)
3. notice_text 通知文案: 可直接发到群里的口吻(称呼+时间地点集合+行程亮点+注意事项(含忌口提醒)+报名回复方式),
   用具体数字(集合时间/地点/时长), 不写"待定"除非方案里确实没有
4. 严格基于上面方案输出, 不得新增方案里没有的活动或收费项目
5. 用户挑选的候选(如有)优先于方案里的默认推荐: 交通/餐饮等按用户选的候选出清单
"""


def build_checklist_prompt(
    proposals: dict, people_count: int, date: Optional[str],
    budget: Optional[float], dietary_all: Optional[list] = None,
    selections: Optional[dict] = None,
) -> str:
    """拼装任务清单生成提示词(审批通过后一次 LLM 调用)"""
    return CHECKLIST_TEMPLATE.format(
        proposals_json=json.dumps(proposals, ensure_ascii=False, indent=2),
        people_count=people_count,
        date=date or "未指定",
        budget=f"{budget:.0f} 元" if budget is not None else "未提供",
        dietary_all=", ".join(dietary_all or []) or "无",
        selections=json.dumps(selections or {}, ensure_ascii=False) or "无",
    )


# ============ 偏好冲突摘要 ============
# 已移除: 偏好提示改由 app/graph/nodes.py::_build_pref_notes 纯规则生成(零 LLM)。
# 原因: 提示里的数字必须是算出来的 —— 交给 LLM 转述时写错人数没人会报错, 而它
# 展示在审批页上长得就像统计结果; 且否决回炉会重跑聚合, 同一输入会得到不同措辞。
# 忌口也从"冲突"里摘出: 它是约束, 不是人群对立。


# ============ 四路提案(transport/venue/flow/food) ============

# 四路通用铁律: 不编造数据源里没有的信息, 报价逐项列出
_PROPOSAL_RULES = """铁律:
1. 只使用输入数据中真实存在的信息, 不得编造场地名/价格
2. 输入中没有的数字(如单价)可按市场常识估算, 但须在 note 里注明"估算"
3. 每个报价项一条 item, total_cost 由系统按 items 求和复核
4. description 用 1-2 句话说清方案思路, 供审批页展示
5. 结构化推荐理由: 每个主推 item 和每个候选 option 必须填 reason 字段(1句话),
   说明为何选/为何排在这个位置(评分/人均/距离/口碑/与团队契合点), 有备选时写明对比差异
"""


def _conflicts_visible_to(conflict_report: dict, category: Optional[str]) -> list:
    """这条冲突该不该给这一路看: 全局的 + 它自己的, 别人的不给

    为什么要筛: 旧行为是把四路各自的冲突**全部**塞给每一路, 于是餐饮路的 prompt 里
    写着"场地容量不足"、场地路的 prompt 里写着"时间线重叠" —— 每路都在读别人的
    作业, 而且容易被带着去改不该它管的东西。

    归属由 reconcile 给出(见 ConflictReport.conflicts_by_category): **没出现在任何类别下的
    即为全局冲突**(典型是预算), 它对每一路都成立, 照给。归属信息缺失时一条不筛,
    与旧行为一致 —— 少给信息只会让模型改错地方。
    """
    conflicts = list(conflict_report.get("conflicts") or [])
    by_cat = conflict_report.get("conflicts_by_category") or {}
    if not category or not by_cat:
        return conflicts
    owned_elsewhere = {t for cat, texts in by_cat.items() if cat != category
                       for t in (texts or [])}
    return [t for t in conflicts if t not in owned_elsewhere]


def build_conflict_feedback(conflict_report: Optional[dict],
                            category: Optional[str] = None) -> str:
    """把上一轮校验报告(带具体数字)拼成修改指令块, 追加到重生成 prompt 之后

    核心原则: 回炉时带具体数字回去改(超支多少/哪两项时间重叠/容量差多少),
    不是笼统的"重新想想"。

    category: 本路自己的类别。给了就只把"与它相关或全局"的冲突放进去(见
    _conflicts_visible_to)。**不传 = 全部照给**, 保持旧行为。
    """
    if not conflict_report:
        return ""
    # conflicts_by_category 必须摘掉: 它是给程序做路由用的, 里面**原样存着每一路的冲突原文**
    # —— 留在 JSON 里等于没筛(别的路的冲突又从这里漏进本路的 prompt)。
    # over_budget/suggestions 则原样保留: 超支数字是全局的, "可降级空间"建议也往往
    # 指向别路 —— 少给这些反而会让本路无从判断该让多少。
    shown = {k: v for k, v in conflict_report.items() if k != "conflicts_by_category"}
    shown["conflicts"] = _conflicts_visible_to(conflict_report, category)
    return (

        "\n⚠️ 上一轮方案未通过约束校验, 以下是程序校验报告(含具体数字), 必须逐条落实:\n\n"
        + json.dumps(shown, ensure_ascii=False, indent=2)
        + "\n\n修改要求:\n"
        "1. 报告涉及你的类别时, 必须给出对应调整(如降级餐标/换场地/压缩项目/错开时段)\n"
        "2. 调整后的报价要让总预算成立; 报告未涉及你的类别时可维持原方案\n"
        "3. 修改思路在 description 中用一句话说明(如\"按校验意见将餐饮降级至XX元\")\n"
    )


def build_user_feedback_block(user_feedback: Optional[list]) -> str:
    """把组织者否决意见(HITL approved=False 的 feedback)拼成修改指令块

    与 build_conflict_feedback 同级: 程序校验意见管"数字不合规",
    本块管"人对方案的不满"(如"不要爬山, 换成室内")。逐条注入, 不得忽略。
    """
    if not user_feedback:
        return ""
    items = "\n".join(f"{i}. {f}" for i, f in enumerate(user_feedback, 1))
    return (
        "\n⚠️ 组织者否决了上一版方案并给出修改意见(按时间先后), 必须逐条落实:\n"
        f"{items}\n"
        "意见涉及你的类别时必须给出对应调整; 未涉及的类别也请自查是否需要配合。\n"
    )


# 交通候选池: (名称, 适用人数下限, 适用人数上限, 报价口径说明)
# 按 人数+距离 动态筛选, 不再写死"大巴/自驾"两个候选
TRANSPORT_CANDIDATES = [
    ("自驾拼车", 1, 20, "按油费+过路费估算, 私家车每车约坐4人"),
    ("打车/网约车", 1, 10, "按趟数×里程估算(短途适用)"),
    ("地铁/公交包组", 1, 25, "同城/近郊适用, 按人均票价×人数估算"),
    ("包中巴(19-25座)", 8, 30, "按天包车估算"),
    ("大巴包车(45-55座)", 15, 999, "按天包车估算, 适合大团"),
    ("高铁/火车", 2, 999, "跨城适用; 有12306真实票价数据时用真实票价, 否则按里程常识估算并注明"),
    ("飞机", 2, 999, "仅800公里以上跨城且预算充裕时考虑"),
]

# 跨城判定: route 距离超过该值(米)且存在出发地/目的地城市对时, 提示纳入高铁/火车候选
INTERCITY_DISTANCE_M = 100_000


def _transport_candidate_lines(people_count: int, route_info: dict,
                               transport_pref: Optional[str],
                               train_data: Optional[list]) -> str:
    """按人数/距离/用户偏好动态拼装交通候选清单

    - 人数窗口过滤: 超出适用区间的方式不进候选(5人团看不到"大巴包车")
    - 用户优先偏好命中的方式置顶标注"(用户优先)"
    - 跨城(route距离>100km / 有火车数据 / 用户明确要高铁飞机 / 距离没采到)才纳入高铁飞机
      注: 距离未知时不再静默丢掉高铁 —— 老逻辑只看 route.distance, 路线采集失败时
      用户明确要求的"优先高铁"会被无声忽略, 候选里只剩大巴。
    """
    distance_m = float(route_info.get("distance") or 0)
    has_route = "duration" in (route_info or {})
    pref_wants_intercity = bool(
        transport_pref and any(k in str(transport_pref) for k in ("高铁", "火车", "动车", "飞机"))
    )
    intercity = (distance_m > INTERCITY_DISTANCE_M or bool(train_data)
                 or not has_route or pref_wants_intercity)

    lines = []
    for name, lo, hi, pricing in TRANSPORT_CANDIDATES:
        if name in ("高铁/火车", "飞机") and not intercity:
            continue
        if name == "高铁/火车" and not train_data and distance_m > 600_000:
            pricing = "超长距离, 票价估算可靠性低, 必须注明'估算, 以12306为准'"
        if name in ("高铁/火车", "飞机") and not has_route:
            pricing += "; 两地距离未采到, 是否适用请按出发地/目的地自行判断并在 note 说明"
        if not (lo <= people_count <= hi):
            continue
        mark = "(用户优先, 必须包含并排在第一)" if transport_pref and transport_pref in name else ""
        lines.append(f"- {name}{mark}: {pricing}")

    if not has_route and not lines:
        lines.append("- 由LLM按常识给出1-2种合理方式并注明'估算'")
    return "\n".join(lines)


def build_transport_prompt(
    route_info: dict, people_count: int, budget: Optional[float],
    origin: Optional[str] = None, destination: Optional[str] = None,
    destination_canon: Optional[dict] = None,
    transport_pref: Optional[str] = None, train_data: Optional[list] = None,
    route_anchor: Optional[str] = None, origin_weather: Optional[list] = None,
    date: Optional[str] = None, date_end: Optional[str] = None,
    conflict_report: Optional[dict] = None, user_feedback: Optional[list] = None,
) -> str:
    """交通提案: 候选集按 人数/距离/偏好 动态生成, 给出 2-3 种方式的多候选方案

    - items = 推荐方式的报价明细(维持 total_cost 守恒校验)
    - options = 全部候选方式的 {名称, 预估费用, 依据}, 供审批页挑选切换
    - origin/destination/transport_pref/train_data/route_anchor 必须由调用方传入:
      缺一个都会让模型凭空判断"同城还是跨城"、忽略用户偏好、无视真实班次。
    - origin_weather 只有交通路能拿到(BasicInfo 里与目的地 weather 分开存): 它回答
      "出发日/返回日赶路会不会受天气影响", 同城时为空(同城无需重复查)。date/date_end
      是给这份预报**定坐标**用的 —— 不说日期, 模型会拿今天的天气去写那天的 note。
    - destination_canon 是归一结果(kind=place 时 display 形如
      "莫干山（浙江省湖州市德清县）"), **只作补充说明**: 同城/跨城判断必须继续
      用 destination 原话与 origin 比 —— 拿归一结果去比会把"上海市"和"上海"
      判成两个城市。城市口径(kind=city)下不显示这行, 因为"杭州 -> 杭州市"这种
      差异对模型没有任何信息量, 添进去只会干扰它读那行"同城/跨城"。
    """
    train_block = (
        "12306真实班次/票价数据(JSON, 优先采用, note注明'12306查询'):\n"
        + json.dumps(train_data, ensure_ascii=False, indent=2)
        if train_data else
        "本次未取到实时火车班次(未配置聚合数据 key, 或活动日超出 15 天预售期, 两者都会返回空)。"
        "如含高铁/火车候选, 按里程常识估算并在 note 注明'估算, 以12306为准'"
    )
    intercity_line = ("与出发地同城" if not destination or destination == origin
                      else f"跨城({origin} -> {destination})")
    canon_line = (f" (归一目的地: {destination_canon.get('display')})"
                  if (destination_canon or {}).get("kind") == "place"
                  and (destination_canon or {}).get("display") else "")
    anchor_line = (f"\n路线基准场地: {route_anchor}(距目的地最近的候选, 不是评分榜首)"
                   if route_anchor else "")
    # 出发地天气只与"去程/返程赶不赶得上"有关。必须显式写清它**不代表活动地点**,
    # 否则模型很容易顺手拿它去建议改活动项目 —— 那是目的地天气的职责, 拿出发地的
    # 雨去改目的地的安排就是误报。
    # 同时必须给出**行程日期**: 高德只报未来 3~4 天, 规划下个月的活动时这份预报
    # 压根覆盖不到出发/返回日, 不说日期模型会拿今天的天气去写那天的 note。
    trip_dates = (f"{date} ~ {date_end}" if date and date_end and date_end != date
                  else (date or "未指定"))
    ow_block = (
        "\n出发地天气预报(JSON): " + json.dumps(origin_weather, ensure_ascii=False)
        + f"\n行程日期: {trip_dates}(出发日 {date or '未指定'} / "
          f"返回日 {date_end or date or '未指定'})"
        + "\n(出发地天气只影响去程/返程: 遇大雨、大雾、大风应在 note 提示预留更多在途"
          " 时间或提前出发; 它**不代表活动地点的天气**, 不要据此建议更改活动项目; "
          "预报没覆盖到出发/返回日时不要臆测那天的天气)"
        if origin_weather else ""
    )
    return f"""你是团建活动策划助手的交通提案器。基于以下数据, 从候选清单中选出最合理的 2-3 种交通方式, 每种给出报价。

候选清单(按人数/距离/用户偏好动态生成, 只能从中选):
{_transport_candidate_lines(people_count, route_info or {}, transport_pref, train_data)}

路线数据(出发地->路线基准场地, driving口径): {json.dumps(route_info, ensure_ascii=False)}{anchor_line}
出发地: {origin or "未提供"} | 目的地: {destination or "未提供"}{canon_line} | 同城/跨城: {intercity_line}{ow_block}
用户优先通行方式: {transport_pref or "未指定"}(指定时该方式必须出现在 items 且排第一)
参与人数: {people_count} 人
总预算: {budget if budget is not None else "未提供(按合理市场价估算)"}

{train_block}

要求:
1. 每种选中的方式各占一条 item(name=方式名, cost=该方式总费用, note=计价依据与人数分摊),
   推荐(最划算/最匹配)的方式放第一条并在 note 标注"推荐"
2. 全部候选方式的概要写入 options(name/est_cost/note), 供组织者在审批页挑选切换;
   options 里的费用与 items 一一对应一致
3. 包车类按"单价×辆数(向上取整 人数/核载)"计算, 写明算式
4. 小团队(如10人以下)不得推荐大巴; 大团队(30人以上)自驾拼车必须给出车数与司机安排压力提示
5. "同城/跨城"只能依据上面的出发地/目的地与路线数据判断, 不要自行假设两地同城;
   跨城时优先给出高铁/火车方案(有 12306 数据则用真实票价)

{_PROPOSAL_RULES}{build_conflict_feedback(conflict_report, "transport")}{build_user_feedback_block(user_feedback)}"""


def build_venue_prompt(
    venues: list, people_count: int, budget: Optional[float],
    destination: Optional[str] = None,
    activity_votes: Optional[dict] = None, activity_type: Optional[str] = None,
    conflict_report: Optional[dict] = None, user_feedback: Optional[list] = None,
) -> str:
    """场地提案: 从高德 POI 候选中选场地并报价(容量按类型常识推断并注明)

    activity_votes 为成员活动偏好投票(票数降序); 候选已按最高票偏好的关键词
    搜索(搜索层保证), prompt 层再要求 LLM 二次把关场地类型与偏好契合,
    契合点写进所选场地的 reason。

    destination 是归一后的目的地(具体地点带行政归属)。候选清单里的每个 POI
    都是**在它周围**搜出来的, 所以"距目的地"是相对这个目的地量的 —— 若不说明
    这一点, 模型会把"距目的地 40km"误读成"离城市中心 40km"(目的地本身就是
    莫干山时, 40km 已经跑到另一个市了)。
    """
    votes_block = (
        json.dumps(activity_votes, ensure_ascii=False)
        if activity_votes else "暂无(按 评分/人均/类型通用契合度 推荐)"
    )
    return f"""你是团建活动策划助手的场地提案器。从以下高德 POI 候选中选出 1 个最合适的场地并给出报价明细。

活动类型: {activity_type or "未指定"}
目的地: {destination or "未提供"}
成员活动偏好投票(key=活动名, value=票数, 按票数降序): {votes_block}
POI 候选(JSON, 含名称/地址/类型/评分/人均/距目的地; 已按评分降序排序且距目的地过远的候选已整体降级, web_note 为博查Web口碑摘要, 仅top3附带): {json.dumps(venues, ensure_ascii=False, indent=2)}
参与人数: {people_count} 人
总预算: {budget if budget is not None else "未提供"}

要求:
1. 优先选 评分高/人均合理/类型契合/距目的地近 的候选, 场地名必须与候选中的名称完全一致
2. 类型必须契合偏好: 有投票时优先选类型与最高票偏好相符的候选(如 爬山→风景区/山地, 剧本杀→剧本杀门店, 露营→露营地), 契合点写入所选场地的 reason; 若候选类型均不契合, 选最接近的并在 reason 说明妥协原因
3. "距目的地"是相对本次活动目的地({destination or "目的地"})的直线距离, 不是距它所在城市中心:
   该值超过 100km 说明候选虽同名同城却已在邻近地市, 一日团建应避开; 超过 30km 的
   也要在 reason 里写明往返通勤代价(车程约值), 让组织者能判断是否值得
4. web_note 里出现 差评/踩雷/乱收费/照骗/卫生 等负面关键词时, 必须在所选场地的 reason 中提示该风险; 有严重差评的候选应降级或避开
5. 报价 items 至少含 场地费(可用候选的"人均"×人数), 其余按需(如 物料/器材租赁);
   候选"人均"为空且类型是公园/广场等免费开放场地时, 场地费写 0 元并在 note 说明依据
6. 容量信息候选里没有, 可按场地类型常识推断, 但必须在 note 注明"推断"并写明"容纳N人"
7. 若数据不足以做出可靠推荐, 在 description 中说明原因

{_PROPOSAL_RULES}{build_conflict_feedback(conflict_report, "venue")}{build_user_feedback_block(user_feedback)}"""


def build_flow_prompt(
    activity_votes: dict, fitness_distribution: dict,
    activity_type: Optional[str], date: Optional[str],
    date_end: Optional[str] = None,
    destination: Optional[str] = None,
    activity_pois: Optional[list] = None,
    conflict_report: Optional[dict] = None, user_feedback: Optional[list] = None,
) -> str:
    """流程提案: 偏好全覆盖 + 每个活动绑定真实POI地点 + 多日按天分组时间线

    activity_pois 为按偏好活动二次搜索的真实 POI 候选(未搜到为空,
    此时活动地点标注"待定", 不得编造地名)。

    destination 是本次活动目的地: 活动地点候选都是在它附近搜出来的, 写明它
    才能让模型把"上午爬山下午剧本杀"排在同一片区域里, 而不是东一个西一个。
    """
    poi_data = (
        json.dumps(activity_pois, ensure_ascii=False, indent=2)
        if activity_pois else "暂无(活动地点标注'待定', 不得编造具体地名)"
    )
    if date and date_end and date_end > date:
        days_block = f"{date} 至 {date_end}(多日活动, 按天分组编排)"
    else:
        days_block = date or "未指定(单日)"
    return f"""你是团建活动策划助手的流程提案器。结合成员偏好与体能分布, 排出活动时间线。

目的地: {destination or "未提供"}
活动类型: {activity_type or "未指定"}
活动日期: {days_block}
活动偏好投票: {json.dumps(activity_votes, ensure_ascii=False)}(key=活动名, value=想玩该活动的人数)
体能分布: {json.dumps(fitness_distribution, ensure_ascii=False)}(key=体能等级1-5, value=人数)

活动地点候选(真实POI, 已按偏好活动在目的地附近搜索, 距目的地过远的已降级):
{poi_data}

要求:
1. 偏好全覆盖: 投票里每一个活动都必须出现在时间线中, 不得只安排部分; 同一天内按 票数高->低 排时段;
   活动确实排不下时, 把取舍原因写进 description(如"桌游票数最低且时间不足, 建议晚上自由活动自选")
2. 每个活动 item 必须带 location 字段: {{"name": 地点全名, "location": "经度,纬度", "address": 地址}},
   只能从上面活动地点候选中选, 不得编造; 候选没有合适的则 location 填 null 并在 note 标注"地点待定"
3. 时间线 items 按 "时间段 -> 费用" 编排, 每项 name 里写明起止时刻(如 "D1 09:00-12:00 爬山",
   单日可省略D前缀); 同一天内时段不得重叠; 低强度项目放在体能弱者也能参与的时段;
   同一天的地点要就近编排(相邻活动的地址/坐标尽量在同一片区), 最远的点别排在
   首个或末个时段 —— 否则一天有两三个小时耗在路上
4. 费用可为 0(如自由活动/破冰游戏), 但须真实对应时间段

{_PROPOSAL_RULES}{build_conflict_feedback(conflict_report, "flow")}{build_user_feedback_block(user_feedback)}"""


def build_food_prompt(
    venue: Optional[dict], dietary_all: list,
    people_count: int, budget: Optional[float],
    nearby_restaurants: Optional[list] = None,
    guides: Optional[list] = None,
    date_end: Optional[str] = None,
    destination: Optional[str] = None,
    conflict_report: Optional[dict] = None, user_feedback: Optional[list] = None,
) -> str:
    """餐饮提案: 结合忌口约束与场地周边餐厅数据, 逐顿推荐且每顿带候选餐厅供挑选

    nearby_restaurants 为 None 时允许先调用受控搜索工具查场地附近餐厅。
    guides 为博查 Web 搜索的口碑/攻略结果(未配置 key 时为空)。
    date_end 非空表示多日活动(每天 早餐+午餐+晚餐; 单日 午餐+晚餐)。
    destination 只在**场地未定时**才写进 prompt: 餐饮是四路里唯一不直接挑地名的
    一路, 场地没定它就完全不知道在哪个城市吃饭(旧 prompt 只有"活动场地: 未定");
    场地已定时它会给出场地名/地址/坐标, 再加一行目的地只会分散注意力。
    """
    restaurant_data = (
        json.dumps(nearby_restaurants, ensure_ascii=False, indent=2)
        if nearby_restaurants else "暂无(可先调用 search_nearby_food 工具搜索场地附近餐厅, 或按常识估算)"
    )
    guide_data = (
        json.dumps(guides, ensure_ascii=False, indent=2)
        if guides else "暂无(未配置博查搜索, 忽略攻略维度)"
    )
    dest_line = (f"\n活动目的地: {destination}(场地未定, 按此地的餐饮情况估算)"
                 if destination and not venue else "")
    return f"""你是团建活动策划助手的餐饮提案器。给出活动期间每一顿的餐饮方案(逐顿推荐, 每顿都有候选餐厅)。

活动场地: {json.dumps(venue, ensure_ascii=False) if venue else "未定"}{dest_line}
全员忌口/饮食标签: {json.dumps(dietary_all, ensure_ascii=False)}(方案必须避开这些忌口)
参与人数: {people_count} 人
总预算: {budget if budget is not None else "未提供"}
活动天数: {"单日(午餐+晚餐)" if not date_end else f"多日(每天早餐+午餐+晚餐)"}

场地附近餐厅数据: {restaurant_data}

口碑/攻略搜索结果(来自Web搜索, 供参考特色与口碑): {guide_data}

要求:
1. 逐顿推荐: 每一顿正餐单独写入 meals 数组, 每组 {{day, meal_type, options}}:
   - 单日: day 用 "当天", 覆盖 午餐+晚餐
   - 多日: day 用 "D1"/"D2"…, 每天 覆盖 早餐+午餐+晚餐
   - 每组 options 给 2-3 家候选餐厅(name/est_cost=按该店这顿用餐的总费用/note=人均/距场地距离/特色/reason=推荐理由与候选间对比, 餐厅数据已按评分降序, 优先从头部选)
2. items 汇总全部餐费明细(按 天次+餐次+餐厅 逐行列出), total_cost 为全程餐饮合计
3. 忌口必须落实: 有清真忌口则建议清真餐厅或备注协调方案
4. 优先使用上面餐厅数据里真实存在的餐厅名, 不得编造; 攻略结果只作口碑参考, 不得虚构其中的餐厅

{_PROPOSAL_RULES}{build_conflict_feedback(conflict_report, "food")}{build_user_feedback_block(user_feedback)}"""


# ============ 风险备选生成(risk 节点, 仅规则层标记风险时调用一次) ============

RISK_TEMPLATE = """你是团建活动策划助手的风险评估师。规则层已检出以下风险(逐条带 id):

{flags_json}

当前方案上下文(生成备选时参考, 不得编造其中没有的场地/数据):
- 活动类型: {activity_type}
- 活动日期: {date}
- 天气预报: {weather_json}
- 流程安排: {flow_json}
- 体能分布: {fitness_json}(key=体能等级1-5, value=人数)
- 参与人数: {people_count}

要求:
1. 每条风险都必须给出至少一个备选方案; covers 填它所覆盖的风险 **id**(照抄上面给出的
   id 字符串, 一个字都不要改 —— 填错或自造 id 的备选会被丢弃, 那条风险就变成"暂无备选")
2. 一条备选同时解决多条风险时, 把这些 id 都填进 covers
3. 备选落到具体形式(如"雨天改室内剧本杀场地: XX店, 人均XX元×30人"), 不写"注意防雨"式空话
4. est_cost 为备选方案预估总费用(元), 与原方案等价或更省优先; 无额外费用填 0。
   这个数会被程序标注成"模型估算、未计入合计", 不要试图把它写成报价明细或已发生费用
5. 涉及场地/餐厅的备选优先使用上下文里真实存在的名称; 上下文没有的按市场常识给出形式与估算价, 并注明"估算"
"""


def build_risk_prompt(
    flags: list, activity_type: Optional[str], date: Optional[str],
    weather: list, flow_items: list, fitness_distribution: dict, people_count: int,
) -> str:
    """拼装风险备选生成提示词: 规则层标记(带 id) + 方案上下文

    flags 是 [{{id, severity, detail, evidence}}]; 备选按 id 关联标记, 不回抄原文。
    """
    return RISK_TEMPLATE.format(
        flags_json=json.dumps(flags, ensure_ascii=False, indent=2),
        activity_type=activity_type or "未指定",
        date=date or "未指定",
        weather_json=json.dumps(weather, ensure_ascii=False),
        flow_json=json.dumps(flow_items, ensure_ascii=False),
        fitness_json=json.dumps(fitness_distribution, ensure_ascii=False),
        people_count=people_count,
    )
