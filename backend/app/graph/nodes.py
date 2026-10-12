"""图节点定义(多人团建活动策划)

当前节点: 需求解析(parse_request) -> 偏好聚合(aggregate_prefs)
        -> 基础信息采集(gather_info) -> 四路提案(Send 并行派发:
           propose_transport / propose_venue / propose_flow / propose_food)
        -> 约束消解(reconcile, 纯程序零LLM)
           未通过且未超回炉上限 -> 条件边重新 Send(ConflictReport 注入重生成 prompt;
             冲突带归属, 只重派相关的那几路, 全局冲突/无归属信息时才四路全发)
           通过或 replan_count > MAX_REPLAN -> 风险评估(risk)
        -> 风险评估(risk): 规则层纯Python(雨天+室外项目/强度vs体能);
           有标记才调一次 LLM 生成备选方案(带报价), 无标记零LLM直接 low
        -> 人工审批(approval, interrupt HITL): 暂停图执行等待 /api/event/resume
           approved=True  -> checklist -> memory -> END
           approved=False -> 条件边路由回 aggregate_prefs 重走(意见注入四路 prompt)
        -> checklist: 一次 LLM 把最终方案拆成任务清单(分工/采购/通知文案)
        -> memory: 团队记忆写入 Store(namespace=("team", team_id), 团队画像+复盘标签)

Store 注入: 节点签名声明 store: Optional[BaseStore] 参数, compile(store=...) 时
LangGraph 自动注入; 未传 Store 时节点优雅降级(parse 不注入历史/memory 跳过写入)。
"""

import asyncio
import functools
import json
import re
import time
import uuid
from collections import Counter
from datetime import date as _date_type, datetime
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import ToolMessage
from langgraph.store.base import BaseStore
from langgraph.types import Send, interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..models.schemas import (
    BasicInfo, ConflictReport, EventRequest, Location, PersonPref, POIInfo,
    PrefSummary, Proposal, RiskAlternative, RiskAlternatives, RiskFlag, RiskReport,
    RuleStatus, TaskChecklist, VenueInfo,
)
from ..prompts.templates import (
    build_checklist_prompt, build_flow_prompt, build_food_prompt,
    build_history_context_block, build_parse_prompt, build_risk_prompt,
    build_transport_prompt, build_venue_prompt, build_user_feedback_block,
    classify_activity,
)
from . import amap_client, bocha_client, train_client
from .errors import (AppError, CallResult, CallStatus, CapabilityDisabled,
                     ConfigError, call_external)
from .lexicon import (
    EXTREME_WEATHER_KEYWORDS, PRECIP_KEYWORDS, fold_activities, fold_dietary,
    resolve_project,
)
from .model import (get_chat_model, invoke_llm, invoke_structured,
                    schema_fix_instruction)
from .state import EventPlanState

# 需要强制补全的关键字段(缺失时路由层返回 400)
# 出发地也算关键: 没有它就定不了城际交通/天气/场地检索的城市, 方案只能瞎编
KEY_FIELDS = ("people_count", "date", "origin")

# 活动类型 -> 场地搜索关键词(未识别 fallback 景点)
VENUE_KEYWORDS = {
    "团建": "团建场地",
    "户外拓展": "拓展基地",
    "生日会": "派对场地",
    "聚餐": "餐厅",
    "剧本杀": "剧本杀",
}

# 活动偏好 -> POI 搜索关键词(活动地点化: 按偏好活动在目的地二次搜真实地点)
ACTIVITY_KEYWORDS = {
    "爬山": "风景区", "登山": "风景区", "徒步": "风景区", "古镇": "古镇",
    "聚餐": "餐厅", "烧烤": "烧烤", "露营": "露营地",
    "KTV": "KTV", "剧本杀": "剧本杀", "桌游": "桌游馆",
    "温泉": "温泉", "飞盘": "体育公园", "骑行": "自行车骑行",
    "密室": "密室逃脱", "漂流": "漂流", "滑雪": "滑雪场",
}

# flow 路最多按几个偏好活动搜地点(高票优先, 防外网调用过多)
MAX_FLOW_POI_SEARCH = 4
# 每个活动保留的地点候选数
POIS_PER_ACTIVITY = 4

# 候选截断上限(防 context 爆炸)
MAX_VENUES = 10
# 火车班次最终保留数(排序后截断)
MAX_TRAINS = 6
# 博查口碑核对只查排序后 top3 场地(成本护栏: 3次×约0.036元/次)
BOCHA_WEB_NOTE_TOP = 3

# ============ 四路提案配置 ============

MIN_VENUE_CANDIDATES = 2    # 场地候选下限: 少于该数程序化报"候选不足"(零LLM, 不编造)
MAX_FOOD_TOOL_ROUNDS = 3    # food 路受控搜索工具最大轮数(防 LLM 循环失控)
# food 路**整条路**的总时间预算(秒): 它是四路里唯一要跑多轮工具循环的, 单次
# 上限(LLM_TIMEOUT_SECONDS)只管一次请求 —— 三轮各超时一次照样能把这一路拖到
# 分钟级, 而四路汇合要等最慢的一路。参考另外三路的 invoke_structured 总预算
# 45s: 三轮共享 120s, 一轮正常几秒, 预算内足够跑完; 用尽则转程序化兜底。
FOOD_TOTAL_BUDGET = 120.0
PROPOSAL_ORDER = ("transport", "venue", "flow", "food")

# ============ 约束消解配置 ============

MAX_REPLAN = 2              # 最多回炉 2 次; replan_count > 2 不再回炉, 带冲突交人工裁决(HITL)

# Proposal.category -> 中文名(校验报告用, 人话不写英文 key)
CATEGORY_NAMES = {"transport": "交通", "venue": "场地", "flow": "流程", "food": "餐饮"}

# 交通匹配阈值: 人数低于该值推大巴 -> 程序化打回(与 prompt 候选清单下限一致)
BUS_MIN_PEOPLE = 10

# 场地"通勤圈"半径(km): 超过它的候选在排序里整体降到圈内候选之后(降级不淘汰)。
# 这条规矩以前只写在 venue prompt 里("距城市中心超过100km的候选应降级或避开"),
# 没有任何程序约束, 也没有任何断言 —— 实测"杭州团建"的评分榜首是 138km 外的
# 千岛湖植物园, 它照样排第一个进候选清单, 全靠模型自觉避开。
VENUE_NEAR_KM = 100.0

# 出发地解析告警距离: 超过该值(km)时把"出发地可能写错"写进 steps_log
ORIGIN_WARN_KM = 300.0

# 必须拿到真实数据的采集维度: 这几路"接口正常但没有结果"时, 方案只能靠 LLM 常识
# 硬凑(场地全空 -> 地点待定; 出发地坐标空 -> 交通只能估算), 必须写进日志告知。
# 天气/火车/攻略为空是可以接受的正常结果(超出预报期/同城/未配 key), 不在此列。
CRITICAL_DIMENSIONS = ("venues", "geocode", "direction")


def _missing_of(parsed: EventRequest) -> List[str]:
    """程序化计算缺失字段(不可靠信息不交给 LLM 自报)"""
    return [f for f in ("people_count", "date", "budget", "origin")
            if getattr(parsed, f) is None]


def _invalid_date_note(parsed: EventRequest) -> Optional[str]:
    """活动日期早于今天 -> 返回提示语, 否则 None

    过去日期不是"缺失", 但处置一样: 必须退回重填。不能放行 —— 放行会让天气/
    火车票去查一个已经过去的日期, 并给出一份"为上周那场团建"的方案。
    """
    if not parsed.date:
        return None
    try:
        start = _date_type.fromisoformat(parsed.date)
    except ValueError:      # 模型层已保证 ISO, 这里只是不同形态兜底
        return None
    if start < _date_type.today():
        return f"活动日期 {parsed.date} 早于今天({_date_type.today().isoformat()}), 请填未来日期"
    return None


# 单次读回来的历史记录上限。**不靠 Store 的返回顺序取"最近 N 条"**: limit 是
# "最多给这么多", 给出来的是哪几条取决于存储的排序, 不是最新几条。所以读宽一点、
# 自己按时间排完再截断。再往上(几百场活动)就是分页的活了, 到那时再说。
_HISTORY_FETCH_LIMIT = 200
_HISTORY_RETRO_CAP = 5


def _retro_sort_key(record: dict) -> str:
    """历史记录的排序键: 优先活动日, 缺了退回记录时间(两者都是可比较的字符串)"""
    return str(record.get("activity_date") or record.get("recorded_at") or "")


def _dedupe_retros(retros: List[dict]) -> List[dict]:
    """按活动日去重, 同一活动日只留最新写入的那条

    旧写法按"到秒的时间戳"命名, 同一场活动重跑两次就是两条无法分辨的记录
    (记录里唯一的身份就是活动日, 两条只能靠写入时刻分先后)。改成业务键之后
    不再产生重复, 但库里已经躺着的那些读出来仍要合掉 —— 否则团队历史会把
    同一场活动说两遍, 而且都标着"更早一次"。
    """
    newest: dict = {}
    undated: List[dict] = []
    for r in retros:
        key = str(r.get("activity_date") or "")
        if not key:
            # 说不清是哪次活动的记录: 不参与去重, 但**也不丢** —— 静默丢记录
            # 正是这类"档案"功能最不该有的行为(渲染时标成"未记录活动日")
            undated.append(r)
            continue
        prev = newest.get(key)
        if prev is None or str(prev.get("recorded_at") or "") <= str(r.get("recorded_at") or ""):
            newest[key] = r
    kept = list(newest.values()) + undated
    kept.sort(key=_retro_sort_key, reverse=True)
    return kept


async def _load_team_history(store: Optional[BaseStore],
                             team_id: str) -> Optional[dict]:
    """读该 team_id 的团队历史(无 Store/无记录返回 None)

    返回 {"profile": dict|None, "retros": [...]}, 渲染成提示词文本的事情
    交给 build_history_context_block。**两条解析路径都会调用**: 结构化路径
    没有 LLM 提示词可注入, 但它同样需要这份记录(审批页要给人看"上次你们…")。
    之前只在自由文本那条路上读, 而产品实际走的全是结构化路径 —— 于是团队记忆
    写了一个多月, 读的那一半一次都没执行过。
    """
    if store is None:
        return None
    try:
        profile_item = await store.aget(("team", team_id), "profile")
        profile = profile_item.value if profile_item is not None else None
        retro_items = await store.asearch(("team", team_id), limit=_HISTORY_FETCH_LIMIT)
        retros = [i.value for i in retro_items if str(i.key).startswith("retro")]
        retros = _dedupe_retros(retros)[:_HISTORY_RETRO_CAP]
        if profile is None and not retros:
            return None
        print(f"[parse_request] 读到团队历史(team_id={team_id}): "
              f"画像={'有' if profile else '无'}, 历史记录{len(retros)}条")
        return {"profile": profile, "retros": retros}
    except Exception as e:
        # 读不到历史不是错: 它只影响"参考上次习惯", 不该让本次策划失败。
        # 但必须留日志 —— 静默的读取失败正是"写了却从没读到"能藏这么久的原因。
        print(f"[parse_request] 团队历史读取失败(不阻塞主流程): {str(e)[:120]}")
        return None


async def _load_history_block(store: Optional[BaseStore], team_id: str) -> Optional[str]:
    """读团队历史并渲染成"上次你们……"提示词块(无记录返回 None)"""
    history = await _load_team_history(store, team_id)
    if not history:
        return None
    return build_history_context_block(history.get("profile"), history.get("retros"))


async def parse_request(state: EventPlanState, store: Optional[BaseStore] = None) -> Dict[str, Any]:
    """需求解析节点: 把输入规范化成结构化 EventRequest(能不用 LLM 就不用)

    两条路, 按输入形态分流:
    - **结构化输入**(raw_request["structured"], 表单/API 直传) -> **零 LLM**,
      直接校验组装。表单本来就握着精确字段, 拼成一句话再让 LLM 拆回来纯亏,
      而且会错: "人均预算1000元(总30000元)"这种句子, 抽成人均还是总没有约束,
      一旦抽成人均, 下游 reconcile 会每次都判超支。
    - **自由文本**(仅 /parse) -> 保留 LLM 抽取, 但走 invoke_structured 硬化层
      (缺 tool_calls/参数不合 schema 会被喂回字段级修正指令重填, 而不是
      IndexError / 裸 ValidationError 变成 500), 并把**服务器当前日期**注入
      prompt(BASE_TEMPLATE 要求按当前时间推断相对日期, 此前从没给过当前时间)。

    missing_fields 一律由程序计算, 不信任 LLM 自报。
    """
    raw = state.get("raw_request") or {}
    raw_text = str(raw.get("raw_text") or "").strip()
    # team_id 兜底: 路由层(_resolve_team_id)已保证非空, 这里再兜一层防止
    # 直调图/单测绕过路由时落进 ("team", "default") 公共桶与他人记忆串号
    team_id = str(raw.get("team_id") or "").strip() or f"anon_{uuid.uuid4().hex[:12]}"

    # 团队历史: **两条路径都读**。它过去只在自由文本分支里读, 而产品实际走的
    # 是结构化分支 —— 记忆写了却从没被读过。读一次, 两条路各自消费:
    # 文本路注入解析提示词(缺字段可参考上次), 结构化路没有提示词可注入, 就带进
    # 状态给审批页展示。读失败只记日志, 不影响本次策划(见 _load_team_history)。
    history = await _load_team_history(store, team_id)

    # ---- 路径 A: 结构化输入 -> 程序化组装, 不调 LLM ----
    structured = raw.get("structured")
    if isinstance(structured, dict) and structured:
        # 只采信 EventRequest 认识的字段(多传的忽略而不是崩); 排除 raw_text,
        # 它是显式参数, 重复传会变成 "multiple values for keyword argument"
        allowed = set(EventRequest.model_fields) - {"raw_text"}
        try:
            parsed = EventRequest(
                raw_text=raw_text,
                **{k: v for k, v in structured.items() if k in allowed},
            )
        except ValidationError as exc:
            print(f"[parse_request] 结构化输入不合法: {str(exc)[:160]}")
            return {
                "parsed_request": {},
                "activity_type": None,
                "missing_fields": ["raw_text"],
                "team_id": team_id,
                "steps_log": [f"[parse_request] 结构化输入不合法: {str(exc)[:160]}"],
            }
        # 活动类型缺省时用关键词兜底(仍不耗 LLM); 没有原文就只能是 None
        if not parsed.activity_type:
            parsed.activity_type = classify_activity(raw_text)
        parsed.missing_fields = _missing_of(parsed)
        note = _invalid_date_note(parsed)
        if note:
            print(f"[parse_request] {note}")
            parsed.missing_fields.append("date")
        print(
            f"[parse_request] 结构化输入(零 LLM): 类型={parsed.activity_type}, "
            f"人数={parsed.people_count}, 日期={parsed.date}, 预算={parsed.budget}, "
            f"出发地={parsed.origin}, 缺失={parsed.missing_fields or '无'}"
        )
        return {
            "parsed_request": parsed.model_dump(),
            "activity_type": parsed.activity_type,
            "missing_fields": parsed.missing_fields,
            "team_id": team_id,
            "team_history": history,
            "steps_log": ["[parse_request] 结构化输入直接组装(零 LLM), "
                          f"缺失={parsed.missing_fields or '无'}"
                          + ("(已读团队历史)" if history else "")],
        }

    # ---- 路径 B: 自由文本 -> LLM 抽取 ----
    if not raw_text:
        # 路由层已挡空输入, 这里兜底防御
        return {
            "parsed_request": {},
            "activity_type": None,
            "missing_fields": ["raw_text"],
            "team_id": team_id,
            "steps_log": ["[parse_request] raw_text 为空, 跳过 LLM 解析"],
        }

    # 0) 团队历史上下文: 文本路把它注进提示词(用户没提的字段可参考上次习惯)。
    #    注意**不**拿历史去填本次没给的必填字段(预算/日期): 那是凭空发明需求,
    #    和"参考习惯"是两件事。
    history_block = build_history_context_block((history or {}).get("profile"),
                                                (history or {}).get("retros"))

    # 1) 关键词预分类 -> 拼装提示词(未识别类型 fallback 通用模板; 注入当前日期)
    hint = classify_activity(raw_text)
    prompt = build_parse_prompt(raw_text, hint, history_context=history_block,
                                today=_date_type.today().isoformat())
    print(f"[parse_request] 类型提示: {hint or '未识别(用通用模板)'} (文本路径走 LLM)")

    # 2) 一次 LLM 调用; invoke_structured 是同步的 -> to_thread 避免阻塞事件循环
    parsed = await asyncio.to_thread(invoke_structured, EventRequest, prompt)
    parsed.raw_text = raw_text  # 原文回填, 防止 LLM 漏带/改写
    # 日期区间防御: date_end 早于 date 视为无效丢弃(两者已归一为 ISO 字符串)
    if parsed.date_end and parsed.date and parsed.date_end < parsed.date:
        print(f"[parse_request] date_end({parsed.date_end}) 早于 date({parsed.date}), 丢弃")
        parsed.date_end = None

    # 3) 程序化计算缺失字段 + 拦截过去日期
    parsed.missing_fields = _missing_of(parsed)
    note = _invalid_date_note(parsed)
    if note:
        print(f"[parse_request] {note}")
        parsed.missing_fields.append("date")

    print(
        f"[parse_request] 解析完成: 类型={parsed.activity_type}, "
        f"人数={parsed.people_count}, 日期={parsed.date}, "
        f"预算={parsed.budget}, 出发地={parsed.origin}, 缺失={parsed.missing_fields or '无'}"
    )
    return {
        "parsed_request": parsed.model_dump(),
        "activity_type": parsed.activity_type,
        "missing_fields": parsed.missing_fields,
        "team_id": team_id,
        "team_history": history,
        "steps_log": [
            f"[parse_request] 解析完成: 类型={parsed.activity_type}, "
            f"缺失={parsed.missing_fields or '无'}"
            + (", 已注入团队历史" if history_block else ""),
        ],
    }


def _normalize_prefs(raw_prefs: List) -> List[PersonPref]:
    """容错归一: 兼容 dict 与 PersonPref 两种形态(前端提交经 JSON 传输为 dict)"""
    return [p if isinstance(p, PersonPref) else PersonPref(**p) for p in raw_prefs]


# 偏好提示的判定阈值(规则生成用, 与 _detect_intensity_conflict 的阈值各司其职:
# 那边比的是"流程强度 vs 体能", 这边比的是"人群自身是否分裂")
ACTIVITY_CONFLICT_RATIO = 0.5    # 次高票 >= 最高票的 50% 才算"分裂", 否则只是"主打"
FITNESS_TIER_RATIO = 0.25        # 弱/强体能各占已表态者 25% 才算"分层落差"


def _build_pref_notes(
    activity_votes: Dict[str, int],
    fitness_distribution: Dict[str, int],
    dietary_counts: Dict[str, int],
    stated_people: int,
) -> List[str]:
    """偏好提示(纯规则, 零 LLM): 从统计好的桶 + 人数直接拼句子

    替代原先"把统计结果交给 LLM 写自然语言"的做法, 理由:
    1. 一句话里的数字必须是**算出来的**。LLM 转述时写错人数没有任何地方会报错
       (schema 只校验"是个字符串列表"), 而它展示在审批页上长得就像统计结果。
    2. 同一 state 重跑必须得到同一句话 —— 否决回炉会重走本节点, 旧写法会让
       第 1 轮和第 2 轮审批页的"分歧点"不一致, 而输入根本没变。

    代价是丢失 LLM 的措辞能力, 换来的是确定性与可信度; 判定规则显式写在这里,
    宁可不报(阈值卡住)也不要报噪音 —— 噪音因为确定性反而更难被发现。
    """
    notes: List[str] = []

    # ---- 活动分歧: 只在最高两项"装得下同一批人"的前提下才算分歧 ----
    # 判据 top1 + top2 <= 已表态人数 => 这两拨人基本是不重合的两群人, 才有对立可言。
    # 反例1: 全员汇总行里 30 人都勾了爬山和剧本杀 -> 30+30 > 30 -> 那是"都想安排"不是分歧;
    # 反例2: 所有人两样都勾 -> 同样被挡住。
    # 只看总票数(含第三名及以后)会把 C4 这种 5人爬山/5人剧本杀 的真分裂误判成"多选"
    # 而漏报, 所以只看最高两项。
    if len(activity_votes) >= 2:
        items = list(activity_votes.items())
        (act1, top1), (act2, top2) = items[0], items[1]
        if top2 >= ACTIVITY_CONFLICT_RATIO * top1 and top1 + top2 <= stated_people:
            notes.append(f"{top1}人想{act1} vs {top2}人想{act2}，建议分两组或都安排")

    # ---- 体能分层: 弱/强两端都有相当比例 -> 同一套强度安排不了 ----
    dist = {int(k): v for k, v in (fitness_distribution or {}).items() if str(k).isdigit()}
    total = sum(dist.values())
    if total:
        weak = sum(v for lvl, v in dist.items() if lvl <= 2)
        strong = sum(v for lvl, v in dist.items() if lvl >= 4)
        if weak / total >= FITNESS_TIER_RATIO and strong / total >= FITNESS_TIER_RATIO:
            notes.append(f"体能分层：1-2级 {weak}人 vs 4-5级 {strong}人，建议按强度分组安排"
                         f"（占已表态 {total} 人）")

    # ---- 忌口: 是**约束**不是分歧, 措辞自带说明, 免得被当成人群对立 ----
    if dietary_counts:
        shown = list(dietary_counts.items())[:3]
        detail = "、".join(f"{n}人{c}" for c, n in shown)
        if len(dietary_counts) > len(shown):
            detail += " 等"
        notes.append(f"忌口约束：{detail}（餐饮方案需避开）")

    return notes


def aggregate_prefs(state: EventPlanState) -> Dict[str, Any]:
    """多人偏好聚合节点(和原单人旅行最大的结构差异)

    **全程零 LLM、零外网, 是纯函数**: 人数/活动投票/体能分布/忌口计数/偏好提示
    全部由确定性 Python 产出。同一 state 多次执行逐字一致 —— 否决回炉重跑时
    审批页上的提示不会变。

    计数口径由 PersonPref.headcount **声明**, 不再靠 len(prefs)==1 猜:
    旧实现按"行数"计票, 表单只提交 1 条"全员汇总"时会按 people_count 放大,
    可一旦是"30 人只代填了 1 条成员偏好", 同一个逻辑就把 1 个人的票放大成 30 人;
    反之"1 人团建 + 1 条偏好"与"30 人 + 1 条汇总行"在旧逻辑里无法区分。
    现在放大与否由调用方声明, 统计一律 += headcount。

    活动/忌口先做同义归一(lexicon.py): "爬山/登山/徒步"若各自成桶, 会写出
    "10人想爬山 vs 8人想登山"这种本质是同一件事的假冲突。
    """
    prefs = _normalize_prefs(state.get("preferences") or [])

    if not prefs:
        return {
            "pref_summary": PrefSummary(),
            "steps_log": ["[aggregate_prefs] 无偏好数据, 跳过聚合"],
        }

    # ---- 纯 Python 统计(确定性, 零成本, 零 LLM) ----
    # 人数以需求解析为准(表单一次提交模式只录入 1 条全员汇总偏好, len(prefs) 会错成 1);
    # 解析没给出人数时回退"声明人数之和" —— headcount 已声明, 这个兜底是站得住的
    parsed = state.get("parsed_request") or {}
    stated_people = sum(p.headcount for p in prefs)
    people_count = parsed.get("people_count") or stated_people

    # 忌口计数: 归一后每个标签的 headcount 之和
    dietary_counter: Counter = Counter()
    for p in prefs:
        for diet in fold_dietary(p.dietary):
            dietary_counter[diet] += p.headcount
    dietary_counts = dict(dietary_counter.most_common())
    dietary_all = sorted(dietary_counts)        # 由 counts 派生, 避免两个字段漂移

    # 体能分布: 只统计**填写了**体能的人。默认值 3 曾让未表态者静默进分布,
    # 稀释 _detect_intensity_conflict 的弱体能占比(阈值 25%), 把真冲突掩盖成假阴性。
    fitness_counter: Counter = Counter()
    for p in prefs:
        if p.fitness_level is not None:
            fitness_counter[p.fitness_level] += p.headcount
    fitness_distribution = {str(lvl): fitness_counter[lvl] for lvl in sorted(fitness_counter)}

    votes: Counter = Counter()
    for p in prefs:
        for act in fold_activities(p.activity_prefs):
            votes[act] += p.headcount
    activity_votes = dict(votes.most_common())                           # 按票数降序

    conflict_points = _build_pref_notes(
        activity_votes=activity_votes,
        fitness_distribution=fitness_distribution,
        dietary_counts=dietary_counts,
        stated_people=stated_people,
    )

    summary = PrefSummary(
        people_count=people_count,
        stated_people=stated_people,
        dietary_all=dietary_all,
        dietary_counts=dietary_counts,
        fitness_distribution=fitness_distribution,
        activity_votes=activity_votes,
        conflict_points=conflict_points,
    )

    print(f"[aggregate_prefs] 聚合完成: {people_count}人(已表态{stated_people}), "
          f"忌口={dietary_counts}, 体能={fitness_distribution}, 投票={activity_votes}")
    if stated_people < people_count:
        # 差额=未表态。票数基数只有已表态的人, 必须打出来, 否则摘要看起来是
        # "全员 N 人"的统计结果而实际基数更小。
        print(f"[aggregate_prefs] 未表态 {people_count - stated_people} 人 "
              f"(票数基数按已表态 {stated_people} 人)")
    print(f"[aggregate_prefs] 偏好提示({len(conflict_points)}条): {conflict_points or '无'}")
    return {
        "pref_summary": summary,
        "steps_log": [
            f"[aggregate_prefs] 聚合完成: {people_count}人(已表态{stated_people}), "
            f"提示{len(conflict_points)}条"
        ],
    }


def _poi_to_venue(poi: POIInfo, distance_km: Optional[float] = None) -> VenueInfo:
    """POIInfo -> VenueInfo 转换

    高德 base 字段没有建议时长, visit_duration 先按 120 分钟占位,
    由后续报价/流程提案节点按活动类型细化; rating/cost 从 biz_ext 透传。
    distance_km 由 gather_info 程序化算出(距活动城市中心), 供锚点选择与 prompt 提示。
    """
    rating: Optional[float] = None
    try:
        rating = float(poi.rating) if poi.rating else None
    except ValueError:
        pass
    ticket_price = 0
    try:
        ticket_price = int(float(poi.cost)) if poi.cost else 0
    except ValueError:
        pass
    return VenueInfo(
        name=poi.name,
        address=poi.address,
        location=poi.location,
        visit_duration=120,
        description=poi.type,
        category=poi.type,
        rating=rating,
        poi_id=poi.id,
        ticket_price=ticket_price,
        distance_km=round(distance_km, 1) if distance_km is not None else None,
    )


def _rank_pois(pois: List[POIInfo], near: Optional[Location] = None,
               near_km: float = VENUE_NEAR_KM) -> List[POIInfo]:
    """程序化预排序: 评分降序, 缺评分垫底(不淘汰), 同分按人均低优先

    高德默认序是综合权重(热度/相关性), 可能把低分店排前面; 先按评分
    排好再截断, 保证 LLM 拿到的候选头部是高分店。缺分的候选只垫底,
    不直接淘汰(高德 biz_ext.rating 空置率不低, 淘汰会误伤大量小店)。

    给了 near(目的地锚点)时再加一道**通勤圈分级**: 距锚点超过 near_km 的候选
    整体排到圈内候选之后。是"降级"不是"淘汰" —— 去千岛湖团建本身没问题,
    只是不该在去杭州市区的方案里排第一。缺坐标信息的按圈内对待(不误伤)。
    """
    def _far(p: POIInfo) -> bool:
        if near is None:
            return False
        d = amap_client._distance_km(p.location, near)
        return d is not None and d > near_km

    def _key(p: POIInfo):
        try:
            r = float(p.rating) if p.rating else -1.0
        except (TypeError, ValueError):
            r = -1.0
        try:
            c = float(p.cost) if p.cost else 99999.0
        except (TypeError, ValueError):
            c = 99999.0
        return (_far(p), -r, c)
    return sorted(pois, key=_key)


def _pick_route_anchor(venues: List[VenueInfo]) -> Optional[VenueInfo]:
    """挑"交通路线基准场地": 取距目的地锚点**最近**的候选

    基准场地只用来算"出发地 -> 目的地"的驾车距离/时长, 所以离目的地越近越好;
    用评分榜首会让交通报价整体失真(实测"杭州团建"榜首是 138km 外的千岛湖植物园,
    照它算出来的驾车里程对应的是另一趟行程)。排名已按通勤圈把圈外候选降级, 这里
    再保险一次: 缺距离信息(目的地无法归一)时退回 venues[0], 不淘汰任何候选。
    """
    if not venues:
        return None
    with_distance = [v for v in venues if v.distance_km is not None]
    if not with_distance:
        return venues[0]
    nearest = min(with_distance, key=lambda v: v.distance_km)
    if nearest is not venues[0]:
        print(f"[gather_info] 交通基准取距目的地最近的「{nearest.name}」"
              f"({nearest.distance_km:.0f}km), 而非评分榜首「{venues[0].name}」"
              f"({venues[0].distance_km if venues[0].distance_km is not None else float('nan'):.0f}km)")
    return nearest


def _rank_trains(trains: List[dict], transport_pref: Optional[str] = None) -> List[dict]:
    """火车班次程序化排序: 默认时长短优先、同时段早出发优先; 偏好含"早"则出发时间优先

    duration 形如 "07:25"(时分), 拆时分转分钟数比较; 解析失败按一天垫底。
    原始序是接口的发车时间序, 直接截断会让"时长"维度完全不被考虑。
    """
    def _dur(t: dict) -> float:
        try:
            h, m = str(t.get("duration") or "").split(":")
            return int(h) * 60 + int(m)
        except (TypeError, ValueError):
            return 24 * 60

    def _dep(t: dict) -> str:
        return str(t.get("depart") or "99:99")

    early = bool(transport_pref and "早" in str(transport_pref))
    if early:
        return sorted(trains, key=lambda t: (_dep(t), _dur(t)))
    return sorted(trains, key=lambda t: (_dur(t), _dep(t)))


async def _attach_web_notes(venues: List[VenueInfo], destination: Optional[str]) -> None:
    """博查口碑核对层: 对程序化排序后的 top3 场地搜"目的地+场地名+口碑", 提炼 web_note

    分工: 程序化排序管效率(评分/人均), Web 口碑管真伪(踩雷/照骗/乱收费)。
    未配置 BOCHA_API_KEY 或搜索失败时 web_note 保持 None, 静默降级不阻塞主流程。
    成本护栏: 只查 top3(BOCHA_WEB_NOTE_TOP), 约 0.11 元/次流程。
    """
    if not destination:
        return
    top = venues[:BOCHA_WEB_NOTE_TOP]

    async def _one(v: VenueInfo) -> None:
        try:
            hits = await asyncio.to_thread(
                bocha_client.web_search, f"{destination} {v.name} 口碑 评价", count=3,
            )
        except AppError as e:
            # 只捕"我知道怎么处理"的失败: 口碑是锦上添花, 缺了不影响方案成立。
            # 认不出的异常照旧抛出 —— 它是代码 bug, 不该被这里静默抹成"没口碑"
            print(f"[web_note] 口碑搜索失败({v.name}): {str(e)[:80]}")
            return
        if not hits:
            return
        parts = [f"{h.get('title', '')}: {h.get('snippet', '')[:60]}"
                 for h in hits[:2] if h.get("title")]
        if parts:
            v.web_note = "Web口碑 | " + "；".join(parts)

    await asyncio.gather(*[_one(v) for v in top])


def _call_status(name: str, result: CallResult) -> dict:
    """把一路的 CallResult 压成可序列化、可写进 state/日志的一行"""
    return {
        "status": result.status.value,
        "reason": result.reason,
        "attempts": result.attempts,
        "elapsed": round(result.elapsed, 2),
        "note": result.as_note(name),
    }


def _raise_required_config_missing(calls: List[Tuple[str, CallResult]]) -> None:
    """必需配置缺失(ConfigError) -> 直接抛出, 不走降级

    与其他失败分开对待的理由: 别的失败让某一维没数据, 方案还能基于其余维产出并
    标注; **必需配置缺失则意味着这些维度全空**, 继续跑只会让下游用 LLM 常识把
    空当正常填满, 产出一份看不出问题的假方案。宁可 500 也不能这样。
    """
    for name, result in calls:
        if isinstance(result.error, ConfigError):
            raise ConfigError(f"[gather_info.{name}] {result.error}",
                              user_message=result.error.user_message)


async def _timed_call(name: str, fn, *args, retries: int = 2,
                      retry_budget: float = 8.0,
                      **kwargs) -> Tuple[CallResult, datetime, datetime]:
    """线程池中调一路外部数据, 把**可分类的失败**收敛成 CallResult(不抛 AppError)

    这就是本次重构的要点: 旧版裸 gather, 任何一路抛异常都会掀翻整个节点 ——
    用户拿到 500, 其余四路已经取到的真实数据全部作废。现在每路各自结算成败,
    挂了的那一路只影响自己。

    **但不是什么都降级**: `call_external` 认不出来的异常(字段名写错、解包数量
    不对这类程序缺陷)照旧向上抛, 让节点整体失败 —— 降级只适用于"我知道怎么
    处理"的失败。默认重试 2 次、总预算 8s: 各路并行, 最长的那一路决定用户等待
    时长, 而单次请求超时是 10s, 所以一次真超时不会进重试(避免把等待翻倍)。
    """
    start = datetime.now()
    print(f"[gather_info] {name} 开始 {start.strftime('%H:%M:%S.%f')[:-3]}")
    result = await asyncio.to_thread(
        call_external, functools.partial(fn, *args, **kwargs),
        kind=name, retries=retries, retry_budget=retry_budget,
    )
    end = datetime.now()
    print(f"[gather_info] {name} 结束 {end.strftime('%H:%M:%S.%f')[:-3]} "
          f"(耗时 {(end - start).total_seconds():.2f}s, {result.as_note(name)})")
    return result, start, end


async def _maybe_weather(name: str, city: Optional[str], skip_reason: str,
                         ) -> Tuple[CallResult, datetime, datetime]:
    """城市为 None 时**不发这次外网调用**, 直接给"未启用"结论

    用于出发地天气: 同城时出发地与目的地是同一座城, 再查一次是纯浪费(高德按调用
    次数计费, 免费额度会被这种重复调用吃掉)。语义与 call_external 抛出的 DISABLED
    一致 —— 刻意不查, 不是故障。
    """
    if not city:
        now = datetime.now()
        return CallResult.disabled(skip_reason), now, now
    return await _timed_call(name, amap_client.get_weather, city)


def _overlapped(ranges: List[Tuple[datetime, datetime]]) -> bool:
    """判断多段时间范围是否两两重叠(并行证据判定: 区间交集非空; 零长区间同刻亦算)"""
    latest_start = max(s for s, _ in ranges)
    earliest_end = min(e for _, e in ranges)
    return latest_start <= earliest_end


def _date_range(start: Optional[str], end: Optional[str], max_days: int = 7) -> List[str]:
    """[start, end] 闭区间日期列表(ISO); 无 end / end<=start / 解析失败时只含 start"""
    from datetime import timedelta
    if not start:
        return []
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []
    if not end or str(end) <= str(start):
        return [start]
    try:
        d1 = datetime.strptime(str(end), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return [start]
    days = min((d1 - d0).days + 1, max_days)   # 超长区间截断, 防天气/餐饮编排失控
    return [(d0 + timedelta(days=i)).isoformat() for i in range(days)]


def _hint_text(place: Optional[Any], key: str) -> Optional[str]:
    """从地点入参取一个非空字符串字段(dict 与 PlaceInput 模型都能吃)"""
    if not place:
        return None
    if not isinstance(place, dict):
        dump = getattr(place, "model_dump", None)
        place = dump() if callable(dump) else {}
    value = (place or {}).get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


# 能进 state/BasicInfo 的 canon 键(白名单): 显式列举而不是"逐键外扩", 否则
# canon 一旦新增带对象的键就会未序列化地进 checkpointer
_CANON_PUBLIC_KEYS = ("name", "adcode", "province", "district", "level",
                      "kind", "display", "source", "query", "note")


def _canon_public(canon: Optional[dict]) -> Optional[dict]:
    """把归一结果压成可进 state 的纯数据

    resolve_city 的 location 是 Location 对象, 直接塞进 BasicInfo 会随
    checkpointer 一起序列化(有坑); 但 location 又是**距离锚点**, 四路提案与
    下游排序都要用它, 不能丢 —— 转成 {"longitude","latitude"} 保留。
    """
    if not canon:
        return None
    loc = canon.get("location")
    out = {k: canon[k] for k in _CANON_PUBLIC_KEYS if k in canon}
    out["location"] = ({"longitude": loc.longitude, "latitude": loc.latitude}
                       if loc is not None else None)
    return out


async def gather_info(state: EventPlanState) -> Dict[str, Any]:
    """基础信息采集节点: 纯工具节点, 零 LLM

    - 先做地名归一(destination -> 规范城市名), 再 asyncio.gather + to_thread 六路并行:
      1) search_venues   场地候选(extensions=all 含评分/人均), 截断 top10
      2) get_weather     活动城市天气预报(含活动日则标注)
      3) get_weather     出发地天气预报(**仅跨城**; 只喂交通提案, 见 origin_weather)
      4) geocode         出发地坐标(带目的地城市提示 + 目的地坐标作参照点消歧)
      5) query_trains    火车班次/票价(聚合数据; 跨城且有key且在预售期内才查)
      6) web_search      攻略/口碑(博查; 有key才查, 否则空)
    - 工具函数均有进程级缓存, 同参数二次调用零外网开销
    - 输出 BasicInfo 写入 state.basic_info

    出发地天气为什么单存一个 key(不并进 weather 列表):
    - 它回答的是"出发日/返回日**在路上**赶得顺不顺"(大雨大雾 -> 高速/航班/堵车),
      与目的地天气(决定活动怎么排)是两件事, 只有交通提案用得上;
    - WeatherInfo 里**没有城市字段**, 并进同一个列表后风险检测会拿出发地的雨去说
      目的地的室外项目要改室内 —— 误报且静默。分开存让这件事在结构上不可能发生,
      而不是靠"记得别读它"。

    地名可信性(本节点是主要入口):
    - destination 自由文本(如"杭州西湖区"/"临安")先经 resolve_city 归一,
      否则高德 place/text 会静默返回全国混排结果;
    - 出发地"陆家嘴"这类歧义地名带上下文 + 参照点解析, 避免落到外省村庄;
    - 解析结果离目的地过远时写进 steps_log, 不静默产出两千公里的报价。
    """
    parsed = state.get("parsed_request") or {}
    origin_name = parsed.get("origin")

    if not origin_name:
        # 仅偏好提交等场景没有 parsed_request, 防御跳过
        return {
            "basic_info": BasicInfo().model_dump(),
            "data_status": {},
            "steps_log": ["[gather_info] 无出发地信息, 跳过采集"],
        }

    activity_type = state.get("activity_type") or parsed.get("activity_type") or ""
    keyword = VENUE_KEYWORDS.get(activity_type, "景点")
    # 场地关键词跟投票走: 最高票偏好若可映射为 POI 关键词(爬山→风景区等),
    # 优先于活动类型关键词——用户投票是显式意愿, 权重高于文本解析的类型;
    # 未映射的偏好名不用作关键词(避免拿任意文本去搜场地), 保留原关键词
    pref_state = state.get("pref_summary")
    if pref_state is not None and not isinstance(pref_state, dict):
        pref_state = pref_state.model_dump()
    top_votes = (pref_state or {}).get("activity_votes") or {}
    if top_votes:
        top_act = next(iter(top_votes))
        keyword = ACTIVITY_KEYWORDS.get(top_act, keyword)
        print(f"[gather_info] 场地关键词跟投票走: 最高票「{top_act}」"
              f"({top_votes[top_act]}票) -> {keyword}")
    activity_date = parsed.get("date")
    date_end = parsed.get("date_end")

    # ---- 入口的地点入参(前端"省市县选择 + 更具体的地点"; 可缺, 缺=老路) ----
    # 读 raw_request["structured"] 而不是 parsed_request: 前者是**入口参数**,
    # 后者是 LLM 抽取结果 —— 两类东西。而且 PlaceInput 不能挂 EventRequest:
    # 那是自由文本路径的 LLM 工具 schema, 幻觉出的"权威归属"会直接跳过
    # _name_related 等闸门 —— 假的权威比没有更糟。
    structured = (state.get("raw_request") or {}).get("structured") or {}
    dest_place = structured.get("destination_place") or None
    origin_place = structured.get("origin_place") or None
    if dest_place or origin_place:
        print(f"[gather_info] 地点入参: 出发地={origin_place} 目的地={dest_place}")

    # ---- 地名归一: 目的地先解析成规范目的地(同城时取出发地) ----
    destination_raw = parsed.get("destination") or origin_name   # 缺省同城
    # hint 为空时不传第三个实参: 否则缓存键从 ["x"] 变成 ["x", null], 老缓存全冷
    dest_args = (destination_raw, dest_place) if dest_place else (destination_raw,)
    dest_r, d_s, d_e = await _timed_call(
        "resolve_city", amap_client.resolve_city, *dest_args)
    dest_canon = dest_r.value if dest_r.usable else None
    # 归一失败(网络抖动/地名认不出)退回原始文本: 地名没归一只是"可能搜偏",
    # 比直接 500 好, 但要记进 data_status —— 它是"这次结果可能不精确"的原因之一
    destination = (dest_canon or {}).get("name") or destination_raw   # 城市名: 天气/火车用
    dest_location = (dest_canon or {}).get("location")   # 距离锚点 + 出发地消歧参照点
    # display 进日志与 prompt(具体地点带上行政归属); 检索用**用户原话** ——
    # search_venues 要靠"莫干山"这三个字判断口径, 传归一后的"湖州市"就退化成了
    # 全市范围检索, 又会把 35km 外的候选捞回来。
    dest_display = (dest_canon or {}).get("display") or destination
    if dest_canon:
        print(f"[gather_info] 目的地归一: 「{destination_raw}」-> 「{dest_display}」"
              f"[{dest_canon.get('kind')}/{dest_canon.get('source')}]")

    # 出发地的城市提示: 只在"同城/近郊/出发地本身不可信"时才给, 跨城出发地
    # 直接全国解析即可 —— 否则每次跨城都要白发一次注定 ENGINE_RESPONSE_DATA_ERROR
    # 的 geocode(高德按 QPS 计费, 免费额度会被这种无效调用吃掉)。
    # 旧实现在这里**同步**调 resolve_city, 会把整个事件循环卡住一次外网往返;
    # 现在同走线程池。
    o_args = (origin_name, origin_place) if origin_place else (origin_name,)
    o_canon_r, o_s, o_e = await _timed_call("resolve_city_origin",
                                            amap_client.resolve_city, *o_args)
    origin_canon = o_canon_r.value if o_canon_r.usable else None
    origin_level = (origin_canon or {}).get("level") or ""
    same_city = (amap_client._norm_admin((origin_canon or {}).get("name"))
                 == amap_client._norm_admin(destination))
    origin_admin = bool(origin_place and (_hint_text(origin_place, "city")
                                          or _hint_text(origin_place, "district")
                                          or _hint_text(origin_place, "province")))
    if origin_admin:
        # 用户选了出发地的省市县 -> 上下文直接用权威归属的城市名
        # (区县/直辖市都能落到市名: 浦东新区 -> 上海市, 栾川县 -> 洛阳市)。
        # 旧逻辑"跨城且级别可信就不给提示"正是 1964km bug 的入口
        hint_city = (origin_canon or {}).get("name") or _hint_text(origin_place, "city")
    else:
        hint_city = destination if (origin_canon is None or same_city
                                    or origin_level in amap_client._UNRELIABLE_LEVELS) else None

    # 出发地天气: **只在跨城时查**。它回答的是"出发那天/回来那天赶路顺不顺"
    # (大雨大雾 -> 高速/航班/堵车, 要不要提前走), 与目的地天气(决定活动怎么排)
    # 是两回事, 所以单独一路而不是并进 weather 列表。同城时出发地=目的地, 同一座
    # 城查两遍是同一个结果, 直接跳过(见 _maybe_weather)。
    origin_weather_city = (None if same_city
                           else ((origin_canon or {}).get("name") or origin_name))

    # ---- 六路并行采集(出发地 geocode 带城市提示与参照点, 修歧义地名落外省) ----
    # 场地检索必须拿**同一个** hint: 只给 resolve_city 不给 search_venues, 检索
    # 这一步会重新猜一次, 两条路得出两份 canon —— 正是"交通按上海算、场地按莫干山
    # 排"那个分叉。同一个 hint -> resolve_city 缓存命中同一对象。
    venue_args = ((destination_raw, keyword, dest_place) if dest_place
                  else (destination_raw, keyword))
    # 攻略/口碑的检索词带上 detail: 「老君山 团建 攻略」远好于「洛阳市 团建 攻略」
    guide_topic = _hint_text(dest_place, "detail") or destination_raw
    # 各路各自结算成败: 一路挂掉不再掀翻整个节点(旧版裸 gather 就是这么炸的)
    (pois_r, v_s, v_e), (weather_r, w_s, w_e), (ow_r, ow_s, ow_e), \
        (origin_r, g_s, g_e), (trains_r, t_s, t_e), (guides_r, b_s, b_e) = \
        await asyncio.gather(
        _timed_call("venues", amap_client.search_venues, *venue_args),
        _timed_call("weather", amap_client.get_weather, destination),
        _maybe_weather("weather_origin", origin_weather_city,
                       "同城, 出发地天气与目的地相同"),
        _timed_call("geocode", amap_client.geocode, origin_name, hint_city, dest_location),
        _timed_call("trains", train_client.query_trains, origin_name, destination, activity_date),
        _timed_call("guides", bocha_client.web_search,
                    f"{guide_topic} {activity_type or '团建'} 攻略 口碑", count=5),
    )

    # 必需配置缺失(如没配高德 key)不走降级分支: 各路皆空时下游只会拿 LLM 常识
    # 编出一份"看起来正常"的报价单, 这正是"带坏配置继续跑"要防的
    calls: List[Tuple[str, CallResult]] = [
        ("resolve_city", dest_r), ("resolve_city_origin", o_canon_r),
        ("venues", pois_r), ("weather", weather_r), ("weather_origin", ow_r),
        ("geocode", origin_r), ("trains", trains_r), ("guides", guides_r),
    ]
    _raise_required_config_missing(calls)

    pois = pois_r.value or []
    weather = weather_r.value or []
    origin_weather = ow_r.value or []
    origin_loc = origin_r.value or None
    trains = trains_r.value or []
    guides = guides_r.value or []

    parallel_ok = _overlapped([(v_s, v_e), (w_s, w_e), (g_s, g_e)])
    print(f"[gather_info] 采集并行重叠: {parallel_ok}")

    # 程序化预排序(评分降序 + 通勤圈分级)后再截断; 顺带算出各候选距目的地锚点的距离
    ranked = _rank_pois(pois, dest_location)[:MAX_VENUES]
    venues = [
        _poi_to_venue(p, amap_client._distance_km(p.location, dest_location))
        for p in ranked
    ]
    await _attach_web_notes(venues, destination_raw)
    trains = _rank_trains(trains, parsed.get("transport_pref"))[:MAX_TRAINS]

    # ---- 汇合后: 出发地 -> 交通锚点场地 路线(依赖场馆坐标, 无法入并行段) ----
    # 锚点不直接用 venues[0]: 评分最高的可能是 300km 外的千岛湖, 会让"杭州一日团建"
    # 的驾车距离与交通报价整体失真; 取距目的地锚点最近的候选(见 _pick_route_anchor)。
    route_info = None
    route_log = "无路线(缺出发地坐标或无场地候选)"
    anchor = _pick_route_anchor(venues) if venues else None
    if origin_loc and anchor:
        direction_r, r_s, r_e = await _timed_call(
            "direction", amap_client.get_route_info, origin_loc, anchor.location, "driving",
        )
        _raise_required_config_missing([("direction", direction_r)])
        calls.append(("direction", direction_r))
        route_info = direction_r.value if direction_r.usable else None
        route_log = f"至「{anchor.name}」{route_info.description if route_info else '失败'}"

    # 出发地可信度: 解析点离目的地过远时显式提示(而不是静默算出一条跨省路线)
    origin_note = ""
    if origin_loc and dest_location:
        origin_km = amap_client._distance_km(origin_loc, dest_location)
        if origin_km is not None:
            origin_note = f", 出发地距目的地约{origin_km:.0f}km"
            if origin_km > ORIGIN_WARN_KM:
                origin_note += "(较远, 请确认出发地填写是否正确)"

    # 活动日期区间内的天气标注(高德预报仅未来3-4天, 超出范围则保留全部预报)
    # 先分清"预报没取到"和"活动日超出预报范围": 两者都表现为 matched 为空, 但原因
    # 完全不同 —— 前者是这一路降级了, 说成"超出预报范围"就是拿一个假的解释糊弄人
    weather_note = ""
    if not weather and weather_r.is_degraded:
        weather_note = f"(天气预报未取到: {weather_r.reason})"
    elif activity_date:
        dates = _date_range(activity_date, date_end)
        matched = [w for w in weather if w.date in dates]
        if matched:
            brief = ", ".join(f"{w.date[5:]}{w.day_weather}" for w in matched)
            weather_note = f", 活动期间预报: {brief}"
        elif weather:
            weather_note = "(活动日期超出预报范围, 保留全部预报)"

    # 出发地天气标注: 只关心**出发日**与**返回日** —— 用户在路上的那两天, 出发地
    # 大雨大雾影响的是赶路。同目的地天气一样, 返回日多半落在高德 3~4 天预报之外
    # (尤其是现在规划下个月的活动), 这时如实说明覆盖范围, 不拿不相干的预报冒充。
    origin_weather_note = ""
    if not origin_weather and ow_r.is_degraded:
        origin_weather_note = f"(出发地天气预报未取到: {ow_r.reason})"
    elif origin_weather:
        ow_dates = _date_range(activity_date, date_end)
        ow_matched = [w for w in origin_weather if w.date in ow_dates]
        if ow_matched:
            brief = ", ".join(f"{w.date[5:]}{w.day_weather}" for w in ow_matched)
            origin_weather_note = f", 出发地({origin_weather_city})天气: {brief}"
        elif ow_dates:
            origin_weather_note = (
                f"(出发日/返回日 {ow_dates[0]}/{ow_dates[-1]} 超出出发地预报范围, "
                f"仅覆盖 {origin_weather[0].date}~{origin_weather[-1].date})")
        else:
            origin_weather_note = (f", 出发地({origin_weather_city})天气已取到"
                                   f"{len(origin_weather)}天(未指定活动日期)")

    # 数据可用性: 哪一路降级/未启用, 为什么 —— 交给下游节点与响应对外说明
    data_status = {name: _call_status(name, r) for name, r in calls}
    trouble = [name for name, r in calls if r.is_degraded]
    # 出发地天气的 DISABLED 是"同城刻意不查", 与"缺 API Key"完全不是一回事,
    # 混进下面那条"未启用(缺 API Key, 非故障)"的提示就是错报原因
    unavailable = [name for name, r in calls
                   if r.status is CallStatus.DISABLED and name != "weather_origin"]
    # "接口正常但确实没查到"对必需维度而言同样是坏消息, 但过去它既不进 trouble
    # (EMPTY 不是降级)也不进 unavailable, 于是整条链路上没有任何人提到它 ——
    # 用户只看到"场地候选不足/地点待定", 既不知道原因, 也无从判断该改什么。
    empty_critical = [n for n in CRITICAL_DIMENSIONS
                      if n in data_status and data_status[n]["status"] == CallStatus.EMPTY.value]

    basic = BasicInfo(venues=venues, weather=weather, origin_weather=origin_weather,
                      route_info=route_info,
                      origin_location=origin_loc, train_info=trains, guides=guides,
                      route_anchor=({"name": anchor.name, "distance_km": anchor.distance_km}
                                    if anchor else None),
                      destination_canon=_canon_public(dest_canon))
    print(f"[gather_info] 采集完成: 目的地={dest_display}{origin_note}, "
          f"候选{len(venues)}个(关键词={keyword}), "
          f"天气{len(weather)}天{weather_note}{origin_weather_note}, {route_log}, "
          f"火车{len(trains)}班次, 攻略{len(guides)}条"
          + (f"; 降级={trouble}" if trouble else "")
          + (f"; 未启用={unavailable}" if unavailable else "")
          + (f"; 必需维度无结果={empty_critical}" if empty_critical else ""))

    steps = [
        f"[gather_info] 目的地={dest_display}{origin_note}; "
        f"并行{'✓重叠' if parallel_ok else '✗未重叠'}"
        f"(场地{len(venues)}/天气{len(weather)}天/出发地天气{len(origin_weather)}天"
        f"/火车{len(trains)}/攻略{len(guides)}), 路线: {route_log}",
    ]
    # 地名归一失败单独说: 它不会让任何一路"失败", 但会让场地/路线整体搜偏,
    # 属于"必须让用户知道"的一种静默
    if destination_raw and dest_canon is None:
        steps.append(f"[gather_info] 目的地「{destination_raw}」未能识别为城市或具体地点, "
                     f"场地按原始文本检索可能搜偏, 建议让用户写明城市或换个地名")
    # 「更具体的地点」与所选省市县不同城: 归属照用户所选, 但候选池可能因此为空 ——
    # 这是**用户可修**的一类问题, 必须说出来(与"宁可空池也不给错数据"一致)
    if (dest_canon or {}).get("note"):
        steps.append(f"[gather_info] {dest_canon['note']}")
    if empty_critical:
        steps.append("[gather_info] 以下必需维度本次**没有查到结果**(接口正常, 不是故障): "
                     + "；".join(data_status[n]["note"] for n in empty_critical)
                     + "。方案中该部分只能按常识估算, 请核对输入(目的地是否写成景区/古镇名、"
                       "出发地是否可识别)")
    # 降级必须显式写进日志: 用户看到的报价里少了哪一维真实数据、为什么少,
    # 要能一眼看到, 而不是自己去数"怎么没有火车票价"
    if trouble:
        steps.append("[gather_info] 以下维度本次降级, 结果缺少该维度真实数据: "
                     + "；".join(f"{data_status[n]['note']}" for n in trouble))
    if unavailable:
        steps.append("[gather_info] 以下维度未启用(缺 API Key, 非故障): "
                     + "；".join(f"{data_status[n]['note']}" for n in unavailable))

    return {
        "basic_info": basic.model_dump(),
        "data_status": data_status,
        "steps_log": steps,
    }


# ============ 四路提案(Send API 并行派发) ============

# ---- Send 切片的"契约": 四路各拿一份, 形状由这里的模型说死 ----

class _Destination(BaseModel):
    """目的地打包(**四路共用同一个形状**)

    今天同一个键名 `destination` 在四路里装的是**不同的东西**: 交通那路装用户原话
    ("莫干山" —— 它决定"同城/跨城"怎么判、检索该用什么词), 场地那路装归一之后的
    展示名("莫干山（浙江省湖州市德清县）")。名字一样、内容两样, 读的人得一路追到
    派发处才知道手上这个究竟是什么; 而餐饮那路**一个都没拿到**(它只能从 venues[0]
    反推自己在哪儿)。于是"四路说的是不是同一个地方"只能靠人肉核对。

    打包之后四路拿到同一个对象, 各取自己要的那一面, 字段名也照实写: `raw` 就是
    用户原话, `display` 就是展示名, 不再有"叫 destination 但不是那个意思"的键。
    """
    model_config = ConfigDict(extra="forbid")

    raw: Optional[str] = None     # 用户原话: 检索口径与同城/跨城判断用这个
    display: Optional[str] = None  # 归一后展示名(带行政归属), 给模型看 / 进日志
    canon: dict = Field(default_factory=dict)   # 归一结果(name/kind/adcode/query/location...)
    place: dict = Field(default_factory=dict)   # 入口地点入参(前端省市县选择): 检索约束


class _ProposalPayload(BaseModel):
    """四路 Send 切片的公共部分

    **为什么要类型**(而不是继续用裸 dict): 现在的写法是生产端写
    `{"train_datas": train_data}`、消费端读 `payload.get("train_data")` —— 两边
    **都不会报错**, 只是这个键静默变成 None: prompt 里少一整块真实班次, 模型
    凭常识估算, 从头到尾没有一行日志、没有一个异常。有了模型, 生产端多写一个键
    (`extra="forbid"`)当场抛, 消费端改用属性访问, 名字写错同样当场抛。

    字段全部给默认值: 切片**构造**失败是生产端(_proposal_sends)的事, 消费端
    不该因为某个可选切片没给就整路炸掉。
    """
    model_config = ConfigDict(extra="forbid")

    people_count: int = 0
    budget: Optional[float] = None
    date: Optional[str] = None
    date_end: Optional[str] = None
    destination: _Destination = Field(default_factory=_Destination)
    conflict_report: Optional[dict] = None
    user_feedback: List[str] = Field(default_factory=list)
    # 上一轮这一路的结果(回炉时才有): 用来挡住"回炉越改越糟" ——
    # 见 _keep_better_proposal
    previous_proposal: Optional[dict] = None


class _TransportPayload(_ProposalPayload):
    route_info: dict = Field(default_factory=dict)
    origin: Optional[str] = None
    transport_pref: Optional[str] = None
    train_data: List[dict] = Field(default_factory=list)
    origin_weather: List[dict] = Field(default_factory=list)
    route_anchor: Optional[str] = None


class _VenuePayload(_ProposalPayload):
    venues: List[dict] = Field(default_factory=list)
    activity_votes: dict = Field(default_factory=dict)
    activity_type: Optional[str] = None


class _FlowPayload(_ProposalPayload):
    activity_votes: dict = Field(default_factory=dict)
    fitness_distribution: dict = Field(default_factory=dict)
    activity_type: Optional[str] = None
    origin_location: Optional[dict] = None


class _FoodPayload(_ProposalPayload):
    venues: List[dict] = Field(default_factory=list)
    dietary_all: List[str] = Field(default_factory=list)
    guides: List[dict] = Field(default_factory=list)


def _degraded_proposal(category: str, exc: BaseException) -> dict:
    """某一路生成失败时的占位提案: 保留槽位, 明说"失败"

    为什么必须留一个(而不是干脆不写这个 key): `proposals` 的合并是**按 key 的**
    (merge_dict), 少一路时下游各有各的"这一路不存在"分支, 容易静默少算钱;
    留一个 category 正确的空提案, "四路齐"这个形状才是不变的。

    文案必须与"模型没给明细"区分开 —— 一个是**这一路坏了**(该重试/该人工补),
    一个是**确实没合适的选项**(是正常结论)。把两者写成同一句话, 人就会拿着
    "本方案不含餐饮"去当"这一带没餐厅"读。
    """
    name = CATEGORY_NAMES.get(category, category)
    if isinstance(exc, CapabilityDisabled):
        # 可选能力未开: errors.py 明说它"不是故障、不该被当成错误上报"。
        # 但这一维确实没有内容可给, 文案就照实说 —— 写成"生成失败"会让人去查
        # 上游, 而真正该做的是去配 key。
        return Proposal(
            category=category, items=[], total_cost=0.0,
            description=(f"⚠️ {name}这一维**未启用**(原因: {exc.user_message}) —— "
                         f"本次方案里没有这一维的报价。这不是故障, 是该项能力尚未接入; "
                         f"如需该维度请配置对应数据源, 或人工补齐后再执行"),
            options=[], meals=[], route_segments=[],
        ).model_dump()
    if isinstance(exc, AppError):
        reason, code = exc.user_message, exc.code
    else:
        reason, code = "模型输出不符合格式要求", "invalid_output"
    return Proposal(
        category=category, items=[], total_cost=0.0,
        description=(f"⚠️ {name}这一维**生成失败**(原因: {reason} / {code}) —— "
                     f"本次方案里**没有**这一维的报价, 这不等于「没有合适的选择」。"
                     f"请重试, 或人工补齐后再执行"),
        options=[], meals=[], route_segments=[],
    ).model_dump()


def _keep_better_proposal(category: str, new: dict, previous: Optional[dict]) -> Tuple[dict, Optional[str]]:
    """回炉时"新的不如旧的"就别换: 返回 (采用的提案, 说明或 None)

    回炉的合并是**按 key 后写覆盖前写**(merge_dict), 所以第二轮的提案会无条件
    盖掉第一轮 —— 包括"第二轮更差"的情况。真实的坏法有两类, 都是静默的:

      1. 第二轮这一路**失败降级**成了空壳;
      2. 第二轮模型**没给明细**(空 items 但结构合法)。

    两者都会让四路合计**少算这一维的钱**, 而 reconcile 只有"空明细"这一条拦得住
    (拦不住"少算"本身): 一份 0 元空壳 + 一个"校验通过"的结论, 比"带着已知冲突
    交人工裁决"糟得多。所以宁可留着上一轮那份**有数字、有冲突**的版本。

    只比"有没有明细": 这是在**两份都不完美**之间选, 判据必须简单到不需要解释。
    不做"哪份更便宜/更好"的聪明判断 —— 谁也没法程序化判定两份方案哪份更好。
    """
    new_items = (new or {}).get("items") or []
    prev_items = (previous or {}).get("items") or []
    if new_items or not prev_items:
        return new, None
    name = CATEGORY_NAMES.get(category, category)
    note = (f"[propose:{category}] 本轮{name}未给出明细(上一轮有 {len(prev_items)} 条), "
            f"保留上一轮结果 —— 空壳会让四路合计少算这一维的费用")
    print(f"[propose:{category}] {note}")
    return previous, note


def _proposal_node(category: str, payload_model):
    """四路共用的节点外壳: 解析切片 -> 失败隔离 -> 挡住回炉越改越糟

    四路本来就是同一类东西(收一份切片、调一次模型、交一份同形状的提案), 于是
    "切片的形状""一路炸了怎么办""回炉该不该换掉上一轮"这三件事只在这一个地方
    定义, 而不是在四个函数里各写一遍(各写一遍的结果是四条路各错各的)。

    **失败隔离**: 旧行为是"四路一起死" —— 四路是同一次 Send 扇出, 谁抛异常整个
    子图就带着异常结束, 已经跑完的另外三路(以及它们真金白银换来的 LLM 结果和
    POI 检索)全部作废, 用户看到 500。而四路之间本来就是独立的。

    三档处置, 边界与 errors.py 的"核心规则"完全一致 —— 只有该降级的才降级:

      - 必需配置缺失(ConfigError) -> 抛。四路全空只会拼出一个假方案,
        这种情况必须让整个节点失败、被人看到, 而不是产出一份"看着完整"的空壳。
      - 分类得出的失败(AppError) / 模型字段过不了校验 -> **只废这一路**。
        上游抖动、模型给不出合规输出, 都是"这一路的问题"。可选能力未开
        (CapabilityDisabled) 也归这一档: 它不是故障, 更不该把整批拖死。
      - 其余异常一律抛出。**这是本壳最要紧的一条**: 字段名写错、解包数量不对
        这类程序缺陷若被顺手降级, 就变成"这维没数据"—— 正是 errors.py 开头
        第 3 条要修的病, 也是 test_error_handling 里钉住的行为。

    **切片解析在降级壳外面**: 切片是我们的代码发的, 它不该长成别的样子。构造
    失败属于程序缺陷, 必须冒出来 —— 放进 try 里就会被下面那句
    `except (AppError, ValidationError)` 当成"模型输出不合法"而静默降级。
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(payload) -> Dict[str, Any]:
            p = payload_model.model_validate(payload)

            try:
                out = await fn(p)
            except ConfigError:
                raise
            except (AppError, ValidationError) as e:
                name = CATEGORY_NAMES.get(category, category)
                print(f"[propose:{category}] {name}这一路失败, 已降级"
                      f"(其余三路不受影响): {str(e)[:160]}")
                # 回炉时这一路炸了: 上一轮的结果至少是一份完整报价, 拿它顶上
                # (降级空壳会把这一维的钱从合计里抹掉, 而问题还在)
                prop, kept = _keep_better_proposal(
                    category, _degraded_proposal(category, e), p.previous_proposal)
                return {
                    "proposals": {category: prop},
                    "steps_log": [kept or (f"[propose:{category}] 生成失败, 本维降级: "
                                           f"{str(e)[:100]}")],
                }

            prop, kept = _keep_better_proposal(
                category, out["proposals"][category], p.previous_proposal)
            if kept:
                out = {**out, "proposals": {category: prop},
                       "steps_log": list(out.get("steps_log") or []) + [kept]}
            return out
        return wrapper
    return decorator


def _finalize_proposal(raw: dict, category: str) -> dict:
    """提案收尾: category 以槽位为准(不信任 LLM 自报), total_cost 程序化重算

    LLM 自报的求和不可靠, 统一按 items 逐项求和覆盖——这是
    "total_cost == items 求和" 验收断言的保障层。
    options(多候选备选, 如多种交通方式/多家候选餐厅)原样保留, 供审批页挑选。
    """
    p = Proposal(
        category=category,
        items=raw.get("items") or [],
        total_cost=0.0,
        description=raw.get("description") or "",
        options=raw.get("options") or [],
        meals=raw.get("meals") or [],
        route_segments=raw.get("route_segments") or [],
    )
    p.total_cost = round(float(sum(i.cost for i in p.items)), 2)
    return p.model_dump()


async def _invoke_proposal(path: str, prompt: str, category: str) -> Tuple[dict, datetime, datetime]:
    """单路提案 LLM 调用: 计时(供并行重叠验证) + 重试封装 + 收尾"""
    start = datetime.now()
    print(f"[propose:{path}] LLM 开始 {start.strftime('%H:%M:%S.%f')}")
    raw = await asyncio.to_thread(invoke_structured, Proposal, prompt)
    end = datetime.now()
    print(f"[propose:{path}] LLM 结束 {end.strftime('%H:%M:%S.%f')} "
          f"(耗时 {(end - start).total_seconds():.2f}s)")
    return _finalize_proposal(raw.model_dump(), category), start, end


def _proposal_sends(state: EventPlanState, only: Optional[set] = None,
                    carry_previous: bool = True) -> List[Send]:
    """构造四路 Send 切片(首次派发与回炉重派发共用)

    每路只携带自己需要的 state 切片, 子任务互不可见对方数据;
    state 里有 conflict_report 时注入各路 payload(回炉时由 prompt 构造器转为修改指令);
    state 里有 user_feedback 时同样注入(HITL 否决重走的修改意见)。

    only: 只派发这几**类**(category, 如 {"flow"}; 定向回炉用, 见 route_after_reconcile)。
    None = 四路全发。

    carry_previous: 是否把**本路上一轮的结果**带给节点(回炉时"新的不如旧的"就别换,
    见 _keep_better_proposal)。只有同一次生成里的回炉才该带 —— 一个"新一轮生成"的
    起点(首次派发、HITL 否决后重走)带上它就错了: 那时 state.proposals 里躺着的
    正是用户刚否掉的那一版, 本轮若没给出明细, 保留它 = 把用户否掉的方案悄悄换回来。
    """
    basic = state.get("basic_info") or {}
    venues = basic.get("venues") or []
    route_info = basic.get("route_info") or {"note": "路线未采集, 按常识距离估算"}
    origin_location = basic.get("origin_location")
    train_data = basic.get("train_info") or []
    guides = basic.get("guides") or []
    # 出发地天气只给交通路: 它影响的是去程/返程赶路, 与场地/流程/餐饮无关。
    # 风险检测读的是 weather(目的地), 读不到这里 —— 结构上就串不了台。
    origin_weather = basic.get("origin_weather") or []
    parsed = state.get("parsed_request") or {}
    people_count = parsed.get("people_count") or 0
    budget = parsed.get("budget")
    activity_type = state.get("activity_type") or parsed.get("activity_type")
    date = parsed.get("date")
    date_end = parsed.get("date_end")
    origin = parsed.get("origin")
    destination = parsed.get("destination")
    transport_pref = parsed.get("transport_pref")
    conflict_report = state.get("conflict_report")
    user_feedback = state.get("user_feedback") or []
    # 目的地归一结果: 四路提案必须认**同一份**目的地, 否则交通按"上海"算、流程按
    # "莫干山"排, 各说各话。display 用于 prompt 与日志(具体地点带行政归属),
    # location 是距离锚点(流程路要用它剔除 35km 外的活动点)。
    dest_canon = basic.get("destination_canon") or {}
    dest_display = dest_canon.get("display") or destination
    # 入口的地点入参要一路透传到 propose_flow: 它自己也会调 search_venues
    # (流程路的活动点), 不透传就会用**另一份** canon 重新猜一次 —— 正是
    # "交通按上海算、流程按莫干山排"的分叉
    dest_place = ((state.get("raw_request") or {}).get("structured")
                  or {}).get("destination_place") or None
    # 四路拿到**同一个**目的地对象(见 _Destination): 各取自己那一面 ——
    # 用户原话决定检索口径与同城/跨城, 展示名给人看, canon 里的 location 是距离锚点。
    # 旧版是三个路三个不同的 destination(字符串各写各的, 餐饮那路一个都没拿到)。
    dest = _Destination(raw=destination, display=dest_display,
                        canon=dest_canon, place=dest_place or {})

    # pref_summary 兼容 dict 与 pydantic 两种形态
    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}

    # 上一轮各路的结果: 回炉时"新的不如旧的"就别换(见 _keep_better_proposal)。
    # 不带时一律 None —— 首次派发时 proposals 本来就是空的, 与旧行为一致。
    prev = (state.get("proposals") or {}) if carry_previous else {}

    # category -> Send 写成字典而不是列表: 定向回炉按 **category** 选路
    # (冲突归属用的是 category, 节点名却带 propose_ 前缀, 靠字符串拼前缀迟早拼错)
    by_category = {
        "transport": Send("propose_transport", _TransportPayload(
            route_info=route_info, people_count=people_count, budget=budget,
            destination=dest, origin=origin, transport_pref=transport_pref,
            train_data=train_data, origin_weather=origin_weather,
            date=date, date_end=date_end,
            route_anchor=(basic.get("route_anchor") or {}).get("name"),
            conflict_report=conflict_report, user_feedback=user_feedback,
            previous_proposal=prev.get("transport"),
        ).model_dump()),
        "venue": Send("propose_venue", _VenuePayload(
            venues=venues, people_count=people_count, budget=budget, destination=dest,
            activity_votes=pref.get("activity_votes") or {},
            activity_type=activity_type,
            conflict_report=conflict_report, user_feedback=user_feedback,
            previous_proposal=prev.get("venue"),
        ).model_dump()),
        "flow": Send("propose_flow", _FlowPayload(
            activity_votes=pref.get("activity_votes") or {},
            fitness_distribution=pref.get("fitness_distribution") or {},
            activity_type=activity_type, date=date, date_end=date_end,
            destination=dest, origin_location=origin_location,
            conflict_report=conflict_report, user_feedback=user_feedback,
            previous_proposal=prev.get("flow"),
        ).model_dump()),
        "food": Send("propose_food", _FoodPayload(
            venues=venues, dietary_all=pref.get("dietary_all") or [],
            people_count=people_count, budget=budget, guides=guides,
            date=date, date_end=date_end, destination=dest,
            conflict_report=conflict_report, user_feedback=user_feedback,
            previous_proposal=prev.get("food"),
        ).model_dump()),
    }
    if only is not None:
        return [s for cat, s in by_category.items() if cat in only]
    return list(by_category.values())


def dispatch_proposals(state: EventPlanState) -> list:
    """gather_info 之后的条件边路由: generate_plan 门控 + Send 派发四路切片

    - generate_plan=False(如 /parse)或无 basic_info -> 返回空列表, 流程直接结束
      (不浪费 4 次 LLM 调用)
    - carry_previous=False: 这里是**一次生成的起点**(首次跑, 或 HITL 否决后重走),
      此时 state.proposals 里如果有东西, 那是用户刚否决的那一版 —— 不能拿它当
      "上一轮"留着(见 _proposal_sends)
    """
    if not state.get("generate_plan"):
        return []
    if not (state.get("basic_info") or {}):
        return []
    return _proposal_sends(state, carry_previous=False)


@_proposal_node("transport", _TransportPayload)
async def propose_transport(p: _TransportPayload) -> Dict[str, Any]:
    """交通提案: 按 人数/距离/用户偏好 动态候选池给出 2-3 种方式报价

    注意: origin/transport_pref/train_data 必须原样透传给 prompt 构造器 ——
    历史 bug 就是这里漏传, 导致 prompt 永远显示"出发地: 未提供 | 目的地: 与出发地
    同城", 且用户的"优先高铁"不会置顶, 火车真实班次也进不了。

    destination 取**用户原话**(p.destination.raw): prompt 判"同城/跨城"是拿它与
    origin 做字符串比较, 拿归一后的展示名去比会把"上海市"和"上海"判成两个城市
    (见 build_transport_prompt docstring)。归一结果只作补充说明传进 canon。
    """
    prompt = build_transport_prompt(
        p.route_info, p.people_count, p.budget,
        origin=p.origin,
        destination=p.destination.raw,
        destination_canon=p.destination.canon,
        transport_pref=p.transport_pref, train_data=p.train_data,
        origin_weather=p.origin_weather, date=p.date, date_end=p.date_end,
        route_anchor=p.route_anchor,
        conflict_report=p.conflict_report, user_feedback=p.user_feedback,
    )
    prop, s, e = await _invoke_proposal("transport", prompt, "transport")
    return {
        "proposals": {"transport": prop},
        "steps_log": [f"[propose:transport] {s.strftime('%H:%M:%S.%f')}~{e.strftime('%H:%M:%S.%f')} "
                      f"合计{prop['total_cost']}元"],
    }


@_proposal_node("venue", _VenuePayload)
async def propose_venue(p: _VenuePayload) -> Dict[str, Any]:
    """场地提案: 从 POI 候选选场地并报价; 候选不足时程序化拒绝(零LLM, 不编造)"""
    venues = p.venues
    if len(venues) < MIN_VENUE_CANDIDATES:
        prop = Proposal(
            category="venue", items=[], total_cost=0.0,
            description=(f"场地候选不足(仅{len(venues)}个, 少于{MIN_VENUE_CANDIDATES}个), "
                         "无法给出可靠报价, 请扩充采集或更换关键词重搜"),
        ).model_dump()
        print(f"[propose:venue] 候选不足({len(venues)}), 程序化拒绝, 零LLM")
        return {
            "proposals": {"venue": prop},
            "steps_log": [f"[propose:venue] 候选不足({len(venues)}), 明确拒绝而非编造"],
        }

    # 高德 POI 无容量字段, 只传名称/类型/评分/人均等真实数据, 容量由 LLM 推断并标注
    # web_note 为博查口碑核对结果(排序top3才有), prompt 要求差评关键词写入推荐理由
    # 距目的地 为程序化算出的直线距离, 让 LLM 能按"车程是否可接受"取舍 ——
    # 目的地是城市时锚点是市中心, 是具体地点(景区/古镇)时锚点是该地点本身
    slim = [{"name": v.get("name"), "address": v.get("address"), "type": v.get("category"),
             "rating": v.get("rating"), "人均": v.get("ticket_price") or None,
             "距目的地": (f"{v['distance_km']:.0f}km" if v.get("distance_km") is not None else None),
             "web_note": v.get("web_note") or None}
            for v in venues[:MAX_VENUES]]
    prompt = build_venue_prompt(slim, p.people_count, p.budget,
                                destination=p.destination.display,
                                activity_votes=p.activity_votes,
                                activity_type=p.activity_type,
                                conflict_report=p.conflict_report,
                                user_feedback=p.user_feedback)
    prop, s, e = await _invoke_proposal("venue", prompt, "venue")
    return {
        "proposals": {"venue": prop},
        "steps_log": [f"[propose:venue] {s.strftime('%H:%M:%S.%f')}~{e.strftime('%H:%M:%S.%f')} "
                      f"合计{prop['total_cost']}元"],
    }


@_proposal_node("flow", _FlowPayload)
async def propose_flow(p: _FlowPayload) -> Dict[str, Any]:
    """流程提案: 偏好全覆盖 + 每个活动绑定真实POI + 程序化逐段转场路线

    活动地点化: 按偏好活动(高票优先)在目的地二次搜索 POI(ACTIVITY_KEYWORDS
    映射关键词), prompt 只允许从真实候选里选 location, 杜绝编造地名;
    提案定稿后 _attach_route_segments 程序化调高德 direction 拼接逐段转场。
    """
    votes = p.activity_votes
    destination = p.destination.raw            # 用户原话: 决定检索口径
    origin_location = p.origin_location
    # 流程里的活动点同样要落在目的地附近: 不传锚点的话"杭州团建"会排进 138km 外的
    # 千岛湖活动点, 和场地提案的取舍口径也不一致(场地已按通勤圈降级)
    dest_canon = p.destination.canon
    near = amap_client._parse_location(dest_canon.get("location"))
    dest_place = p.destination.place or None    # 与 gather_info 同一份入口入参

    async def _search_act(act: str) -> List[dict]:
        kw = ACTIVITY_KEYWORDS.get(act, act)   # 未映射的活动名直接作为关键词搜
        search_args = ((destination, kw, dest_place) if dest_place
                       else (destination, kw))
        try:
            pois = await asyncio.to_thread(amap_client.search_venues, *search_args)
        except Exception as e:
            print(f"[propose:flow] POI搜索失败({act}): {str(e)[:80]}")
            return []
        ranked = _rank_pois(pois, near)
        far = 0
        for p in ranked:
            d = amap_client._distance_km(p.location, near)
            if d is not None and d > VENUE_NEAR_KM:
                far += 1
        if far:
            print(f"[propose:flow] 「{act}」{len(ranked)}个候选中有{far}个距目的地"
                  f"超过{VENUE_NEAR_KM:.0f}km, 已整体降级到候选尾部")
        return [{"name": p.name,
                 "location": f"{p.location.longitude},{p.location.latitude}",
                 "address": p.address, "rating": p.rating, "cost": p.cost,
                 "activity": act}
                for p in ranked[:POIS_PER_ACTIVITY]]

    activity_pois: List[dict] = []
    if destination and votes:
        top_acts = list(votes.keys())[:MAX_FLOW_POI_SEARCH]
        groups = await asyncio.gather(*[_search_act(a) for a in top_acts])
        activity_pois = [p for g in groups for p in g]
        print(f"[propose:flow] 活动地点候选 {len(activity_pois)}个({', '.join(top_acts)})")

    prompt = build_flow_prompt(
        votes,
        p.fitness_distribution,
        p.activity_type,
        p.date,
        date_end=p.date_end,
        # 给人看的展示名(带行政归属): 这在 prompt 里是"流程发生在哪儿"的说明,
        # 与上面的检索口径(destination 原话)是两回事, 各取 `_Destination` 的一面
        destination=dest_canon.get("display") or destination,
        activity_pois=activity_pois,
        conflict_report=p.conflict_report,
        user_feedback=p.user_feedback,
    )
    prop, s, e = await _invoke_proposal("flow", prompt, "flow")
    prop = _attach_route_segments(prop, origin_location)
    return {
        "proposals": {"flow": prop},
        "steps_log": [f"[propose:flow] {s.strftime('%H:%M:%S.%f')}~{e.strftime('%H:%M:%S.%f')} "
                      f"合计{prop['total_cost']}元, 地点候选{len(activity_pois)}个, "
                      f"转场{len(prop.get('route_segments') or [])}段"],
    }


def _attach_route_segments(prop: dict, origin_location: Optional[dict]) -> dict:
    """流程提案定稿后程序化拼接逐段转场路线(纯工具零LLM, 高德缓存)

    起点=出发地坐标(缺省直接从第一个有坐标的活动算起), 相邻有坐标的活动
    两两调高德 direction(driving), 结果写 prop["route_segments"]; 查不到的段跳过。
    """
    items = prop.get("items") or []
    located = [(it.get("location") or {}).get("location") for it in items]
    pairs = [(it, loc) for it, loc in zip(items, located) if loc]
    if not pairs:
        return prop

    segments: List[str] = []
    prev_coord: Optional[str] = None
    prev_name = "出发地"
    if origin_location and origin_location.get("longitude"):
        prev_coord = f"{origin_location['longitude']},{origin_location['latitude']}"
    for it, loc in pairs:
        cur_name = (it.get("location") or {}).get("name") or it.get("name") or "活动地点"
        if prev_coord:
            try:
                from ..models.schemas import Location as _Loc
                route = amap_client.get_route_info(
                    _Loc(longitude=float(prev_coord.split(",")[0]),
                         latitude=float(prev_coord.split(",")[1])),
                    _Loc(longitude=float(loc.split(",")[0]),
                         latitude=float(loc.split(",")[1])),
                    "driving",
                )
            except Exception as e:
                print(f"[propose:flow] 转场路线查询失败({prev_name}->{cur_name}): {str(e)[:80]}")
                route = None
            if route:
                segments.append(
                    f"{prev_name}→{cur_name}: 驾车约{route.duration / 60:.0f}分钟"
                    f"({route.distance / 1000:.1f}km)"
                )
        prev_coord = loc
        prev_name = cur_name
    prop["route_segments"] = segments
    if segments:
        print(f"[propose:flow] 逐段转场{len(segments)}段: {segments[0]}")
    return prop


def _make_food_search_tool(venue_location: Location):
    """构造 food 路的受控搜索工具: 位置/半径由系统锁定, LLM 只能传关键词"""
    from langchain_core.tools import tool

    @tool
    def search_nearby_food(keywords: str = "餐厅") -> str:
        """搜索活动场地附近的餐厅(受控版: 只能指定关键词, 如 餐厅/清真/火锅; 位置与半径由系统锁定)

        返回 {"status": ..., "results": [...]}:
          - status=ok       -> results 就是候选餐厅
          - status=empty    -> 搜索成功但周边确实没有匹配的餐厅, 这是**正常结果**
          - status=degraded -> 搜索本身失败了(网络/限流等), **不代表附近没有餐厅**,
                               可换个关键词再试, 或按常识估算并在报价里注明
        """
        try:
            results = amap_client.search_nearby(venue_location, radius=3000, keywords=keywords)
        except AppError as e:
            # 只把"我知道怎么处理"的失败变成有状态的返回值; 其余异常照旧抛出
            # (旧实现这里是裸调用, 一抛就把整个餐饮路打挂, 且模型分不清
            #  "周边没餐厅"和"搜索挂了" —— 一个是业务结果, 一个是故障)
            print(f"[propose:food] 周边搜索失败({keywords}): {str(e)[:100]}")
            return json.dumps({
                "status": "degraded",
                "results": [],
                "note": (f"搜索失败({e.user_message}), 这不代表附近没有餐厅; "
                         f"可换关键词再试, 或按常识估算人均并注明'待核实'"),
            }, ensure_ascii=False)

        # 高德周边搜默认按距离序, 先程序化按评分排序再交给 LLM(头部是高分店)
        ranked = _rank_pois(results)
        return json.dumps({
            "status": "ok" if ranked else "empty",
            "results": [{"name": p.name, "address": p.address, "type": p.type,
                         "rating": p.rating, "cost": p.cost} for p in ranked],
            "note": "" if ranked else "周边3公里内没有匹配的餐厅(正常结果, 可换关键词)",
        }, ensure_ascii=False)

    return search_nearby_food


def _fallback_food_proposal(
    restaurants: List[dict],
    dietary_all: List[str],
    people_count: int,
    date: Optional[str],
    date_end: Optional[str],
    budget: Optional[float] = None,
) -> dict:
    """餐饮路兜底: LLM 没在轮次内给出 Proposal 时, 用已搜到的真实餐厅程序化组报价

    设计原则与其余三路一致 —— 宁可明确标注"程序化兜底", 也不给空提案:
    空提案会让四路合计少算全部餐费, 却仍能通过 reconcile 的预算校验。
    餐厅名/人均全部取自高德周边搜索结果, 缺失人均时按市场常识估算并注明。
    候选先按 评分降序/人均升序 排过序, 预算吃紧时改为按人均升序取最省的几家。
    """
    uniq: List[dict] = []
    seen = set()
    for r in restaurants or []:
        name = str((r or {}).get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        uniq.append(r)

    if not uniq or not people_count:
        return {
            "items": [],
            "description": ("餐饮提案生成失败: 搜索工具轮次用尽且无可用餐厅数据, "
                            "需人工补充餐饮安排(此项为 0 元不代表真的没有餐费)"),
        }

    def _rate(rec: dict) -> float:
        try:
            return float(rec.get("rating"))
        except (TypeError, ValueError):
            return 0.0

    def _cost(rec: dict) -> float:
        try:
            value = float(rec.get("cost"))
            return value if value > 0 else 60.0
        except (TypeError, ValueError):
            return 60.0

    days = _date_range(date, date_end) or ([date] if date else [])
    meal_plan = ([("当天", "午餐"), ("当天", "晚餐")] if len(days) <= 1
                 else [(f"D{i + 1}", m) for i in range(len(days))
                       for m in ("早餐", "午餐", "晚餐")])

    # 默认按 评分降序+人均升序(头部是高分店); 预算吃紧时换成最省的几家, 避免兜底把预算吃光
    uniq.sort(key=lambda r: (-_rate(r), _cost(r)))
    estimated = sum(_cost(uniq[(i * 2) % len(uniq)]) * people_count
                    for i in range(len(meal_plan)))
    tight = budget is not None and estimated > float(budget)
    if tight:
        uniq.sort(key=lambda r: (_cost(r), -_rate(r)))
        print(f"[propose:food] 兜底预估{estimated:.0f}元 超预算{budget:.0f}元, 改取最省候选")

    def _per_capita(rec: dict, meal_type: str) -> float:
        """按餐次折算人均: 早餐远低于正餐(直接拿正餐人均×人数会让早餐占掉大半预算)"""
        base = _cost(rec)
        if meal_type == "早餐":
            return max(15.0, round(base * 0.3, 2))
        return base

    items: List[dict] = []
    meals: List[dict] = []
    for idx, (day, meal_type) in enumerate(meal_plan):
        picks = [uniq[(idx * 2 + k) % len(uniq)] for k in range(min(2, len(uniq)))]
        options = []
        for rec in picks:
            per = _per_capita(rec, meal_type)
            note = (f"人均{per:.0f}元(平台数据按{meal_type}折算)" if rec.get("cost")
                    else f"人均{per:.0f}元(估算)")
            options.append({
                "name": str(rec.get("name")),
                "est_cost": round(per * people_count, 2),
                "note": f"{note}, 评分{rec.get('rating') or '未知'}; 类型{rec.get('type') or '餐饮'}",
                "reason": "取自高德场地周边搜索的真实餐厅(程序化兜底, 未经 LLM 二次筛选)",
            })
        meals.append({"day": day, "meal_type": meal_type, "options": options})
        best = picks[0]
        per = _per_capita(best, meal_type)
        items.append({
            "name": f"{day}-{meal_type} {best.get('name')}({people_count}人×{per:.0f}元)",
            "cost": round(per * people_count, 2),
            "note": (f"程序化兜底: 取自高德周边搜索结果; "
                     f"人均按{meal_type}口径{'平台数据' if best.get('cost') else '估算'}"
                     + ("(早餐按正餐人均的 30% 折算)" if meal_type == "早餐" else "")),
        })

    diet_note = f", 已按忌口({'/'.join(dietary_all)})筛选候选" if dietary_all else ""
    total = round(sum(i["cost"] for i in items), 2)
    budget_note = ""
    if budget is not None:
        budget_note = (f"; 合计约{total:.0f}元 vs 预算{budget:.0f}元 "
                       + ("(已按预算取最省候选)" if tight else "(在预算内)" if total <= float(budget)
                          else "(超预算, 待人工压缩)"))
    return {
        "items": items,
        "meals": meals,
        "options": [],
        "description": (f"⚠️ 餐饮提案由程序化兜底生成(LLM 未在 {MAX_FOOD_TOOL_ROUNDS} 轮/预算内给出可用方案): "
                        f"按 {'/'.join(sorted({d for d, _ in meal_plan}))} 共 {len(items)} 顿, "
                        f"每顿从场地周边真实餐厅中取候选{diet_note}{budget_note}; 建议人工复核后再执行"),
    }


@_proposal_node("food", _FoodPayload)
async def propose_food(p: _FoodPayload) -> Dict[str, Any]:
    """餐饮提案: 唯一绑定受控高德搜索工具的一路, 可自主查场地附近餐厅

    迷你 agent 循环: bind_tools([search_tool, Proposal], tool_choice='required'),
    LLM 先调搜索工具补数据, 再输出 Proposal; 最多 MAX_FOOD_TOOL_ROUNDS 轮。

    两个历史 bug 的修复:
    1. date_end 没透传 -> prompt 永远写"单日(午餐+晚餐)", 3 天行程只报 1 天餐费;
    2. 循环用尽仍只调搜索工具时静默产出空提案(实测同一 payload 连跑 6 次有 5 次
       为空, 且 reconcile 不拦)。现在最后一轮改绑"只给 Proposal"的强约束,
       并用已经搜到的餐厅数据兜底组一份可用的餐饮明细。

    餐饮是四路里**唯一不直接挑地名**的一路: 它的位置来自场地候选(venues[0])。
    场地没定下来时它原先连"在哪个城市吃饭"都不知道(prompt 只有"活动场地: 未定"),
    现在至少拿到目的地展示名 —— 四路终于认的是同一个地方(见 _Destination)。
    """
    venues = p.venues
    venue = venues[0] if venues else None
    dietary_all = p.dietary_all
    people_count = p.people_count
    budget = p.budget
    date_end = p.date_end
    date = p.date
    guides = p.guides
    conflict_report = p.conflict_report
    user_feedback = p.user_feedback
    destination = p.destination.display or p.destination.raw

    start = datetime.now()
    print(f"[propose:food] LLM 开始 {start.strftime('%H:%M:%S.%f')}")

    # 累积搜索到的餐厅, 供"轮次用尽"时程序化兜底(不丢已花掉的搜索结果)
    found_restaurants: List[dict] = []

    if venue and venue.get("location"):
        food_tool = _make_food_search_tool(Location(**venue["location"]))
        messages = [{"role": "user",
                     "content": build_food_prompt(venue, dietary_all, people_count, budget,
                                                  guides=guides,
                                                  date_end=date_end,
                                                  destination=destination,
                                                  conflict_report=conflict_report,
                                                  user_feedback=user_feedback)}]
        raw = None
        route_deadline = time.monotonic() + FOOD_TOTAL_BUDGET
        for round_i in range(MAX_FOOD_TOOL_ROUNDS):
            remaining = route_deadline - time.monotonic()
            if remaining <= 0:
                print(f"[propose:food] 路线总预算({FOOD_TOTAL_BUDGET:.0f}s)已用完, "
                      f"停止继续轮次(已跑 {round_i} 轮)")
                break
            last_round = round_i == MAX_FOOD_TOOL_ROUNDS - 1
            if last_round:
                # 最后一轮不给搜索工具: 逼模型直接产出 Proposal(否则整路白跑)
                print("[propose:food] 最后一轮, 只绑定 Proposal 强制收敛")
                llm = get_chat_model(0.3, disable_thinking=True).bind_tools(
                    [Proposal], tool_choice="required"
                )
            else:
                llm = get_chat_model(0.3, disable_thinking=True).bind_tools(
                    [food_tool, Proposal], tool_choice="required"
                )
            # 与其余三路走**同一套纪律**(invoke_llm -> call_external): 次数上限 +
            # 退避 + 时长预算。旧实现是裸 llm.invoke, 唯一的重试来自编程接口自带的
            # 两次, 而那两次**在项目预算之外**。四路里轮次最多的一路, 恰好是唯一
            # 没有纪律的一路。
            call = await asyncio.to_thread(invoke_llm, llm, messages, remaining)
            if not call.usable:
                # 网络层已按纪律放弃 -> 跳出循环走下面的程序化兜底(用已经搜到的
                # 餐厅组一份可用的报价)。兜底路径本来就在, 只是以前走不到这里。
                print(f"[propose:food] 网络层放弃(已试 {call.attempts} 次): "
                      f"{str(call.error or call.reason)[:120]}")
                break
            resp = call.value
            calls = resp.tool_calls or []
            if not calls:
                # 模型输出不合法(明明 tool_choice=required 却没调工具): 属于
                # 「Schema/格式错 -> 回填可修正信息让模型重填」那一类, 不该把整路打挂。
                # 回填一轮; 若已经是最后一轮, continue 会让循环自然结束 ->
                # raw 仍为 None -> 走下面的程序化兜底, 而不是抛 500。
                print(f"[propose:food] round{round_i + 1} 未返回 tool_calls, 回填指令重试")
                messages.append(resp)
                messages.append({"role": "user",
                                 "content": "上一次回复没有调用工具。请直接调用工具并填写全部必填参数。"})
                continue

            # Proposal 优先: 出现即收货(LLM 可能一次返回多个 tool_calls)
            proposal_calls = [c for c in calls if c["name"] == "Proposal"]
            if proposal_calls:
                try:
                    # **当场校验**: 与其余三路(invoke_structured 收尾处 schema(**args))
                    # 同一个口径。旧实现是 raw = args 直接收下, 一路拖到循环外
                    # _finalize_proposal 重建 Proposal 时才炸 —— 那时已经出不了循环,
                    # 只能整路失败, 白白浪费模型给的其他字段。
                    raw = Proposal(**proposal_calls[0]["args"]).model_dump()
                    break
                except ValidationError as e:
                    # 参数过不了校验 -> 与结构化输出同一条处置: 把"哪个字段、收到
                    # 什么"回给模型重填。消息里 Proposal 带着 tool_calls, 按协议必须
                    # 紧跟一条对应 id 的 ToolMessage, 否则部分端点直接 400。
                    print(f"[propose:food] round{round_i + 1} Proposal 参数校验失败, "
                          f"已回填修正指令: {str(e)[:120]}")
                    messages.append(resp)
                    messages.append(ToolMessage(content=schema_fix_instruction(e),
                                                tool_call_id=proposal_calls[0].get("id")))
                    continue

            if last_round:
                # 兜底: 即使强制只绑 Proposal, 模型仍可能吐一个别的工具名;
                # 只要参数形态像 Proposal 就收下来, 比整路丢掉强。
                salvage = [c for c in calls if isinstance(c.get("args"), dict)
                           and ("items" in c["args"] or "description" in c["args"])]
                if salvage:
                    try:
                        raw = Proposal(**salvage[0]["args"]).model_dump()
                    except ValidationError as e:
                        # 兜底收下的参数也过不了校验 -> 当作没收到, 交给循环外的
                        # 程序化兜底(它不依赖模型输出, 只用真实餐厅数据)
                        print(f"[propose:food] 最后一轮收下的参数仍不合规"
                              f"({str(e)[:100]}), 转程序化兜底")
                    else:
                        print(f"[propose:food] 最后一轮返回 {calls[0]['name']} "
                              f"但参数可用, 按 Proposal 收下")
                    break
                print(f"[propose:food] 最后一轮仍未返回 Proposal(实际: "
                      f"{[c['name'] for c in calls]}), 跳出循环走兜底")
                break

            # 受控搜索: 逐个执行本条消息里的全部工具调用(缺一个都会 400),
            # 白名单外的一律拒绝, 不放行
            print(f"[propose:food] 工具调用 round{round_i + 1}: "
                  f"{[c['name'] for c in calls]}")
            messages.append(resp)
            # 变量名用 tc(tool call)而不是 call: 上面那个 call 是本次模型往返的
            # CallResult, 同名会把它盖掉 —— 后面若有人接一句 call.attempts 就会
            # 拿到一个 dict, 报出的错和真正的错完全无关
            for tc in calls:
                if tc["name"] != "search_nearby_food":
                    result = json.dumps({"error": f"未知工具 {tc['name']} 被拒绝"},
                                        ensure_ascii=False)
                else:
                    try:
                        result = await asyncio.to_thread(food_tool.invoke, tc["args"])
                    except ValidationError as e:
                        # 参数格式错(如把 keywords 写成数组): 正是"Schema 拦截 +
                        # 把可修正的信息交给模型重填"那一类, 不该打挂整路
                        print(f"[propose:food] 工具参数不合法: {str(e)[:120]}")
                        result = json.dumps(
                            {"error": f"参数不合法: {str(e)[:200]}。请按工具定义的参数名与类型重新调用。"},
                            ensure_ascii=False)
                    else:
                        # json.loads 失败只可能是我们自己工具的 bug —— 不再像旧实现
                        # 那样 `except Exception: pass` 静默丢掉, 让它冒出来
                        payload_out = json.loads(result)
                        if isinstance(payload_out, dict):
                            found_restaurants.extend(payload_out.get("results") or [])
                        else:                      # 兼容旧形状(裸列表)
                            found_restaurants.extend(payload_out)
                messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
    else:
        # 场地未定: 无搜索工具, 纯推理按忌口约束估算。这条路没有工具循环,
        # 直接复用 invoke_structured —— 它自带「缺 tool_calls / 参数不合规」的
        # 回填重试, 比裸取 resp.tool_calls[0](模型不调工具就 IndexError)稳。
        prompt = build_food_prompt(None, dietary_all, people_count, budget,
                                   guides=guides, date_end=date_end,
                                   destination=destination,
                                   conflict_report=conflict_report,
                                   user_feedback=user_feedback)
        raw = (await asyncio.to_thread(invoke_structured, Proposal, prompt)).model_dump()

    end = datetime.now()
    print(f"[propose:food] LLM 结束 {end.strftime('%H:%M:%S.%f')} "
          f"(耗时 {(end - start).total_seconds():.2f}s)")

    if raw is None:
        raw = _fallback_food_proposal(found_restaurants, dietary_all, people_count,
                                      date, date_end, budget)
        print(f"[propose:food] 未在轮次/预算内拿到合规 Proposal -> 程序化兜底: "
              f"{len(raw.get('items') or [])} 条明细"
              + ("(无可用餐厅数据, 仅给出说明)" if not found_restaurants else ""))
    prop = _finalize_proposal(raw, "food")
    return {
        "proposals": {"food": prop},
        "steps_log": [f"[propose:food] {start.strftime('%H:%M:%S.%f')}~{end.strftime('%H:%M:%S.%f')} "
                      f"合计{prop['total_cost']}元"],
    }


# ============ 约束消解(reconcile, 纯程序零LLM) ============

# 时间段解析: "09:00-12:00" / "9点-12点" / "09:00~11:30" / "13:30至15:00"
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2})(?:[:：](\d{2}))?\s*[-–—~至到]\s*(\d{1,2})(?:[:：](\d{2}))?"
)
# 日期形态: 判断某个无冒号匹配是不是"月-日"(如 "10-18"), 而不是时间段
def _looks_like_date_match(match: "re.Match", text: str) -> bool:
    """判断一个时间正则匹配其实是日期片段

    - 带冒号("09:00-12:00")一定是时间段;
    - 无冒号且两段都在 月(1-12)-日(1-31) 范围内("10-18"/"12-25")按日期处理。
      取舍说明: 流程 item 的时段由 prompt 强制写成 "HH:MM-HH:MM" 形式, 因此
      无冒号的 "a-b" 更可能是日期; 宁可漏检一个不规范的 "9-12" 时段, 也不要
      把日期当成时段而误报重叠。
    """
    fragment = match.group(0)
    if ":" in fragment or "：" in fragment:
        return False
    try:
        left, right = int(match.group(1)), int(match.group(3))
    except (TypeError, ValueError):
        return False
    return 1 <= left <= 12 and 1 <= right <= 31


# 保留以便外部复用/测试(实际解析走 extract_time_ranges, 见其 docstring)
_DATE_LIKE_RE = re.compile(r"(?:19|20)\d{2}[-/年]\d{1,2}|^\d{1,2}[-/月]\d{1,2}$")
# 容量声明: "容纳30人" / "可容纳 50 人"
_CAPACITY_RE = re.compile(r"容纳\s*(\d+)\s*人")
# 天次前缀: "D1 09:00-12:00 爬山" / "第2天 上午聚餐"
_DAY_PREFIX_RE = re.compile(r"D(\d+)|第([一二三四五六七八九十\d]+)天")
_CN_DAY_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def extract_time_ranges(text: str) -> List[Tuple[int, int, str]]:
    """从一段文本里抽出所有合法时间段 -> [(开始分钟, 结束分钟, 原始片段)]

    必须用 finditer 遍历**全部**匹配, 且跳过日期片段:
    - 只取 search() 首个匹配时, "2026-10-18 09:00-12:00 爬山" 的首个匹配是
      "26-10"(被当成 26:00-10:00 直接丢弃), 后面的真实时段永远检测不到;
    - "10-18 09:00-12:00" 更糟: 首个匹配 "10-18" 被当成 10:00-18:00 的假时段。
    因此逐匹配判断是否为日期形态(见 _looks_like_date_match), 并要求 start < end。
    """
    results: List[Tuple[int, int, str]] = []
    for m in _TIME_RANGE_RE.finditer(text or ""):
        if _looks_like_date_match(m, text):
            continue
        start = int(m.group(1)) * 60 + int(m.group(2) or 0)
        end = int(m.group(3)) * 60 + int(m.group(4) or 0)
        if end > start:
            results.append((start, end, m.group(0)))
    return results


def _item_day(text: str) -> str:
    """从 item 文本提取天次 key('D1'/'D2'…; 识别不出返回 '' 归入同组)"""
    m = _DAY_PREFIX_RE.search(text)
    if not m:
        return ""
    if m.group(1):
        return f"D{m.group(1)}"
    cn = m.group(2)
    if cn in _CN_DAY_NUM:
        return f"D{_CN_DAY_NUM[cn]}"
    try:
        return f"D{int(cn)}"
    except ValueError:
        return ""


def _budget_check(proposals: dict, budget: Optional[float]) -> Tuple[float, List[str], List[str]]:
    """四路报价加总 vs 预算比对; 超支时给出带具体数字的冲突与降级建议"""
    totals = {
        k: float((proposals.get(k) or {}).get("total_cost") or 0)
        for k in PROPOSAL_ORDER if k in proposals
    }
    grand_total = round(sum(totals.values()), 2)
    conflicts: List[str] = []
    suggestions: List[str] = []

    over = 0.0
    if budget is not None:
        over = round(grand_total - float(budget), 2)
        if over > 0:
            breakdown = " + ".join(
                f"{CATEGORY_NAMES.get(k, k)}{v:.0f}元"
                for k, v in sorted(totals.items(), key=lambda x: -x[1])
            )
            conflicts.append(
                f"超支{over:.0f}元: 四路合计{grand_total:.0f}元 vs 预算{budget:.0f}元 ({breakdown})"
            )
            # 降级空间: 占比最高的类别压到多少可回到预算内(具体数字, 不是笼统"降级")
            biggest_k, biggest_v = max(totals.items(), key=lambda x: x[1])
            share = (biggest_v / grand_total * 100) if grand_total else 0.0
            target = max(0.0, round(biggest_v - over, 2))  # 超支超过该类别总额时钳到 0
            if biggest_v >= over:  # 该类别降级空间足以覆盖超支(压到0也算降级到位)
                suggestions.append(
                    f"{CATEGORY_NAMES.get(biggest_k, biggest_k)} {biggest_v:.0f}元占 {share:.0f}%, "
                    f"可降级空间约{over:.0f}元: 压到 {target:.0f}元 以内即可回到预算"
                )
            else:
                suggestions.append(
                    f"超支{over:.0f}元已超过最大单项({CATEGORY_NAMES.get(biggest_k, biggest_k)} "
                    f"{biggest_v:.0f}元), 单靠降级无法回预算, 需砍项或提高预算"
                )
    return over, conflicts, suggestions


def _detect_timeline_overlap(flow_prop: dict) -> List[str]:
    """flow 方案内各时段重叠检测: 解析 item 里的 'HH:MM-HH:MM' 时间段, 按天分组两两比对

    - item 的 name/note 带 D1/第2天 前缀时归入对应天次组, 只比对同组内的时段
      (多日活动跨天同时段不算重叠); 识别不出天次的归入 '' 组。
    - 解析不出时间段的项跳过(无法程序判定时不妄下结论)。
    """
    if not flow_prop:
        return []
    ranges: Dict[str, List[Tuple[int, int, str]]] = {}
    for it in flow_prop.get("items") or []:
        text = f"{it.get('name') or ''} {it.get('note') or ''}"
        # 用 extract_time_ranges 抽全部时段并跳过日期片段(见该函数 docstring:
        # 只取首个匹配会被 "2026-10-18" 这类日期截胡, 导致重叠漏报/误报)
        for start, end, _ in extract_time_ranges(text):
            day = _item_day(text)
            ranges.setdefault(day, []).append((start, end, it.get("name") or "未命名时段"))

    def _fmt(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    conflicts = []
    for day, day_ranges in ranges.items():
        for i in range(len(day_ranges)):
            for j in range(i + 1, len(day_ranges)):
                s1, e1, n1 = day_ranges[i]
                s2, e2, n2 = day_ranges[j]
                if s1 < e2 and s2 < e1:  # 半开区间相交即重叠
                    day_label = f"({day})" if day else ""
                    conflicts.append(
                        f"时间线重叠{day_label}: 「{n1}」({_fmt(s1)}-{_fmt(e1)}) 与 "
                        f"「{n2}」({_fmt(s2)}-{_fmt(e2)})"
                    )
    return conflicts


def _detect_capacity_conflict(venue_prop: dict, people_count: int) -> List[str]:
    """场地容量 >= 人数检查: 从 venue 提案的 description/note 中解析容量声明"""
    if not venue_prop or not people_count:
        return []
    texts = [venue_prop.get("description") or ""]
    texts += [it.get("note") or "" for it in venue_prop.get("items") or []]
    for text in texts:
        m = _CAPACITY_RE.search(text)
        if not m:
            continue
        capacity = int(m.group(1))
        if capacity < people_count:
            return [
                f"场地容量不足: 声明容纳{capacity}人 < 参与人数{people_count}人, "
                f"需更换可容纳{people_count}人以上的场地"
            ]
        return []  # 以第一处容量声明为准, 检查通过
    return []  # 无容量声明, 交由容量建议环节(LLM 侧已要求标注推断)


def _detect_transport_mismatch(transport_prop: dict, people_count: int) -> List[str]:
    """交通方式 vs 人数匹配检查(纯关键词规则, 与 prompt 候选清单同一套阈值)

    1. 小团推大巴: 提案含"大巴"但人数 < BUS_MIN_PEOPLE(10) -> 冲突
    2. 大团纯自驾: 人数 > 30 且不含"大巴/中巴/高铁/火车" -> 冲突(协调成本+安全)
    解析不出方式关键词时跳过(不妄下结论)。
    """
    if not transport_prop or not people_count:
        return []
    texts = [f"{it.get('name') or ''} {it.get('note') or ''}"
             for it in transport_prop.get("items") or []]
    texts += [o.get("name") or "" for o in transport_prop.get("options") or []]
    joined = " ".join(texts)

    if "大巴" in joined and people_count < BUS_MIN_PEOPLE:
        return [
            f"交通方式不匹配: {people_count}人小团推荐了大巴包车(人数<{BUS_MIN_PEOPLE}), "
            f"应改用 自驾拼车/打车/包小车 等更经济的方式"
        ]
    big_group_modes = ("大巴", "中巴", "高铁", "火车", "飞机")
    if people_count > 30 and not any(m in joined for m in big_group_modes):
        return [
            f"交通方式不匹配: {people_count}人大团仅推荐自驾拼车, 协调车数多且疲劳驾驶风险高, "
            f"应补充 大巴/中巴包车 或 高铁/火车 等集中出行方式"
        ]
    return []


def _replan_targets(report: dict) -> Optional[set]:
    """这次回炉该重派哪几路: None = 四路全发

    回炉原先一律四路全跑。但冲突大多有明确归属(时间线重叠只跟流程有关, 容量只跟
    场地有关, 交通方式只跟交通有关), 让不相关的那几路跟着重跑一遍不只是浪费钱:
    它们会**重新写一份本来没问题的提案**, 而且很可能写出与原来不一样的东西
    (同一个 prompt 跑两次结果不同), 把一个已经被人工看过/权衡过的方案换掉。

    规则: 任何一块归属信息缺失就退回"四路全发" —— 预算超支是全局的(要四路一起
    让利才能回到预算内), 归属缺失(老报告/手工构造的报告)也无法判定谁该修。
    宁可多跑, 不可漏修。
    """
    if (report.get("over_budget") or 0) > 0:
        return None
    owners_of = report.get("conflicts_by_category") or {}
    if not owners_of:
        return None
    owners = {k for k, texts in owners_of.items() if texts}
    return owners or None


def reconcile(state: EventPlanState) -> Dict[str, Any]:
    """约束消解节点: 纯程序零 LLM, 四项校验

    1. 预算: 四路 Proposal 报价加总 vs parsed_request.budget
    2. 时间线: flow 方案内各时段重叠检测
    3. 容量: 场地容量声明 >= 参与人数
    4. 交通方式 vs 人数: 小团推大巴/大团纯自驾 -> 打回

    每条冲突都带**归属**(哪一路该修它), 汇成 ConflictReport.conflicts_by_category; 归属不到
    具体某一路的(预算)即为全局冲突。回炉据此定向重派(route_after_reconcile),
    每路的 prompt 也只看到与自己相关的冲突(build_conflict_feedback)。

    产出 ConflictReport(带具体数字); replan_count 检出问题即自增,
    条件边 route_after_reconcile 依据它决定回炉或放行。
    """
    proposals = state.get("proposals") or {}
    parsed = state.get("parsed_request") or {}
    people_count = parsed.get("people_count") or 0
    budget = parsed.get("budget")

    # (冲突原文, 归属类别) —— 归属为空元组 = 全局冲突(每一路都该看)
    owned: List[Tuple[str, Tuple[str, ...]]] = []
    suggestions: List[str] = []
    over_budget = 0.0

    if proposals:
        over_budget, budget_conflicts, budget_suggestions = _budget_check(proposals, budget)
        # 超支是**全局**的: 谁都能让一点, 单压某一路未必够 -> 归属为空
        owned += [(t, ()) for t in budget_conflicts]
        suggestions += budget_suggestions

        venue_prop = proposals.get("venue") or {}
        flow_prop = proposals.get("flow") or {}

        # 空明细检查: 原来只查 venue, 于是"餐饮提案 0 元空壳"能直接通过预算校验,
        # 四路合计少算全部餐费却显示"校验通过"。这里对四路统一拦截, 且**逐路成条**
        # (旧版把空的几路拼成一行)—— 归属要精确到路, 才能只把坏的那一路送回炉。
        for k in PROPOSAL_ORDER:
            if proposals.get(k) and not (proposals.get(k) or {}).get("items"):
                owned.append((
                    f"{CATEGORY_NAMES.get(k, k)} 提案无有效报价(空明细), "
                    f"四路合计会漏算该部分费用, 需重新生成或人工补齐", (k,)))
        owned += [(t, ("flow",)) for t in _detect_timeline_overlap(flow_prop)]
        owned += [(t, ("venue",)) for t in _detect_capacity_conflict(venue_prop, people_count)]
        owned += [(t, ("transport",))
                  for t in _detect_transport_mismatch(proposals.get("transport") or {},
                                                      people_count)]

    conflicts = [text for text, _ in owned]
    owners_of: Dict[str, List[str]] = {}
    for text, cats in owned:
        for cat in cats:
            owners_of.setdefault(cat, []).append(text)

    has_issue = (over_budget > 0) or bool(conflicts)
    replan = state.get("replan_count") or 0
    report = ConflictReport(over_budget=over_budget, conflicts=conflicts,
                            suggestions=suggestions, conflicts_by_category=owners_of)

    if has_issue:
        # 检出问题即自增: 路由按 replan_count <= MAX_REPLAN 决定是否回炉
        new_count = replan + 1
        if new_count <= MAX_REPLAN:
            log = (f"[reconcile] 第{new_count}次校验未通过(超支{over_budget:.0f}元/"
                   f"冲突{len(conflicts)}条), 回炉重生成(ConflictReport 已注入 prompt)")
        else:
            log = (f"[reconcile] 第{new_count}次校验仍未通过(超支{over_budget:.0f}元/"
                   f"冲突{len(conflicts)}条), 已达回炉上限{MAX_REPLAN}, "
                   "不再回炉, 带冲突交人工裁决(HITL)")
        print(f"[reconcile] {log}")
        if suggestions:
            print(f"[reconcile] 建议: {suggestions}")
        return {
            "conflict_report": report.model_dump(),
            "replan_count": new_count,
            "steps_log": [log],
        }

    total = sum(float((proposals.get(k) or {}).get("total_cost") or 0) for k in proposals)
    log = (f"[reconcile] 校验通过: 四路合计{total:.0f}元"
           f"(预算{budget:.0f}元)" if budget is not None else
           f"[reconcile] 校验通过: 四路合计{total:.0f}元(未提供预算)")
    print(f"[reconcile] {log}")
    return {
        "conflict_report": report.model_dump(),
        "replan_count": replan,
        "steps_log": [log],
    }


def route_after_reconcile(state: EventPlanState) -> Any:
    """reconcile 之后的条件边(沿用原 route_after_validate 模式)

    - 未通过 且 replan_count <= MAX_REPLAN -> 返回 List[Send] 重新派发
      (payload 已带 conflict_report, prompt 构造器转为带具体数字的修改指令)。
      派哪几路由 _replan_targets 定: 冲突有明确归属时**只派相关的那几路**,
      全局冲突(预算)或没有归属信息时才四路全发。
    - 通过 / replan_count > MAX_REPLAN -> 返回 "risk", 进入风险评估节点
      (带冲突交人工裁决的方案同样过一遍风险标记, 信息更全)
    """
    report = state.get("conflict_report") or {}
    has_issue = bool(report.get("conflicts")) or (report.get("over_budget") or 0) > 0
    if has_issue and (state.get("replan_count") or 0) <= MAX_REPLAN:
        targets = _replan_targets(report)
        if targets is not None:
            print(f"[reconcile] 定向回炉: 冲突归属为 {sorted(targets)}, "
                  f"只重派这几路(其余各路保留上一轮结果)")
        return _proposal_sends(state, only=targets)
    return "risk"


# ============ 风险评估(risk, 规则层纯Python + 条件触发LLM) ============
#
# 判定与叙述分开, 这是这一段的骨架:
#   - 判定(assess_rules 及下面这几条规则)是**纯函数**: 输入 基础信息/需求/四路提案/
#     偏好汇总, 零 LLM 零外网, 免费且确定。因此可以在任何一次方案变更后重算
#     (交付前组装、审批换选后), 不必担心成本。
#   - 叙述(带报价的备选)才是花钱的那一次 LLM 调用, 只在有标记时触发。
# 过去两者糅在同一个节点里, 于是"重算"显得很贵 —— 换选后重评、交付方案与风险
# 结论对不上这两件事就都没人做。
#
# 项目画像(强度/室内外/时段)与天气关键词表都在 lexicon 里: 它们过去是节点内的
# 两张平行表, 已漂移过一次(高尔夫在室外表里有、强度表里没有 —— 于是它算"雨天要
# 备室内", 对全队平均强度却贡献 0)。合成一张表后, 这类漂移在结构上不可能发生。

# 规则标识(标记与备选按它关联, 也是规则执行表的主键)
RULE_WEATHER = "weather_outdoor"      # 降水 + 当天有室外项目
RULE_EXTREME = "extreme_weather"      # 极端天气(不要求室外项目)
RULE_INTENSITY = "intensity_fitness"  # 强度尖峰 vs 弱体能成员
RULE_TEMP_WIND = "temperature_wind"   # 温度/风力(本期未纳入判定, 显式记为未评估)

# 严重度分档
WEAK_RATIO_HIGH = 0.25    # 弱体能占比 >= 25% 时强度冲突记 high, 否则 medium
HIGH_ITEM_INTENSITY = 4   # 单项强度达到此值就要为弱体能成员单独安排(不看全队平均)


def _flag_id(rule: str, day: Optional[str] = None) -> str:
    """标记的稳定标识: 规则 + 命中日(同一条规则每天最多一条, 故唯一)

    备选按这个 id 关联标记, 而不是回抄标记原文 —— 模型改写一个字的措辞就会让
    关联断掉, 而断掉的关联**当时不会报错**, 只是审批人看不到"这条标记没有备选"。
    """
    return f"{rule}:{day or '-'}"


def _flow_item_text(item: dict) -> str:
    """flow item 的可匹配文本(name + note 拼接)"""
    return f"{item.get('name') or ''} {item.get('note') or ''}"


def _activity_days(parsed: dict) -> List[str]:
    """活动日集合(ISO 字符串), 与采集侧同一口径

    多日活动必须覆盖到每一天。过去风险只拿活动**起始日**做等值比较, 10-18~10-19
    的活动雨落在第二天就查不出来; 又因为"匹配不到就返回无标记", 这是静默漏报,
    结论还会写成"未检出天气/强度风险"。
    """
    return _date_range(parsed.get("date"), parsed.get("date_end"))


def _day_index(parsed: dict) -> Dict[str, str]:
    """天次 key(D1/D2…) -> 活动日 ISO 日期(与 _item_day 的 key 口径对应)"""
    return {f"D{i + 1}": d for i, d in enumerate(_activity_days(parsed))}


def _weather_days(basic_info: dict, parsed: dict) -> Tuple[List[dict], List[str], List[str]]:
    """把预报切成"覆盖到活动日的"与"没覆盖到的", 返回(条目, 覆盖到的活动日, 未覆盖的活动日)

    日期缺失时(计划未定)沿用老口径: 全部预报都有参考价值, 没有"未覆盖"一说。
    未覆盖的活动日绝不拿别的日期的预报顶上 —— 那是编造。
    """
    weather = basic_info.get("weather") or []
    days = _activity_days(parsed)
    if not days:
        return weather, [w.get("date") for w in weather if w.get("date")], []
    by_date = {w.get("date"): w for w in weather}
    covered = [d for d in days if d in by_date]
    missed = [d for d in days if d not in by_date]
    return [by_date[d] for d in covered], covered, missed


def _outdoor_items(flow_prop: dict) -> List[dict]:
    """流程里的室外项目(带天次与时段), 供降水规则做逐日/分时段匹配"""
    out = []
    for it in flow_prop.get("items") or []:
        text = _flow_item_text(it)
        profile = resolve_project(text)
        if profile and profile.outdoor:
            out.append({"name": it.get("name") or "未命名项目",
                        "slot": profile.slot, "day": _item_day(text)})
    return out


def _weather_span(basic_info: dict) -> str:
    """预报实际覆盖的日期区间(展示用)"""
    weather = basic_info.get("weather") or []
    return f"{weather[0].get('date')}~{weather[-1].get('date')}" if weather else "无"


def _rule_status(rule: str, name: str, status: str, reason: str = "", detail: str = "") -> RuleStatus:
    return RuleStatus(rule=rule, name=name, status=status, reason=reason, detail=detail)


def _detect_weather_risk(basic_info: dict,
                         parsed: dict,
                         flow_prop: dict) -> Tuple[Optional[RiskFlag], RuleStatus]:
    """规则1: 活动日降水 + 当天有室外项目 -> 标记

    返回 (标记或 None, 规则执行情况) —— **一次返回两件**, 不让"标记"和"规则状态"
    各判一遍前置条件: 两个真相来源迟早会不一致(改了门槛却忘了同步状态)。

    - **逐日**匹配: 项目文案带天次(D1/D2…)时只算"那天在室外且那天有雨"; 不带
      天次时退回保守口径(任一覆盖到的活动日有雨 + 存在室外项目), 并在证据与
      文案里写明是保守判断, 而不是假装精确。
    - **夜间降水只对傍晚/通宵项目算命中**: 白天晴、夜间小雨配白天的户外项目是
      误报 —— 和"出发地的雨不能说目的地的室外项目"是同一类错误的两个面。
    - 活动日超出预报范围的部分不评估(不拿不相干日期的预报下结论); 未取到预报、
      或活动日全部超范围时本条**未评估**, 由规则执行表表达, 不是"无风险"。
    - 无室外项目则降水不构成风险 —— 室内活动不受影响, 不妄下结论。
    """
    nm = "降水+室外项目"
    if not (basic_info.get("weather") or []):
        return None, _rule_status(RULE_WEATHER, nm, "not_evaluated", reason="未取到天气预报")
    covered, covered_days, missed = _weather_days(basic_info, parsed)
    if not covered:
        return None, _rule_status(
            RULE_WEATHER, nm, "not_evaluated",
            reason=f"活动日超出预报范围(现仅覆盖 {_weather_span(basic_info)})")
    partial = f"部分活动日({'、'.join(missed)})超出预报范围, 那几天未评估" if missed else ""

    outdoor = _outdoor_items(flow_prop)
    if not outdoor:
        return None, _rule_status(RULE_WEATHER, nm, "clear", reason=partial,
                                  detail="流程无室外项目, 降水不构成风险")

    date_to_key = {v: k for k, v in _day_index(parsed).items()}
    hits: List[dict] = []
    for w in covered:
        day = w.get("date")
        precip = [k for k in PRECIP_KEYWORDS if k in (w.get("day_weather") or "")]
        night = [k for k in PRECIP_KEYWORDS if k in (w.get("night_weather") or "")]
        key = date_to_key.get(day)
        for it in outdoor:
            # 项目带天次 -> 只算它那天; 不带天次 -> 视为计划内任意一天(保守)
            if it["day"] and key is not None and it["day"] != key:
                continue
            if precip:
                hits.append({"day": day, "item": it["name"], "when": "白天",
                             "weather": w.get("day_weather"), "keyword": precip[0]})
            elif night and it["slot"] in ("evening", "any"):
                hits.append({"day": day, "item": it["name"], "when": "夜间",
                             "weather": w.get("night_weather"), "keyword": night[0]})
    if not hits:
        return None, _rule_status(
            RULE_WEATHER, nm, "clear", reason=partial,
            detail=f"{'、'.join(covered_days)} 在预报范围内, 未见降水与室外项目冲突")

    conservative = any(not it["day"] for it in outdoor)
    seen_days: Dict[str, dict] = {}
    for h in hits:
        seen_days.setdefault(h["day"], h)
    days_txt = "、".join(f"{h['day']}「{h['when']}{h['weather']}」" for h in seen_days.values())
    names = "、".join(sorted({h["item"] for h in hits}))
    detail = (f"降水+室外项目: {days_txt} 与室外项目({names})冲突, 需准备室内备选"
              + ("(项目未标天次, 按任一活动日有雨即算, 保守判断)" if conservative else ""))
    flag = RiskFlag(id=_flag_id(RULE_WEATHER, hits[0]["day"]), rule=RULE_WEATHER,
                    severity="high", detail=detail, day=hits[0]["day"],
                    evidence={"hits": hits, "conservative": conservative,
                              "covered_days": covered_days, "missed_days": missed})
    return flag, _rule_status(RULE_WEATHER, nm, "hit", reason=partial, detail=detail)


def _detect_extreme_weather(basic_info: dict,
                            parsed: dict) -> Tuple[Optional[RiskFlag], RuleStatus]:
    """规则2: 活动日预报出现极端天气 -> 标记(**不要求**流程里有室外项目)

    与降水那条前提不同是刻意的: 台风天在室内办活动同样要重问整个行程(通勤/
    返程), 拿"室内活动不受影响"放行是错的。降水那一档才需要"有室外项目"。
    """
    nm = "极端天气"
    if not (basic_info.get("weather") or []):
        return None, _rule_status(RULE_EXTREME, nm, "not_evaluated", reason="未取到天气预报")
    covered, covered_days, missed = _weather_days(basic_info, parsed)
    if not covered:
        return None, _rule_status(
            RULE_EXTREME, nm, "not_evaluated",
            reason=f"活动日超出预报范围(现仅覆盖 {_weather_span(basic_info)})")
    partial = f"部分活动日({'、'.join(missed)})超出预报范围, 那几天未评估" if missed else ""

    hits: List[dict] = []
    for w in covered:
        for field, when in (("day_weather", "白天"), ("night_weather", "夜间")):
            text = w.get(field) or ""
            matched = [k for k in EXTREME_WEATHER_KEYWORDS if k in text]
            if matched:
                hits.append({"day": w.get("date"), "when": when,
                             "weather": text, "keyword": matched[0]})
    if not hits:
        return None, _rule_status(RULE_EXTREME, nm, "clear", reason=partial,
                                  detail=f"{'、'.join(covered_days)} 在预报范围内, 未见极端天气")
    desc = "、".join(f"{h['day']}「{h['when']}{h['weather']}」" for h in hits)
    detail = f"极端天气: 预报{desc}, 建议改期, 或改室内并复核往返与通勤"
    flag = RiskFlag(id=_flag_id(RULE_EXTREME, hits[0]["day"]), rule=RULE_EXTREME,
                    severity="high", detail=detail, day=hits[0]["day"],
                    evidence={"hits": hits})
    return flag, _rule_status(RULE_EXTREME, nm, "hit", reason=partial, detail=detail)


def _high_intensity_items(flow_prop: dict) -> List[Tuple[str, Any]]:
    """流程里达到高强度门槛的项目 (名称, 画像)"""
    out: List[Tuple[str, Any]] = []
    for it in flow_prop.get("items") or []:
        profile = resolve_project(_flow_item_text(it))
        if profile and profile.intensity >= HIGH_ITEM_INTENSITY:
            out.append((it.get("name") or "未命名项目", profile))
    return out


def _detect_intensity_conflict(flow_prop: dict,
                               pref_summary: dict) -> Tuple[Optional[RiskFlag], RuleStatus]:
    """规则3: 流程里存在高强度项目 **且** 有弱体能成员 -> 标记

    判据是"单点达标 + 有人吃不消", 不是全队平均: 五项里只有一项爬山(5级)、其余
    聚餐(1级)时平均只有 1.8, 旧口径不标记 —— 但对那部分弱体能成员, 那一项就是
    实打实的风险。平均值天然抹掉尖峰, 所以平均强度只用来定严重度, 不当门槛。

    分母如实写进文案: 只统计明确填了体能的人, 并把"已填/总人数"标出来。样本小
    本来就该说出来 —— 用阈值把它藏起来是替用户做决定。解析不出强度的项目走
    unresolved_flow_items(), 列进报告而不是静默不计入。
    """
    nm = "强度 vs 体能"
    flagged = _high_intensity_items(flow_prop)
    if not flagged:
        return None, _rule_status(RULE_INTENSITY, nm, "clear",
                                  detail="流程无高强度项目, 无需分层安排")

    dist = (pref_summary or {}).get("fitness_distribution") or {}
    total = sum(int(v) for v in dist.values())
    if not total:
        return None, _rule_status(RULE_INTENSITY, nm, "not_evaluated",
                                  reason="无人填写体能, 无法比对强度")
    weak = sum(int(v) for k, v in dist.items() if str(k).isdigit() and int(k) <= 2)
    if not weak:
        return None, _rule_status(RULE_INTENSITY, nm, "clear",
                                  detail="有高强度项目, 但无弱体能成员(体能≤2级)")

    weak_ratio = weak / total
    avg = sum(p.intensity for _n, p in flagged) / len(flagged)
    severity = "high" if weak_ratio >= WEAK_RATIO_HIGH else "medium"
    names = "、".join(f"{n}({p.intensity}级)" for n, p in flagged)
    people = (pref_summary or {}).get("people_count")
    sample = f", 仅 {total}/{people} 人填写" if people and int(people) > total else ""
    # 故意不把"平均值"写进结论文案: 无论算全队的还是只算高强度项的, 摆在最前面都
    # 会被读成"团队整体强度"。要传达的是"哪几项重 + 多少人吃不消", 平均不是结论。
    detail = (f"强度不匹配: 流程含高强度项目 {names}, "
              f"但体能≤2级成员占{weak_ratio:.0%}({weak}/{total}人{sample}), "
              f"需为这部分人单独安排或降级")
    flag = RiskFlag(id=_flag_id(RULE_INTENSITY), rule=RULE_INTENSITY, severity=severity,
                    detail=detail,
                    evidence={"high_items": [n for n, _ in flagged],
                              "high_item_avg": round(avg, 2),
                              "weak_ratio": round(weak_ratio, 4), "weak": weak,
                              "fitness_filled": total, "people_count": people})
    return flag, _rule_status(RULE_INTENSITY, nm, "hit", detail=detail)


def unresolved_flow_items(flow_prop: dict) -> List[str]:
    """强度/室内外解析不出的项目名(规则的**盲区**, 要列出来给审批人看)

    解析不出不妄下结论是对的, 但"不计入"和"不存在"在结论里长得一样 —— 前者只要
    没人说就会被当成后者。列出来, 让盲区可见。
    """
    out = []
    for it in flow_prop.get("items") or []:
        text = _flow_item_text(it)
        if (it.get("name") or "").strip() and resolve_project(text) is None:
            out.append(it.get("name") or "")
    return out


def _weather_coverage_note(basic_info: dict, parsed: dict) -> str:
    """天气覆盖情况说明(不构成标记, 写进 summary 让用户知道这份结论能不能信)

    活动日超出预报范围时, 正确做法是承认"现在评估不了", 而不是拿不相关日期的
    预报去下结论。多日活动按区间逐日核对覆盖率 —— 只看第一天会把"第一天在范围
    内、第二天不在"的缺口整段藏起来, 连这句提醒都不会出现。
    """
    weather = basic_info.get("weather") or []
    days = _activity_days(parsed)
    if not weather:
        return "未取到天气预报, 天气风险未评估"
    if not days:
        return ""     # 日期未定: 全部预报都有参考价值
    _entries, covered_days, missed = _weather_days(basic_info, parsed)
    if not covered_days:
        span = f"{weather[0].get('date')}~{weather[-1].get('date')}"
        return (f"活动日 {'、'.join(days)} 全部超出预报范围(现仅覆盖 {span}), "
                f"天气风险无法评估, 请临近活动日再复核")
    if missed:
        return (f"活动日 {'、'.join(missed)} 超出预报范围"
                f"(现覆盖 {'、'.join(covered_days)}), 这些天的天气风险未评估")
    return ""


def assess_rules(basic_info: dict, parsed: dict, proposals: dict,
                 pref_summary: Optional[dict]) -> Tuple[List[RiskFlag], List[RuleStatus], List[str]]:
    """规则层判定(纯函数: 零 LLM 零外网, 免费且确定)

    返回 (标记, 每条规则的执行情况, 强度解析不出的项目名)。

    可对任意一套输入重算 —— 这是"判定与叙述分开"的落点: 交付前组装方案时要拿
    **最终**方案重算一次, 否则交付物里那段风险结论描述的是暂停时的旧方案。
    """
    flow_prop = (proposals or {}).get("flow") or {}
    pref_summary = pref_summary or {}
    flags: List[RiskFlag] = []
    rules: List[RuleStatus] = []
    for detect in (
        lambda: _detect_weather_risk(basic_info or {}, parsed or {}, flow_prop),
        lambda: _detect_extreme_weather(basic_info or {}, parsed or {}),
        lambda: _detect_intensity_conflict(flow_prop, pref_summary),
    ):
        flag, status = detect()
        rules.append(status)
        if flag:
            flags.append(flag)
    # 温度/风力本期未纳入判定: 显式记一条未评估, 而不是"不写"
    # (day_temp 缺省 0 与真实 0℃ 无法区分, 直接判低温会误报; 取值口径定了再补规则)
    rules.append(_rule_status(
        RULE_TEMP_WIND, "温度/风力", "not_evaluated",
        reason="本期未纳入判定(温度缺省值与真实0℃不可区分, 取值口径待定)"))
    return flags, rules, unresolved_flow_items(flow_prop)


def _level_of(flags: List[RiskFlag], rules: List[RuleStatus]) -> str:
    """等级: 有标记取严重度最大值; 全部规则未评估=unknown; 其余=low

    刻意**不**把"别的信号"(未解冲突/采集降级/未评估规则)并进这个字段 —— 一个值
    同时表示几件事, 读的人迟早会把它当成其中之一。那些走审批 payload 的阻断项,
    未评估的规则走上面的规则执行表。
    """
    if flags:
        return "high" if any(f.severity == "high" for f in flags) else "medium"
    if not rules or all(r.status == "not_evaluated" for r in rules):
        return "unknown"
    return "low"


def _align_alternatives(drafts: List[Any],
                        flags: List[RiskFlag]) -> Tuple[List[RiskAlternative], List[str]]:
    """把备选与标记对齐: 覆盖不到任何真实标记的丢弃, 返回未被覆盖的标记 id

    过去备选靠"回抄标记原文"关联, 不校验也不对齐, 只数非空条数 —— 三条标记一条
    备选时摘要写"已给出 1 条备选", 却没有任何地方说清哪条标记没被覆盖。
    """
    valid = {f.id for f in flags}
    kept: List[RiskAlternative] = []
    covered: set = set()
    for d in drafts:
        covers = [cid for cid in (getattr(d, "covers", None) or []) if cid in valid]
        if not covers:
            # 纯 ASCII: 这行曾在 GBK 控制台下抛 UnicodeEncodeError(带 emoji),
            # 把整条重评路径带崩 —— 诊断信息不该有能力弄坏主流程。全局的输出
            # 加固已补上(app/console.py), 这一行仍然保持 ASCII: 它是"正在丢东西"
            # 的记录, 属于最该活下来的那类日志, 不依赖任何加固生效才有保障。
            print(f"[risk] drop alternative covering no real flag: "
                  f"{str(getattr(d, 'plan', ''))[:40]}")
            continue
        covered.update(covers)
        kept.append(RiskAlternative(covers=covers, plan=d.plan, est_cost=d.est_cost))
    return kept, sorted(valid - covered)


async def assess_risk(state: EventPlanState) -> Dict[str, Any]:
    """风险评估节点: 规则层纯 Python(免费), 有标记才追加一次 LLM 备选

    规则层三条(外加一条显式未评估的温度/风力): 活动日降水 vs 当天室外项目 /
    极端天气 / 强度尖峰 vs 弱体能成员。等级由标记严重度取最大值, 全部规则未评估
    时为 unknown。每条规则的执行情况(命中/清白/未评估)整表带出, 免得"没评估"
    被读成"没事"。

    LLM 条件触发: 仅标记非空时调一次生成带报价的备选; 无标记零 LLM。

    节点是 **async**: 这里会发一次阻塞的模型调用, 而图在异步执行时对同步节点是
    原地调用、不走线程池(会把整个事件循环卡住几十秒)。会调模型的其他节点都走
    同一套"丢进线程里等"的做法。
    """
    basic = state.get("basic_info") or {}
    parsed = state.get("parsed_request") or {}
    proposals = state.get("proposals") or {}
    flow_prop = proposals.get("flow") or {}

    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}

    flags, rules, unresolved = assess_rules(basic, parsed, proposals, pref)
    level = _level_of(flags, rules)
    note = _weather_coverage_note(basic, parsed)
    gap_txt = "; " + "; ".join(f"{r.name}未评估: {r.reason}" for r in rules
                               if r.status == "not_evaluated" and r.reason)

    # ---- 无标记: 零 LLM ----
    if not flags:
        head = "未检出天气/强度风险" if level == "low" else "规则层未能评估(缺输入)"
        summary = head + gap_txt + (f"; {note}" if note else "")
        report = RiskReport(level=level, flags=[], rules=rules, alternatives=[],
                            uncovered_flags=[], unevaluated_inputs=unresolved,
                            summary=summary)
        print(f"[risk] 规则层无标记(level={level}), 零LLM" + (f" ({note})" if note else ""))
        return {"risk_report": report.model_dump(),
                "steps_log": [f"[risk] 规则层无标记({level}), 零LLM"]}

    # ---- 有标记: 一次 LLM 生成带报价备选 ----
    print(f"[risk] 规则层标记{len(flags)}条(level={level}), 调用 LLM 生成备选")
    weather_slim = [
        {"date": w.get("date"), "day_weather": w.get("day_weather"),
         "night_weather": w.get("night_weather"), "day_temp": w.get("day_temp")}
        for w in (basic.get("weather") or [])
    ]
    flow_slim = [{"name": it.get("name"), "note": it.get("note")}
                 for it in flow_prop.get("items") or []]
    prompt = build_risk_prompt(
        [{"id": f.id, "severity": f.severity, "detail": f.detail, "evidence": f.evidence}
         for f in flags],
        state.get("activity_type") or parsed.get("activity_type"),
        parsed.get("date"),
        weather_slim, flow_slim,
        pref.get("fitness_distribution") or {},
        parsed.get("people_count") or 0,
    )
    try:
        draft = await asyncio.to_thread(invoke_structured, RiskAlternatives, prompt)
        alternatives, uncovered = _align_alternatives(list(draft.alternatives), flags)
    except Exception as e:
        # 备选生成失败不阻塞主流程: 标记保留, 备选为空交人工, 并如实说清"哪条没备选"
        print(f"⚠️  [risk] 备选方案生成失败, 保留风险标记交人工: {str(e)[:120]}")
        alternatives, uncovered = [], [f.id for f in flags]

    summary = (f"规则层标记{len(flags)}条({level}), 备选{len(alternatives)}条"
               + (f", 其中{len(uncovered)}条标记暂无备选" if uncovered else "")
               + gap_txt + (f"; {note}" if note else ""))
    report = RiskReport(level=level, flags=flags, rules=rules, alternatives=alternatives,
                        uncovered_flags=uncovered, unevaluated_inputs=unresolved,
                        summary=summary)
    print(f"[risk] 风险评估完成: level={level}, 备选{len(alternatives)}条, "
          f"未被覆盖标记{len(uncovered)}条")
    for a in alternatives:
        print(f"   - {a.plan}(约{a.est_cost:.0f}元, 模型估算, 未计入合计)")
    return {"risk_report": report.model_dump(),
            "steps_log": [f"[risk] 标记{len(flags)}条({level}), 备选{len(alternatives)}条"]}


def reassess_risk(previous: Optional[dict], basic_info: dict, parsed: dict,
                  proposals: dict, pref_summary: Optional[dict]) -> dict:
    """重算规则层结论(零 LLM), 并复用仍然对得上标记的旧备选

    交付的方案必须是**最终**方案: 审批换选/重走之后若还引用暂停那一刻的风险
    结论, 就会出现"用户挑了室外场地、交付文档却说低风险"。规则层是纯函数、免费,
    所以这里直接重算; 备选按标记 id 重新对齐 —— 对得上的沿用(不重复花钱), 对不上
    的丢弃并列进 uncovered_flags。
    """
    flags, rules, unresolved = assess_rules(basic_info, parsed, proposals, pref_summary)
    level = _level_of(flags, rules)
    prev_alts = [RiskAlternative(**a) for a in ((previous or {}).get("alternatives") or [])]
    alternatives, uncovered = _align_alternatives(prev_alts, flags)
    note = _weather_coverage_note(basic_info, parsed)
    gap_txt = "; " + "; ".join(f"{r.name}未评估: {r.reason}" for r in rules
                               if r.status == "not_evaluated" and r.reason)
    if flags:
        summary = (f"规则层标记{len(flags)}条({level}), 备选{len(alternatives)}条"
                   + (f", 其中{len(uncovered)}条标记暂无备选" if uncovered else "")
                   + gap_txt + (f"; {note}" if note else ""))
    else:
        head = "未检出天气/强度风险" if level == "low" else "规则层未能评估(缺输入)"
        summary = head + gap_txt + (f"; {note}" if note else "")
    return RiskReport(level=level, flags=flags, rules=rules, alternatives=alternatives,
                      uncovered_flags=uncovered, unevaluated_inputs=unresolved,
                      summary=summary).model_dump()


# ============ 人工审批(HITL, interrupt 暂停等待 /api/event/resume) ============

# TODO(下一期, 不可逆动作闸门): 通知成员/付款/订场等动作前再放第二个 interrupt。
# 本期只留契约与占位, 不实现 —— 但契约先写死, 免得将来随手接上去。
#
# 【为什么必须有第二道闸门】
# approval 这一道放行的是**方案**(可以改、可以反复重来); 而发通知/付款/订场是
# **已执行就回不去**的动作 —— 审批页点"通过"不等于授权扣款。两者的失败代价
# 完全不同: 方案错了改一版, 钱付重了要人工追回。所以闸门必须在放行**之后**、
# 动作**之前**, 且一次审批只放行一次(不能因为重试/重放而重复扣款)。
#
# 【幂等键口径】键 = 业务意图, 不是单次调用:
#     "{team_id}:{activity_date}:{action}:{proposal_item 的稳定标识}"
#   不能用 uuid/时间戳/请求 id 当键 —— 那些每次调用都不同, 重试会再扣一次款,
#   等于没有幂等。同一业务意图无论重试多少次、由哪个节点触发, 键必须一致。
#
# 【失败必须可对账】有副作用的调用失败时不能"当没发生过"继续跑:
#   支付网关超时无法判断是"没扣"还是"扣了没回包", 只能:
#     1) 记录 (idempotency_key, 请求参数, 响应/超时) 到可查询的账本;
#     2) 交人工/定时任务按幂等键向上游对账, 而不是自动重试;
#     3) 同一幂等键的第二次请求只做**查询**, 不做提交。
#
# 【占位接口】(下一期按此实现, 现在不接线)
#     def payment_gate(state) -> dict:
#         """checklist 之前: interrupt({"action": ..., "idempotency_key": ...})
#         只处理 approved=True 且幂等键未被消费过的动作; 失败落账本, 不自动重试"""
#         ...
#   对应 state 里预留的 approval_selections(审批页挑选结果)可作为业务意图的一部分。


def _blocking_issues(state: EventPlanState) -> List[dict]:
    """审批前还没闭合的事: 未解冲突 / 超预算 / 采集降级 / 规则未评估

    每条带来源与原因, 前端置顶显示。刻意**不**合并成一个布尔, 也不并进风险等级:
    一个值同时表示几件事, 读的人迟早只当成其中之一 —— 是预算超了、还是天气没查到,
    合并之后就无从分辨, 而这正是"这份方案能不能直接执行"的答案。

    温度/风力那条占位规则**不**进这里: 它是本期明确不做的事, 不是这次的数据缺口。
    一条永远在场的横幅只会把真正需要注意的那条淹掉。
    """
    out: List[dict] = []
    report = state.get("conflict_report") or {}
    for c in report.get("conflicts") or []:
        out.append({"kind": "conflict", "source": "约束消解", "detail": c})
    over = report.get("over_budget") or 0
    if over > 0:
        out.append({"kind": "budget", "source": "约束消解",
                    "detail": f"超出预算 {over:.0f} 元"})
    for dim, st in (state.get("data_status") or {}).items():
        if (st or {}).get("status") in ("degraded", "failed"):
            out.append({"kind": "data", "source": "采集",
                        "detail": f"{dim} 降级: {(st or {}).get('reason') or '未知原因'}"})
    for r in (state.get("risk_report") or {}).get("rules") or []:
        if r.get("status") == "not_evaluated" and r.get("rule") != RULE_TEMP_WIND:
            out.append({"kind": "risk_gap", "source": "风险评估",
                        "detail": f"{r.get('name') or r.get('rule')}未评估: "
                                  f"{r.get('reason') or '缺输入'}"})
    return out


def _build_approval_payload(state: EventPlanState) -> dict:
    """组装审批页 payload: 最终方案 + 预算明细 + 分歧点 + 风险备选"""
    proposals = state.get("proposals") or {}
    parsed = state.get("parsed_request") or {}
    budget = parsed.get("budget")

    by_category = {k: float((v or {}).get("total_cost") or 0) for k, v in proposals.items()}
    total = round(sum(by_category.values()), 2)

    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}

    return {
        "proposals": proposals,
        "budget": {
            "total": total,
            "by_category": by_category,
            "budget_limit": budget,
            "over_budget": round(total - float(budget), 2) if budget is not None else None,
        },
        "conflict_points": pref.get("conflict_points") or [],
        "risk_report": state.get("risk_report") or {},
        "conflict_report": state.get("conflict_report") or {},
        "user_feedback": state.get("user_feedback") or [],
        # 数据可用性对审批人同样重要: 哪一维没查到/降级了, 决定了这份报价该按
        # "可执行方案"还是"参考草案"来对待 —— 不能只留在服务端日志里
        "destination": _destination_brief(state),
        "data_status": state.get("data_status") or {},
        # 团队历史: 从 Store 读出来的记录(上次的人数结构/忌口/偏好, 以及过往几次
        # 活动的计划报价与预算关系、被否决原因)。**是给人看的**: 组织者在这里
        # 重做一场活动, "上次你们 30 人、忌口素食、上次因为场地容量回炉两次"是有用
        # 的参照。它**不参与任何自动判定**, 也不会去填本次没填的字段 —— 把历史
        # 拿去补预算/日期这类约束, 等于凭空发明需求。
        "team_history": state.get("team_history"),
        # 阻断项与等级分开: 等级只答"方案本身有多大风险", 这里答"还有哪些事没闭合"
        "blocking": _blocking_issues(state),
    }


def _destination_brief(state: EventPlanState) -> dict:
    """审批页要看到的"这次到底在给哪儿做方案"的一行交代

    包含用户原话、归一结果与是否为具体地点 —— 目的地是景区/古镇而方案按城市
    口径出时, 审批人能一眼看出偏差, 而不是等执行当天才发现去错了地方。
    """
    parsed = state.get("parsed_request") or {}
    basic = state.get("basic_info") or {}
    canon = basic.get("destination_canon") or {}
    dest_place = ((state.get("raw_request") or {}).get("structured")
                  or {}).get("destination_place") or {}
    return {
        "raw": parsed.get("destination") or parsed.get("origin"),
        "resolved": canon.get("display") or canon.get("name"),
        "kind": canon.get("kind"),
        "source": canon.get("source"),
        # 前端选的省市县与"更具体的地点"**另立字段**, 不重载 raw: raw 是用户原话,
        # 审批人要靠它判断"归一对不对"; 选择是入口参数, 两者对不上时正是要看的东西
        "selection": {"province": dest_place.get("province"),
                      "city": dest_place.get("city"),
                      "district": dest_place.get("district")} if dest_place else None,
        "detail": dest_place.get("detail") if dest_place else None,
        "note": canon.get("note"),
    }


def approval(state: EventPlanState) -> Dict[str, Any]:
    """人工审批节点: interrupt 暂停图执行, payload 推给前端审批页

    - 首次执行: interrupt(payload) 抛出 GraphInterrupt, 图停在此处,
      payload 经 checkpointer 持久化, 由 /api/event/resume 恢复
    - resume 后: 本节点二次执行, interrupt() 返回 resume 值
      ({"approved": bool, "feedback": str, "selections": dict}), 写入审批结论
    - selections(多候选挑选): {"food": "候选名", "transport": "高铁/火车"} 形态,
      审批页从 Proposal.options 里勾选的候选; 无论通过与否都写入
      approval_selections, 并落进 steps_log 与复盤依据
    - approved=False 时把 feedback + selections 追加进 user_feedback(reducer 累加),
      由 route_after_approval 路由回 aggregate_prefs, 意见注入四路 prompt
    """
    payload = _build_approval_payload(state)
    decision = interrupt(payload) or {}
    approved = bool(decision.get("approved"))
    feedback = str(decision.get("feedback") or "").strip()
    selections = dict(decision.get("selections") or {})
    print(f"[approval] 收到审批结果: approved={approved}, "
          f"feedback={feedback or '无'}, selections={selections or '无'}")

    out: Dict[str, Any] = {
        "approval_payload": payload,
        "approval_decision": {"approved": approved, "feedback": feedback,
                              "selections": selections},
        "approval_selections": selections,
        "steps_log": [f"[approval] {'通过' if approved else '否决(附意见)'}"
                      + (f", 挑选候选: {selections}" if selections else "")],
    }
    if not approved:
        # 否决意见 + 用户挑选的候选 一起注入重走 prompt(Annotated add 自动累加)
        merged = ([feedback] if feedback else []) + [
            f"用户挑选了「{k}」候选: {v}, 重新生成时请以该候选为准"
            for k, v in selections.items()
        ]
        if merged:
            out["user_feedback"] = merged
    elif selections:
        # 通过但用户换了候选: 换选意见同样注入(如"餐饮换成B店"), checklist 以换选后为准
        out["user_feedback"] = [
            f"用户审批时挑选了「{k}」候选: {v}, 收尾清单请以该候选为准"
            for k, v in selections.items()
        ]
    return out


def route_after_approval(state: EventPlanState) -> str:
    """approval 之后的条件边

    - approved=True -> checklist(任务清单 -> 团队记忆 -> END)
      下一期: 通知/付款等不可逆动作前, 在 checklist 之前插入第二个 interrupt
      (即"不可逆动作闸门"), 契约见本文件 approval 段上方的 TODO(含幂等键口径
      与失败对账要求)。占位待实现, 本期不改路由。
    - approved=False -> aggregate_prefs 重走(带修改意见重新聚合/采集/提案)
    """
    decision = state.get("approval_decision") or {}
    if decision.get("approved"):
        return "checklist"
    return "aggregate_prefs"


# ============ 收尾: 任务清单 + 团队记忆(审批通过后) ============

# 清单里的分工角色模板(程序兜底与提示词共用一份定义, 免得两处漂移)
_CHECKLIST_ROLE_TEMPLATE: List[tuple] = [
    ("集合签到", "按通知的时间地点集合, 清点人数、对接迟到成员"),
    ("交通对接", "确认车辆/车次, 核对发车时间与上下车点"),
    ("场地对接", "确认场地预订与开放时间, 提前到场验收"),
    ("活动执行", "按流程推进各环节, 把控节奏与时间点"),
    ("餐饮对接", "确认餐标/忌口/用餐时间, 现场核对上菜"),
    ("物资采购", "按下单清单采购与领取物资, 清点交接"),
    ("摄影记录", "拍摄活动照片, 结束时汇总素材"),
    ("应急联络", "常备药品与联系名单, 处理突发情况"),
]


def _fallback_checklist(state: EventPlanState, reason: str) -> dict:
    """清单兜底: 模型那次调用失败时, 直接用已批准的方案拼一份可用清单

    为什么要兜底: 这是流程最后一步, 且用户**已经点过"通过"** —— 在这里空手
    而归的代价最大(前面四路提案/餐饮都有各自的兜底, 只有它没有)。
    内容只用方案里已有的事实: 采购项直接取四路报价明细并标上归属, 通知文案
    由日期/人数/地点/忌口拼装。**不编造**集合时间、单价、人名, 缺就不写。
    """
    proposals = state.get("proposals") or {}
    parsed = state.get("parsed_request") or {}
    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}

    purchases: List[dict] = []
    for cat, prop in proposals.items():
        for item in (prop or {}).get("items") or []:
            name = str((item or {}).get("name") or "").strip()
            if not name:
                continue
            purchases.append({
                "item": name,
                "quantity": 1,
                "est_cost": float((item or {}).get("cost") or 0),
                "note": "来自方案报价明细",
                "category": cat,
            })

    facts: List[str] = []
    if parsed.get("date"):
        ends = f" 至 {parsed['date_end']}" if parsed.get("date_end") else ""
        facts.append(f"时间: {parsed['date']}{ends}")
    if parsed.get("people_count"):
        facts.append(f"人数: {parsed['people_count']} 人")
    if parsed.get("destination") or parsed.get("origin"):
        facts.append(f"地点: {parsed.get('destination') or parsed.get('origin')}")
    if pref.get("dietary_all"):
        facts.append(f"忌口提醒: {'、'.join(pref['dietary_all'])}")
    notice = ("【活动通知】\n"
              + "\n".join(facts)
              + ("\n具体集合时间与行程安排见下方方案, 请回复确认参加。"
                 if facts else "\n请回复确认参加。"))

    print(f"[checklist] 模型生成失败, 走程序兜底: {reason[:120]}")
    return {
        "assignments": [{"role": role, "owner": "待指定", "detail": detail}
                        for role, detail in _CHECKLIST_ROLE_TEMPLATE],
        "purchase_list": purchases,
        "notice_text": notice,
        "degraded": True,
        "warnings": [f"任务清单由程序按已批准方案直接生成(模型生成失败: {reason[:120]})。"
                     "分工为通用模板、通知文案为程序拼装, 建议人工过一遍。"],
    }


def _purchase_category(raw: Any) -> Optional[str]:
    """采购项的归属类别: 认不出的一律 None, 不硬塞一个猜测值"""
    cat = str(raw or "").strip().lower()
    return cat if cat in ("transport", "venue", "flow", "food") else None


def _finalize_checklist(data: dict, proposals: dict) -> dict:
    """程序侧收口: 归属规范化 + 费用校验 + 代码填事实字段

    【为什么这些不能交给模型】
    - 采购项的 category 认不出时置空并记警告: 它决定这项能不能参与"不超该路报价"
      的核对, 猜一个类别等于让校验对着错的基准比。
    - est_cost_is_estimate 一律由程序填 True: 清单里的钱从没被询过价。
    - 按类别汇总与所属提案报价比对: 提示词里写着"不要超过对应提案的报价",
      但在有 category 之前这条**程序做不到**, 只能靠模型自觉。现在能核了,
      核出来就摆在清单里, 而不是只写进日志(旧版把合计打印完就扔了)。
    """
    warnings: List[str] = []
    purchases = list(data.get("purchase_list") or [])
    by_cat: dict = {}
    unclassified = 0
    for item in purchases:
        cat = _purchase_category(item.get("category"))
        item["category"] = cat
        # 这个事实由程序填: 清单里的费用一律是估算, 从没被询过价
        item["est_cost_is_estimate"] = True
        if cat is None:
            unclassified += 1
            continue
        by_cat[cat] = by_cat.get(cat, 0.0) + float(item.get("est_cost") or 0)

    label = {"transport": "交通", "venue": "场地", "flow": "流程", "food": "餐饮"}
    for cat, spent in sorted(by_cat.items()):
        quote = float(((proposals.get(cat) or {}).get("total_cost")) or 0)
        if quote and spent > quote + 0.5:
            warnings.append(f"{label.get(cat, cat)}采购合计 {spent:.0f} 元超过该路报价 "
                            f"{quote:.0f} 元, 差额 {spent - quote:.0f} 元需人工确认")

    if unclassified:
        warnings.append(f"{unclassified} 项采购没标明归属提案, 未参与"
                        f"『不超该路报价』核对")
    if not purchases and proposals:
        warnings.append("采购清单为空: 方案里有需要预订/采购的内容却没有任何采购项, "
                        "请人工确认")

    data["purchase_list"] = purchases
    merged = list(data.get("warnings") or []) + warnings
    # 去重保序: 兜底那次的说明不该因为同样的校验警告出现两遍
    data["warnings"] = list(dict.fromkeys(merged))
    data["degraded"] = bool(data.get("degraded"))
    return data


async def checklist(state: EventPlanState) -> Dict[str, Any]:
    """任务清单节点: 一次 LLM 把最终方案拆成 TaskChecklist

    输入是已通过人工审批的四路方案, 输出结构化 JSON 供前端渲染可勾选清单:
    assignments 分工表 / purchase_list 采购清单(带预算与归属) / notice_text 可直接发群的通知文案

    这是流程的最后一步, 也是唯一一步"用户已经点过通过"之后的 LLM 调用 ——
    失败不能空手而归, 所以有程序兜底(见 _fallback_checklist)。
    """
    proposals = state.get("proposals") or {}
    parsed = state.get("parsed_request") or {}
    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}

    people_count = parsed.get("people_count") or pref.get("people_count") or 0
    date = parsed.get("date")
    budget = parsed.get("budget")

    prompt = build_checklist_prompt(
        proposals, people_count, date, budget,
        dietary_all=pref.get("dietary_all") or [],
        selections=state.get("approval_selections") or {},
    )
    # invoke_structured 是同步函数 -> to_thread 避免阻塞事件循环
    try:
        result = await asyncio.to_thread(invoke_structured, TaskChecklist, prompt)
        data = result.model_dump()
    except Exception as e:
        data = _fallback_checklist(state, f"{type(e).__name__}: {e}")
    data = _finalize_checklist(data, proposals)

    purchase_total = sum(i.get("est_cost") or 0 for i in data.get("purchase_list") or [])
    print(f"[checklist] 任务清单生成完成: 分工{len(data.get('assignments') or [])}项, "
          f"采购{len(data.get('purchase_list') or [])}项(合计{purchase_total:.0f}元), "
          f"通知文案{len(data.get('notice_text') or '')}字"
          + (f", 警告{len(data.get('warnings') or [])}条" if data.get("warnings") else "")
          + ("[程序兜底]" if data.get("degraded") else ""))
    steps = [f"[checklist] 分工{len(data.get('assignments') or [])}项/"
             f"采购{len(data.get('purchase_list') or [])}项/通知文案"
             + ("已生成(程序兜底)" if data.get("degraded") else "已生成")]
    for w in data.get("warnings") or []:
        steps.append(f"[checklist] 警告: {w}")
    return {"checklist": data, "steps_log": steps}


def _build_team_profile(state: EventPlanState) -> dict:
    """程序化组装团队画像记录(①): 人数结构/体能/忌口/本次偏好分布"""
    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}
    parsed = state.get("parsed_request") or {}
    return {
        "team_id": state.get("team_id"),
        "people_count": parsed.get("people_count") or pref.get("people_count"),
        "fitness_distribution": pref.get("fitness_distribution") or {},
        "dietary_all": pref.get("dietary_all") or [],
        "activity_votes": pref.get("activity_votes") or {},
        "last_activity_date": parsed.get("date"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _build_retro_record(state: EventPlanState) -> dict:
    """程序化组装这次活动的计划记录(②): 计划报价与预算的关系/被否决的原因

    全部来自 state 已有数据, 零 LLM。

    【措辞纪律】这里存的**不是**"花了多少", 而是"计划了多少, 批没批":
    记录在审批通过、清单生成之后立刻写入, 而活动本身还在未来, 一分钱没花。
    字段名带 plan_ 前缀、并写明 stage="planned", 就是为了让读它的人(下一个
    团队的解析提示词、审批页)不会把计划报价读成实际花费 —— 旧版字段叫
    total_cost、渲染成"总花费", 读起来就是已经花掉的钱。
    """
    parsed = state.get("parsed_request") or {}
    proposals = state.get("proposals") or {}
    total = sum(float((v or {}).get("total_cost") or 0) for v in proposals.values())
    budget = parsed.get("budget")
    report = state.get("conflict_report") or {}
    rejected: List[str] = []
    for fb in state.get("user_feedback") or []:
        rejected.append(f"人工否决意见: {fb}")
    for c in report.get("conflicts") or []:
        rejected.append(f"程序校验冲突: {c}")
    return {
        "stage": "planned",          # 事前计划, 不是事后复盘
        "activity_date": parsed.get("date"),
        "budget_limit": budget,
        "plan_total_cost": round(total, 2),
        # 负数=计划结余, 正数=计划超支; None=没给预算, 与 0 是两件事(0=正好打平)
        "plan_budget_deviation": round(total - float(budget), 2) if budget is not None else None,
        "approved": bool((state.get("approval_decision") or {}).get("approved")),
        "rejected_reasons": rejected,
        "replan_count": state.get("replan_count") or 0,
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _retro_store_key(state: EventPlanState) -> str:
    """记录的键 = 业务身份(哪一次活动), 不是写入时刻

    旧写法用"到秒的时间戳": 同一场活动重跑两次就留下两条无法分辨的记录, 读取时
    只能靠日期去合 —— 键本该保证这件事不发生。同一活动日重写即覆盖, 库里一个
    活动日一条("这次最终批下来的计划是什么样")。活动日缺失时才退回时间戳,
    并留日志说明为什么退化了。
    """
    date = (state.get("parsed_request") or {}).get("date")
    if date:
        return f"retro_{date}"
    print("[memory] 本次没有活动日, 记录键退回时间戳(同一场活动可能产生多条)")
    return f"retro_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


async def memory(state: EventPlanState, store: Optional[BaseStore] = None) -> Dict[str, Any]:
    """团队记忆节点: 写两条记录进 Store, namespace=("team", team_id) 团队隔离

    ① key="profile": 团队画像(人数结构/体能分布/忌口/偏好分布) - 每次覆盖最新
    ② key="retro_<活动日>": 这次活动的计划记录(计划报价与预算的关系/被否决原因)
       - 同一活动日重写即覆盖(键是业务身份, 不是写入时刻)
    下次同一 team_id 发起时, parse_request 两条解析路径都会读出来:
    文本路注入解析提示词("上次你们……"), 结构化路带进状态给审批页展示。
    无 Store 时优雅跳过(轻量流程/单测场景), 不阻塞主流程。

    写没写成会作为一个**结构化事实**返回(memory_status), 而不是只留一行日志:
    交付页要告诉用户"这次计划已记入团队档案", 而写入是可能失败并降级放行的,
    没有这个字段那句话就只能靠猜 —— 猜错就是页面在替服务端作证。
    """
    # team_id 兜底同 parse_request: 不用 "default"(那是所有人的公共桶)
    team_id = (state.get("team_id") or "").strip() or f"anon_{uuid.uuid4().hex[:12]}"
    if store is None:
        print("[memory] 无 Store, 跳过团队记忆写入")
        return {"steps_log": ["[memory] 无 Store, 跳过团队记忆写入"],
                "memory_status": {"written": False, "reason": "no_store", "team_id": team_id}}

    profile = _build_team_profile(state)
    retro = _build_retro_record(state)
    ns = ("team", team_id)
    retro_key = _retro_store_key(state)
    try:
        await store.aput(ns, "profile", profile)
        await store.aput(ns, retro_key, retro)
    except Exception as e:
        print(f"⚠️  [memory] 团队记忆写入失败(不阻塞主流程): {str(e)[:160]}")
        return {"steps_log": ["[memory] 写入失败(已降级放行)"],
                "memory_status": {"written": False, "reason": "write_failed",
                                  "error": f"{type(e).__name__}: {str(e)[:160]}",
                                  "team_id": team_id}}

    print(f"[memory] 团队记忆已写入: namespace={ns}, keys=[profile, {retro_key}]")
    return {
        "steps_log": [f"[memory] 团队记忆已写入(team_id={team_id}: 画像+复盘)"],
        "memory_status": {"written": True, "reason": None, "team_id": team_id,
                          "retro_key": retro_key,
                          "activity_date": (state.get("parsed_request") or {}).get("date")},
    }
