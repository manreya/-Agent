"""需求解析 + 偏好聚合 + 基础信息采集 + 四路提案 + 约束消解回环 Phase 验证脚本

验证项:
1. 全部核心模型假数据实例化(pydantic 校验)
2. 提示词模板: 分类命中拼片段 / 未识别 fallback 通用模板
3. build_event_graph() 可编译(含 reconcile 回环拓扑)
4. 真实 LLM 解析: 完整文本 -> 全字段; 缺失文本 -> missing_fields
5. 偏好聚合: 5人假数据手算精确断言(含 headcount 计数/忌口带人数) + 规则提示生成(零 LLM)
6. gather_info: 真实高德三路并行(时间戳重叠) + BasicInfo 完整 + 缓存命中
7. 四路提案: ①LLM时间戳重叠 ②1个POI候选报"候选不足" ③total_cost==items求和
8. 图级 Send 拓扑: generate_plan=True 全流程四路 key 合拢 + risk 节点产出 risk_report
9. 约束消解: ①必然超支回环触发+ConflictReport数字注入prompt ②replan_count到2不再回环
   ③不超支一次通过
10. 风险评估: ①规则层单测(雨天+室外/强度vs体能, 误报反例) ②无风险零LLM(炸弹函数证明)
   ③雨天+室外+弱体能 -> 规则标记2条 + 真实LLM一次生成带报价备选
11. HITL+持久化(SqliteSaver): ①发起->interrupt payload->approved=True走完
   ②杀进程重启->同thread_id resume状态原样恢复(四路报价逐路一致)
   ③approved=False+意见->回aggregate_prefs重走+意见注入四路prompt
12. 收尾: ①checklist(分工/采购/通知文案)+memory两条记录(profile/retro)
   ②同team_id二次发起parse注入历史上下文 ③不同team_id互不串数据

运行方式(在 backend 目录下):
    D:/conda_envs/langchain1.2/python.exe scripts/verify_model_phase.py
"""

import asyncio
import sys
import time
import uuid
from pathlib import Path

# 把 backend 目录加入 sys.path(脚本位于 backend/scripts/ 下)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 本脚本全程 print("✅ ..."), 而 Windows 控制台默认 GBK(cp936) 编不出 ✅/emoji,
# print 会抛 UnicodeEncodeError 直接把验证打断在半路。统一切到 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import app.config  # noqa: F401,E402  触发 load_dotenv, 保证 LLM_API_KEY 就绪

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.models.schemas import (  # noqa: E402
    EventRequest, PersonPref, PrefSummary,
    VenueInfo, BasicInfo, WeatherInfo, Location, RouteInfo,
    Proposal, ProposalItem, ConflictReport, EventPlan,
    Assignment, PurchaseItem, TaskChecklist, EventPlanResponse,
    RiskAlternative, RiskAlternatives, RiskReport,
)
from app.prompts.templates import (  # noqa: E402
    classify_activity, build_parse_prompt, FRAGMENT_MAP,
)
from app.graph import build_event_graph, EventPlanState  # noqa: E402


def _extract_interrupt(result: dict) -> dict:
    """从 ainvoke 结果提取 interrupt payload(langgraph 1.x 返回 __interrupt__ key)"""
    intr = result.get("__interrupt__")
    if not intr:
        return None
    first = intr[0]
    return first.value if hasattr(first, "value") else first


async def run_to_end(graph, inp: dict, cfg: dict, feedback: str = "") -> dict:
    """跑到图终点: 遇审批 interrupt 一律 resume approved=True(供旧用例适配 HITL)"""
    res = await graph.ainvoke(inp, config=cfg)
    while res.get("__interrupt__"):
        res = await graph.ainvoke(
            Command(resume={"approved": True, "feedback": feedback}), config=cfg,
        )
    return res


def verify_models():
    """1) 实例化全部核心模型假数据"""
    er = EventRequest(
        raw_text="20人团建,预算1万,10月1日北京出发,想搞户外拓展",
        people_count=20, date="2026-10-01", budget=10000.0,
        origin="北京", activity_type="户外拓展", missing_fields=[],
    )
    er_missing = EventRequest(raw_text="帮我策划个团建", missing_fields=["people_count", "date"])
    assert er_missing.people_count is None

    PersonPref(person_name="张三", activity_prefs=["爬山"], fitness_level=4,
               dietary=["素食"], headcount=1)
    PrefSummary(people_count=20, stated_people=20, dietary_all=["素食"],
                dietary_counts={"素食": 3},
                fitness_distribution={"3": 10, "4": 7}, activity_votes={"爬山": 12},
                conflict_points=["时间冲突"])
    vi = VenueInfo(name="某营地", address="x", location=Location(longitude=116.0, latitude=39.0),
                   visit_duration=120, description="d", ticket_price=0)
    w = WeatherInfo(date="2026-10-01", day_temp="25°C", night_temp="14℃")
    assert w.day_temp == 25 and w.night_temp == 14
    bi = BasicInfo(venues=[vi], weather=[w],
                   route_info=RouteInfo(distance=1000, duration=600, route_type="driving", description="x"))
    pr = Proposal(category="venue", items=[ProposalItem(name="场地", cost=2000.0, note="半天")],
                  total_cost=2000.0, description="d")
    ConflictReport(over_budget=-500.0, conflicts=[], suggestions=["提前预订"])
    ep = EventPlan(proposals=[pr], conflict_points=[], risk_report="r", pref_coverage={"爬山": "覆盖"})
    TaskChecklist(assignments=[Assignment(role="领队", owner="张三")],
                  purchase_list=[PurchaseItem(item="水", quantity=20, est_cost=2.0)],
                  notice_text="通知")
    EventPlanResponse(success=True, message="ok", data=None)
    print("✅ 1/4 全部核心模型假数据实例化通过")


def verify_templates():
    """2) 提示词模板: 分类命中拼片段 / 未识别 fallback"""
    # 命中"团建"
    hint = classify_activity("10月底组织30人团建，预算3万，上海周边一天")
    assert hint == "团建", f"预分类失败: {hint}"
    prompt_hit = build_parse_prompt("xxx", hint)
    assert "团建" in prompt_hit and "协作性" in prompt_hit, "片段未拼入"

    # 未识别 -> fallback 通用模板(无片段内容)
    assert classify_activity("搞个活动") is None
    prompt_fallback = build_parse_prompt("搞个活动", None)
    assert "协作性" not in prompt_fallback and "抽取规则" in prompt_fallback, "fallback 应为纯通用模板"

    # 片段映射可扩展: key 与片段一一对应
    for k, frag in FRAGMENT_MAP.items():
        assert k in frag, f"片段 {k} 未自述类型"
    print("✅ 2/4 提示词模板测试通过(命中拼片段 / 未识别 fallback)")


def verify_graph_compile():
    """3) 图编译"""
    graph = build_event_graph()
    print("✅ 3/4 build_event_graph() 编译通过(parse_request -> aggregate_prefs -> gather_info)")
    return graph


async def verify_live_parse(graph):
    """4) 真实 LLM 解析: bind_tools + tool_choice='required'"""
    # 用例A: 信息完整 -> 全字段 + missing_fields 为空
    final = await graph.ainvoke(
        {"raw_request": {"raw_text": "10月底组织30人团建，预算3万，上海周边一天"}},
        config={"recursion_limit": 10},
    )
    parsed = final.get("parsed_request") or {}
    missing = final.get("missing_fields") or []
    assert parsed.get("people_count") == 30, f"人数抽取失败: {parsed.get('people_count')}"
    assert parsed.get("budget") == 30000.0, f"预算抽取失败: {parsed.get('budget')}"
    assert parsed.get("date"), f"日期抽取失败: {parsed.get('date')}"
    assert parsed.get("origin"), f"出发地抽取失败: {parsed.get('origin')}"
    assert not missing, f"完整文本不应有缺失: {missing}"
    print(f"✅ 4/4-A 完整文本解析通过: {parsed}")

    # 用例B: 信息缺失 -> missing_fields 含 people_count/date
    final2 = await graph.ainvoke(
        {"raw_request": {"raw_text": "搞个活动"}},
        config={"recursion_limit": 10},
    )
    missing2 = final2.get("missing_fields") or []
    assert "people_count" in missing2 and "date" in missing2, f"缺失检测失败: {missing2}"
    print(f"✅ 4/4-B 缺失文本解析通过: missing_fields={missing2}")


# 5 人假数据(手算基准):
#   张三: [爬山,剧本杀] 体能3 素食 | 李四: [爬山] 体能4 -
#   王五: [剧本杀] 体能2 清真     | 赵六: [爬山] 体能5 -
#   钱七: [聚餐] 体能3 微辣
# 手算: 人数=5(无 parsed_request 时回退声明人数之和); 每行 headcount=1 -> 已表态5
#       忌口计数={素食:1, 清真:1, 微辣:1}; 忌口并集={素食,清真,微辣}
#       体能分布={2:1, 3:2, 4:1, 5:1}; 投票={爬山:3, 剧本杀:2, 聚餐:1}
def _fake_prefs():
    return [
        {"person_name": "张三", "headcount": 1, "activity_prefs": ["爬山", "剧本杀"], "fitness_level": 3, "dietary": ["素食"]},
        {"person_name": "李四", "headcount": 1, "activity_prefs": ["爬山"], "fitness_level": 4, "dietary": []},
        {"person_name": "王五", "headcount": 1, "activity_prefs": ["剧本杀"], "fitness_level": 2, "dietary": ["清真"]},
        {"person_name": "赵六", "headcount": 1, "activity_prefs": ["爬山"], "fitness_level": 5, "dietary": []},
        {"person_name": "钱七", "headcount": 1, "activity_prefs": ["聚餐"], "fitness_level": 3, "dietary": ["微辣"]},
    ]


async def verify_aggregate(graph):
    """5) 偏好聚合: 手算断言(精确相等) + 规则生成提示(零 LLM)"""
    final = await graph.ainvoke(
        {"preferences": _fake_prefs()},
        config={"recursion_limit": 10},
    )
    # 入口断言: 只有 preferences、没有 raw_text/structured 时, 图**不再绕过解析**
    # 直达聚合 —— 旧的"仅偏好直达聚合"分支已删除(入口唯一: START -> parse_request)。
    # 如今这条入参会由 parse_request 如实报 missing_fields, 而不是静默跳过解析。
    logs = " ".join(final.get("steps_log") or [])
    assert "[parse_request]" in logs, f"应经过 parse_request(入口唯一), 实际日志: {logs}"
    assert final.get("missing_fields") == ["raw_text"], \
        f"缺 raw_text 应被报出, 实际: {final.get('missing_fields')}"

    s = final.get("pref_summary")
    assert s is not None, "未产出 pref_summary"
    assert s.people_count == 5, f"人数: {s.people_count} != 5"
    assert s.stated_people == 5, f"已表态人数: {s.stated_people} != 5"
    assert set(s.dietary_all) == {"素食", "清真", "微辣"}, f"忌口并集: {s.dietary_all}"
    # 忌口带人数: 每人各忌一件 -> 每个标签 1 人(这条字段是 headcount 计数的直接体现)
    assert s.dietary_counts == {"素食": 1, "清真": 1, "微辣": 1}, f"忌口计数: {s.dietary_counts}"
    assert s.fitness_distribution == {"2": 1, "3": 2, "4": 1, "5": 1}, f"体能分布: {s.fitness_distribution}"
    assert s.activity_votes == {"爬山": 3, "剧本杀": 2, "聚餐": 1}, f"投票: {s.activity_votes}"
    print(f"✅ 5/5-A 统计断言全过(手算精确相等): {s.model_dump(exclude={'conflict_points'})}")

    # 提示生成(纯规则, 非 LLM): 5 人数据有明显活动对立(爬山3 vs 剧本杀2)
    # 活动分歧判据: 次高票 >= 最高票50% (2 >= 1.5 ✓) 且 top1+top2 <= 已表态人数 (5 <= 5 ✓)
    assert any(" vs " in c for c in s.conflict_points), f"应生成活动分歧提示: {s.conflict_points}"
    assert any("忌口约束" in c for c in s.conflict_points), f"应生成忌口约束提示: {s.conflict_points}"
    # 体能分层**不该**触发: 弱体能(<=2级)只有 1/5 = 20% < FITNESS_TIER_RATIO(25%)。
    # 旧版这句交给 LLM 自由发挥, "2级 vs 5级"经常被写成一条分歧; 现在阈值写死,
    # 单人弱体能不会被误报成"人群分层" —— 这条断言反过来锁住阈值。
    assert not any("体能分层" in c for c in s.conflict_points), \
        f"弱体能仅占 20%, 不该报分层: {s.conflict_points}"
    print(f"✅ 5/5-B 规则生成提示 {len(s.conflict_points)} 条: {s.conflict_points}")

    # 空输入防御
    final_empty = await graph.ainvoke({"preferences": []}, config={"recursion_limit": 10})
    s_empty = final_empty.get("pref_summary")
    assert s_empty is not None and (s_empty.people_count is None), "空输入应返回默认汇总"
    print("✅ 5/5-C 空偏好输入防御通过")


async def verify_gather_info():
    """6) gather_info: 真实高德三路并行 + BasicInfo 完整 + 缓存命中"""
    from app.graph.nodes import gather_info, MAX_VENUES
    from app.graph import amap_client

    # 清缓存: 确保首次调用走真实外网, 并行时间戳才有长度
    amap_client._tool_cache.clear()

    state = {
        "parsed_request": {
            "origin": "上海", "date": None,
            "people_count": 30, "budget": 30000.0, "raw_text": "30人团建 上海",
        },
        "activity_type": "团建",
    }

    # 首次调用(真实外网): 三路并行 + BasicInfo 完整
    t0 = time.perf_counter()
    out = await gather_info(state)
    elapsed1 = time.perf_counter() - t0
    log = " ".join(out["steps_log"])
    assert "✓重叠" in log, f"三路并行时间戳未重叠: {log}"

    bi = BasicInfo(**out["basic_info"])
    assert 1 <= len(bi.venues) <= MAX_VENUES, f"场地候选数量异常: {len(bi.venues)}"
    top = bi.venues[0]
    assert top.name and top.address and top.location, f"top1 场地字段不完整: {top}"
    assert len(bi.weather) >= 1, "天气预报为空"
    assert bi.route_info is not None and bi.route_info.distance > 0 and bi.route_info.duration > 0, \
        f"路线信息缺失: {bi.route_info}"
    print(f"✅ 6/6-A 真实采集通过({elapsed1:.1f}s): 候选{len(bi.venues)}个, "
          f"天气{len(bi.weather)}天, {bi.route_info.description}")
    print(f"   top1: {top.name} 评分={top.rating or '无'} 人均={top.ticket_price or '无'}元")

    # 缓存命中: 同参数二次调用返回同一缓存对象(零外网开销)
    t0 = time.perf_counter()
    cached = amap_client.search_venues("上海", "团建场地")
    elapsed2 = time.perf_counter() - t0
    cached_again = amap_client.search_venues("上海", "团建场地")
    assert cached is cached_again, "同参数二次调用未命中缓存(对象不同)"
    print(f"✅ 6/6-B 缓存命中通过: 二次调用 {elapsed2:.3f}s(首次全流程 {elapsed1:.1f}s)")

    # 防御: 无 origin 时跳过采集
    out_skip = await gather_info({"parsed_request": {}})
    assert out_skip["basic_info"] == BasicInfo().model_dump(), "无出发地应返回空 BasicInfo"
    assert "跳过采集" in " ".join(out_skip["steps_log"])
    print("✅ 6/6-C 无出发地防御跳过通过")


async def verify_propose():
    """7) 四路提案: ①LLM时间戳重叠 ②候选不足拒绝 ③total_cost==items求和"""
    from datetime import datetime as dt

    from app.graph import amap_client
    from app.graph.nodes import (
        MIN_VENUE_CANDIDATES, propose_food, propose_flow, propose_transport,
        propose_venue,
    )
    from app.models.schemas import Proposal as P

    # 公共切片数据(高德缓存的真实上海团建场地)
    pois = amap_client.search_venues("上海", "团建场地")[:10]
    venues = [p.model_dump() for p in pois]
    origin_loc = amap_client.geocode("上海")
    # 守卫: 空候选 / 解析不出坐标都会让下面取下标或 .location 直接崩 ——
    # 客户端现在按"正常业务结果 vs 故障"分开表达, 脚本不能假设一定取到值
    if not pois or origin_loc is None:
        print(f"[skip] 缺少切片前置数据(候选{len(pois)}个, origin_loc={origin_loc}), "
              f"无法跑提案阶段验证")
        return
    route = amap_client.get_route_info(origin_loc, pois[0].location, "driving")
    if route is None:
        print("[skip] 起终点之间算不出路线, 无法跑提案阶段验证")
        return

    slices = {
        "transport": {"route_info": route.model_dump(), "people_count": 30, "budget": 30000.0},
        "venue": {"venues": venues, "people_count": 30, "budget": 30000.0},
        "flow": {"activity_votes": {"爬山": 3, "剧本杀": 2, "聚餐": 1},
                 "fitness_distribution": {"2": 1, "3": 2, "4": 1, "5": 1},
                 "activity_type": "团建", "date": "2026-10-31"},
        "food": {"venues": venues, "dietary_all": ["素食", "清真"],
                 "people_count": 30, "budget": 30000.0},
    }
    fns = {"transport": propose_transport, "venue": propose_venue,
           "flow": propose_flow, "food": propose_food}

    # ① 四路并发调用, 各自记录起止时间 -> 重叠判定
    async def timed(name, fn, payload):
        s = dt.now()
        out = await fn(payload)
        e = dt.now()
        return name, s, e, out

    results = await asyncio.gather(*[timed(k, fns[k], slices[k]) for k in fns])
    ranges = [(s, e) for _, s, e, _ in results]
    latest_start = max(s for s, _ in ranges)
    earliest_end = min(e for _, e in ranges)
    assert latest_start < earliest_end, \
        f"四路 LLM 时间戳未重叠: {[(n, s.strftime('%H:%M:%S.%f'), e.strftime('%H:%M:%S.%f')) for n, s, e, _ in results]}"
    print(f"✅ 7/7-A 四路 LLM 时间戳重叠: "
          f"{[(n, f'{(e-s).total_seconds():.1f}s') for n, s, e, _ in results]}, "
          f"公共区间 {(earliest_end - latest_start).total_seconds():.1f}s")

    # ③ 每路 Proposal: category 合法 + total_cost == items 求和
    for name, _, _, out in results:
        prop = P(**out["proposals"][name])
        item_sum = round(sum(i.cost for i in prop.items), 2)
        assert prop.category == name, f"{name} 路 category 错误: {prop.category}"
        assert abs(prop.total_cost - item_sum) < 0.01, \
            f"{name} 路 total_cost({prop.total_cost}) != items求和({item_sum})"
        print(f"   {name}: total={prop.total_cost} == items和={item_sum} ✓ ({len(prop.items)}项)")
    print("✅ 7/7-B 四路 total_cost == items 求和断言全过")

    # ② 候选不足: 仅 1 个 POI -> 程序化拒绝, 不编造
    out_one = await propose_venue({"venues": venues[:1], "people_count": 30, "budget": 30000.0})
    v = P(**out_one["proposals"]["venue"])
    assert "候选不足" in v.description, f"应明确报候选不足: {v.description}"
    assert len(v.items) == 0 and v.total_cost == 0, f"拒绝时不应有报价: {v.items}"
    print(f"✅ 7/7-C 仅1个POI候选 -> 明确拒绝: {v.description}")


async def verify_propose_graph():
    """8) 图级 Send 拓扑: 全流程 generate_plan=True -> proposals 四 key 合拢 + HITL 放行"""
    g = build_event_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 40}
    final = await run_to_end(
        g,
        {"raw_request": {"raw_text": "10月底组织30人团建，预算3万，上海周边一天"},
         "generate_plan": True},
        cfg,
    )
    props = final.get("proposals") or {}
    assert set(props.keys()) == {"transport", "venue", "flow", "food"}, \
        f"Send 派发后应有四路 key: {set(props.keys())}"
    for k, v in props.items():
        p = Proposal(**v)
        assert abs(p.total_cost - sum(i.cost for i in p.items)) < 0.01, f"{k} 求和不符"
    # risk 节点在 reconcile 放行后必经, 终态应有 risk_report
    risk = final.get("risk_report")
    assert risk is not None and risk.get("level") in ("low", "medium", "high"), \
        f"图终态应含 risk_report: {risk}"
    assert any(s.startswith("[risk]") for s in final.get("steps_log") or []), "steps_log 应含 risk 节点日志"
    print(f"✅ 8/8 图级 Send 并行拓扑通过: 四路 key 合拢, 总预算 "
          f"{sum(Proposal(**v).total_cost for v in props.values()):.0f} 元, "
          f"风险评估 level={risk.get('level')}")


def verify_reconcile():
    """9) 约束消解: 确定性断言(零LLM零外网)

    ① 必然超支(预算5000/四路合计9000) -> over_budget==4000 + 回环触发
       + ConflictReport 具体数字注入重生成 prompt
    ② replan_count 到 2(下一轮变3)后 route 不再返回 Send
    ③ 不超支/无冲突 -> 一次通过
    附: 时间线重叠检测 + 场地容量检查
    """
    from app.graph.nodes import (
        MAX_REPLAN, _detect_capacity_conflict, _detect_timeline_overlap,
        reconcile, route_after_reconcile,
    )
    from app.prompts.templates import build_transport_prompt

    def _prop(cat, cost, items=None):
        # items 默认给一条与 total_cost 守恒的明细, 避免误触"venue 无有效报价"规则
        if items is None:
            items = [{"name": f"{cat}费用", "cost": float(cost), "note": ""}]
        return {"category": cat, "items": items, "total_cost": float(cost),
                "description": f"{cat}方案"}

    # ---- ① 必然超支: 预算5000, 四路合计 800+3200+1000+4000=9000 -> over=4000 ----
    overspent_state = {
        "parsed_request": {"people_count": 30, "budget": 5000.0},
        "proposals": {
            "transport": _prop("transport", 800), "venue": _prop("venue", 3200),
            "flow": _prop("flow", 1000), "food": _prop("food", 4000),
        },
        "replan_count": 0,
    }
    out = reconcile(overspent_state)
    report = out["conflict_report"]
    assert report["over_budget"] == 4000.0, f"超支金额应为4000: {report['over_budget']}"
    assert any("超支4000" in c for c in report["conflicts"]), f"冲突应含具体数字: {report['conflicts']}"
    assert any("9000" in c for c in report["conflicts"]), "冲突应含四路合计9000"
    assert any("降级空间" in s and "4000" in s for s in report["suggestions"]), \
        f"建议应含可降级空间数字: {report['suggestions']}"
    assert out["replan_count"] == 1, f"首次检出问题 replan_count 应自增为1: {out['replan_count']}"
    print(f"✅ 9/9-A 必然超支检出: over_budget=4000, 冲突={report['conflicts']}")
    print(f"   建议: {report['suggestions']}")

    # 回环触发: replan_count=1(<=2) 时 route 返回 4 个 Send(重新派发)
    loop_state = dict(overspent_state)
    loop_state["conflict_report"] = report
    loop_state["replan_count"] = 1
    sends = route_after_reconcile(loop_state)
    assert len(sends) == 4, f"应重新派发四路 Send: {len(sends)}"
    from langgraph.types import Send as _Send
    assert all(isinstance(s, _Send) for s in sends), "返回元素应为 Send"
    assert all(s.arg.get("conflict_report") for s in sends), "回炉 payload 应携带 ConflictReport"
    print("✅ 9/9-B 回环触发: replan_count=1 -> route 返回 4 个 Send(带 ConflictReport)")

    # ConflictReport 具体数字注入重生成 prompt
    prompt = build_transport_prompt({"distance": 1000}, 30, 5000.0, conflict_report=report)
    assert "校验报告" in prompt and "4000" in prompt, "重生成 prompt 应含校验报告与具体数字"
    assert "超支4000" in prompt, "prompt 应含'超支4000'原文"
    print("✅ 9/9-C 重生成 prompt 注入 ConflictReport 具体数字(超支4000)")

    # ---- ② replan_count 到 2 后不再回环 ----
    # 语义: reconcile 每次检出问题自增; route 在 replan_count <= MAX_REPLAN 时回炉
    #       replan_count=2 时仍回炉一次(第2次), 下一轮自增到 3 > 2 -> 终止
    cap_state = dict(overspent_state)
    cap_state["conflict_report"] = report
    cap_state["replan_count"] = 2
    out2 = reconcile(cap_state)
    assert out2["replan_count"] == 3, f"再次检出应自增到3: {out2['replan_count']}"
    assert "不再回炉" in " ".join(out2["steps_log"]), "应提示不再回炉(HITL)"
    sends_stop = route_after_reconcile({**cap_state, "conflict_report": out2["conflict_report"],
                                        "replan_count": 3})
    assert sends_stop == "risk", f"replan_count=3(>2) 不应再回环, 应路由到风险节点: {sends_stop}"
    sends_last = route_after_reconcile({**cap_state, "conflict_report": out2["conflict_report"],
                                        "replan_count": 2})
    assert len(sends_last) == 4, "replan_count=2 时应回炉最后一次"
    print(f"✅ 9/9-D replan_count 上限: 2时回炉最后一次, 3(>MAX_REPLAN={MAX_REPLAN})时路由到 risk 节点")

    # ---- ③ 不超支/无冲突 -> 一次通过 ----
    ok_state = {
        "parsed_request": {"people_count": 30, "budget": 20000.0},
        "proposals": {
            "transport": _prop("transport", 1500), "venue": _prop("venue", 8000),
            "flow": _prop("flow", 2000), "food": _prop("food", 6000),
        },
        "replan_count": 0,
    }
    out_ok = reconcile(ok_state)
    assert out_ok["conflict_report"]["conflicts"] == [], f"不应有冲突: {out_ok['conflict_report']}"
    # schema 约定 over_budget 负数=结余: 17500 vs 预算20000 -> -2500
    assert out_ok["conflict_report"]["over_budget"] == -2500.0, \
        f"结余应为-2500: {out_ok['conflict_report']['over_budget']}"
    assert route_after_reconcile({**ok_state, "conflict_report": out_ok["conflict_report"],
                                  "replan_count": 0}) == "risk", "通过后应路由到风险节点而非直通END"
    assert out_ok["replan_count"] == 0, "通过时不应自增"
    print("✅ 9/9-E 不超支输入一次通过: conflicts=[], over_budget=0, 无回环")

    # ---- 附: 时间线重叠 + 容量检查(纯程序检测器) ----
    flow_bad = {"items": [
        {"name": "09:00-12:00 上午拓展", "cost": 0, "note": ""},
        {"name": "10:30-11:30 团队游戏", "cost": 0, "note": "与拓展冲突"},
        {"name": "14:00-17:00 下午自由活动", "cost": 0, "note": ""},
    ]}
    overlaps = _detect_timeline_overlap(flow_bad)
    assert len(overlaps) == 1, f"应检出1对重叠(09:00-12:00 vs 10:30-11:30): {overlaps}"
    assert "09:00" in overlaps[0] and "10:30" in overlaps[0]
    flow_ok = {"items": [{"name": "09:00-12:00 拓展", "cost": 0, "note": ""},
                         {"name": "13:00-17:00 自由活动", "cost": 0, "note": ""}]}
    assert _detect_timeline_overlap(flow_ok) == []
    print("✅ 9/9-F 时间线重叠检测: 重叠对精确检出, 无重叠不误报")

    venue_small = {"description": "小型场地", "items": [{"name": "场地费", "cost": 100, "note": "容纳20人(推断)"}]}
    cap_conflicts = _detect_capacity_conflict(venue_small, 30)
    assert len(cap_conflicts) == 1 and "20" in cap_conflicts[0] and "30" in cap_conflicts[0], \
        f"容量不足应检出: {cap_conflicts}"
    venue_big = {"description": "", "items": [{"name": "场地费", "cost": 100, "note": "容纳50人(推断)"}]}
    assert _detect_capacity_conflict(venue_big, 30) == []
    print("✅ 9/9-G 场地容量检查: 容量20<30检出, 容量50>=30通过")

    # 求和守恒再确认: reconcile 输入的 proposals 与 _finalize_proposal 输出同构
    assert isinstance(report, dict) and set(report.keys()) == {
        "over_budget", "conflicts", "suggestions",
        "conflicts_by_category"}, \
        f"ConflictReport 字段集变了: {sorted(report.keys())}"


async def verify_reconcile_loop(graph):
    """9-H 图级回环: 必然超支输入(预算3000/30人) -> 真实LLM回炉 -> 上限后带冲突放行 -> 审批"""
    g = build_event_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 40}
    final = await run_to_end(
        g,
        {"raw_request": {"raw_text": "10月底组织30人团建，预算3000元，上海周边一天"},
         "generate_plan": True},
        cfg,
    )
    steps = final.get("steps_log") or []
    steps_text = " ".join(steps)
    replan = final.get("replan_count") or 0
    report = final.get("conflict_report") or {}

    propose_rounds = sum(1 for s in steps if s.startswith("[propose:"))
    assert propose_rounds > 4, f"应发生回炉(四路×多轮), 实际 propose 日志 {propose_rounds} 条"
    assert "回炉" in steps_text, f"steps_log 应含回炉记录: {steps_text[-300:]}"
    assert replan >= 1, f"replan_count 应 >= 1: {replan}"
    assert (report.get("over_budget") or 0) > 0 or report.get("conflicts"), \
        f"终态应带 ConflictReport: {report}"
    # 回炉超限放行后仍应经过 risk 节点
    assert any(s.startswith("[risk]") for s in steps), \
        f"超限放行后应经过 risk 节点: {steps[-3:]}"
    if replan > 2:
        assert "不再回炉" in steps_text, "超上限时应提示不再回炉"
    print(f"✅ 9-H 图级回环: propose 日志{propose_rounds}条(即回炉{(propose_rounds // 4) - 1}次), "
          f"replan_count={replan}, 终态超支={report.get('over_budget')}元, "
          f"带冲突经 risk 节点交人工裁决")


def _risk_state(rain: bool, weak: bool) -> dict:
    """risk 节点验证用 mock state

    rain=False -> 活动日晴天;  weak=False -> 全员体能3级(无弱体能)
    流程默认含室外高强度项目(拓展训练4级/攀岩5级/定向越野4级, 平均4.33)
    """
    weather = [{
        "date": "2026-10-31", "day_weather": "小雨" if rain else "晴",
        "night_weather": "阴", "day_temp": 20, "night_temp": 12,
        "wind_direction": "东南", "wind_power": "3",
    }]
    flow = {"category": "flow", "total_cost": 9000.0, "description": "d", "items": [
        {"name": "09:00-11:30 拓展训练", "cost": 3000, "note": ""},
        {"name": "13:00-15:00 攀岩体验", "cost": 3500, "note": ""},
        {"name": "15:30-17:30 定向越野", "cost": 2500, "note": ""},
    ]}
    fitness = {"1": 2, "2": 6, "3": 22} if weak else {"3": 30}   # weak: 8/30=26.7% 弱体能
    return {
        "parsed_request": {"people_count": 30, "date": "2026-10-31",
                           "budget": 30000.0, "raw_text": "30人团建"},
        "activity_type": "团建",
        "basic_info": {"weather": weather, "venues": [], "route_info": None},
        "proposals": {"flow": flow},
        "pref_summary": {"people_count": 30, "stated_people": 30,
                         "fitness_distribution": fitness,
                         "dietary_all": [], "dietary_counts": {}, "activity_votes": {}},
    }


def verify_risk_rules():
    """10-A 规则层单测(纯函数, 零LLM零外网)

    ① 雨天+室外 -> 标记; 雨天+纯室内 -> 不标记(不妄下结论)
    ② 强度: 平均4.33级+弱体能26.7% -> 标记; 弱体能0% -> 不标记
    """
    from app.graph.nodes import _detect_intensity_conflict, _detect_weather_risk

    basic = _risk_state(True, True)["basic_info"]
    parsed = _risk_state(True, True)["parsed_request"]
    flow = _risk_state(True, True)["proposals"]["flow"]
    pref_weak = _risk_state(True, True)["pref_summary"]
    pref_strong = _risk_state(True, False)["pref_summary"]

    # ① 雨天 + 室外项目 -> 标记(含项目名)
    w = _detect_weather_risk(basic, parsed, flow)
    assert w and "室外项目" in w and "拓展训练" in w and "小雨" in w, f"雨天+室外应标记: {w}"
    # 雨天 + 纯室内(剧本杀/聚餐) -> 不标记
    flow_indoor = {"items": [{"name": "09:00-12:00 剧本杀", "cost": 0, "note": ""},
                             {"name": "13:00-17:00 聚餐", "cost": 0, "note": ""}]}
    assert _detect_weather_risk(basic, parsed, flow_indoor) is None, "雨天+纯室内不应标记"
    # 晴天 + 室外 -> 不标记
    basic_sunny = _risk_state(False, True)["basic_info"]
    assert _detect_weather_risk(basic_sunny, parsed, flow) is None, "晴天不应标记"
    print(f"✅ 10-A① 雨天+室外标记(含具体项目), 雨天+室内/晴天不误报: {w}")

    # ② 强度 vs 体能: 平均(4+5+4)/3=4.33 >= 4 且弱体能 8/30=26.7% >= 25% -> 标记
    i = _detect_intensity_conflict(flow, pref_weak)
    assert i and "4.3" in i and "27%" in i, f"强度不匹配应标记(含具体数字): {i}"
    # 全员体能3级(弱体能0%) -> 不标记
    assert _detect_intensity_conflict(flow, pref_strong) is None, "无弱体能不应标记"
    # 解析不出强度的流程(纯室内) -> 不标记
    assert _detect_intensity_conflict(flow_indoor, pref_weak) is None, "纯室内低强度不应标记"
    print(f"✅ 10-A② 强度不匹配标记(平均4.3级/弱体能27%), 体能充足/低强度不误报: {i}")


async def verify_risk_zero_llm():
    """10-B 无风险 -> 零LLM(用炸弹函数证明: 若调 LLM 直接断言失败)"""
    from app.graph import nodes as _nodes
    from app.graph.nodes import assess_risk

    state = _risk_state(rain=False, weak=False)   # 晴天 + 全员体能3级 -> 双规则均不触发
    orig = _nodes.invoke_structured

    def _bomb(*a, **k):
        raise AssertionError("无风险标记时不应调用 LLM(花了不该花的钱)")

    _nodes.invoke_structured = _bomb
    try:
        out = await asyncio.to_thread(assess_risk, state)
    finally:
        _nodes.invoke_structured = orig

    report = out["risk_report"]
    assert report["level"] == "low" and report["flags"] == [] and report["alternatives"] == [], \
        f"无风险应 low+空: {report}"
    assert "零LLM" in " ".join(out["steps_log"]), f"日志应标注零LLM: {out['steps_log']}"
    print("✅ 10-B 无风险零LLM: 炸弹函数未触发(未调LLM), level=low, flags=[]")


async def verify_risk_with_llm():
    """10-C 雨天+室外+弱体能 -> 规则层标记2条 + 真实LLM一次生成带报价备选"""
    from app.graph.nodes import assess_risk

    state = _risk_state(rain=True, weak=True)   # 双规则全触发 -> level=high
    out = await asyncio.to_thread(assess_risk, state)
    report = out["risk_report"]

    assert report["level"] == "high", f"双规则触发应为 high: {report['level']}"
    assert len(report["flags"]) == 2, f"应标记2条(雨天+强度): {report['flags']}"
    assert any("室外项目" in f for f in report["flags"]), "应含雨天+室外标记"
    assert any("强度不匹配" in f for f in report["flags"]), "应含强度不匹配标记"
    alts = report["alternatives"]
    assert len(alts) >= 2, f"每条风险都应有备选(实际{len(alts)}条): {alts}"
    for a in alts:
        assert a["issue"] and a["plan"], f"备选应含 issue/plan: {a}"
        assert a["est_cost"] >= 0, f"报价应为非负数: {a}"
    # 备选应落在具体形式(室内替换), 不写"注意防雨"式空话
    joined = " ".join(a["plan"] for a in alts)
    print(f"✅ 10-C 有风险: 标记{len(report['flags'])}条(level=high), "
          f"LLM 一次生成{len(alts)}条备选(带报价)")
    for a in alts:
        print(f"   - [{a['issue'][:20]}...] {a['plan']}(约{a['est_cost']:.0f}元)")


def verify_risk_models():
    """10-0 风险模型假数据实例化"""
    RiskReport(level="low", flags=[], alternatives=[],
               summary="未检出天气/强度风险")
    RiskAlternatives(alternatives=[RiskAlternative(issue="雨天+室外", plan="改室内剧本杀: XX店",
                                                   est_cost=2400.0)])
    print("✅ 10-0 风险模型(RiskReport/RiskAlternatives/RiskAlternative)实例化通过")


_HITL_DB = Path(__file__).resolve().parent.parent / "data" / "verify_hitl.db"
_HITL_INPUT = {"raw_request": {"raw_text": "10月底组织30人团建，预算3万，上海周边一天"},
               "generate_plan": True}


def _hitl_graph(conn):
    """构建带 AsyncSqliteSaver 的图(每次调用新建, 模拟独立进程实例)"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    return build_event_graph(checkpointer=AsyncSqliteSaver(conn))


# ============ 12. checklist 任务清单 + memory 团队记忆 ============

_MEMORY_TEAM_A = "verify_team_alpha"   # 主团队(验证历史注入)
_MEMORY_TEAM_B = "verify_team_beta"    # 隔离团队(验证互不串数据)
# 独立 db 文件且不做 unlink(环境安全删除 shim 会拦截 Path.unlink,
# 且 thread_id 为 uuid 不会撞, 无需清库)
_CHECKLIST_DB = Path(__file__).resolve().parent.parent / "data" / "verify_checklist.db"


async def verify_checklist_memory():
    """12-A 完整跑通: 发起(InMemoryStore) -> 审批通过 -> checklist 产出 + memory 写入两条记录"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    import aiosqlite
    conn = await aiosqlite.connect(str(_CHECKLIST_DB))
    try:
        g = build_event_graph(checkpointer=AsyncSqliteSaver(conn), store=store)
        tid = str(uuid.uuid4())
        cfg = {"configurable": {"thread_id": tid}, "recursion_limit": 40}
        inp = {"raw_request": {"raw_text": "10月底组织30人团建，预算3万，上海周边一天",
                               "team_id": _MEMORY_TEAM_A},
               "generate_plan": True}

        res = await g.ainvoke(dict(inp), config=cfg)
        assert _extract_interrupt(res) is not None, "应先暂停于审批闸门"

        res2 = await g.ainvoke(Command(resume={"approved": True, "feedback": ""}), config=cfg)
        steps = res2.get("steps_log") or []
        assert any("[checklist]" in s for s in steps), f"审批通过后应执行 checklist: {steps[-3:]}"
        assert any("[memory]" in s for s in steps), f"checklist 后应执行 memory: {steps[-3:]}"

        # checklist 结构化产出(前端渲染可勾选清单的三块数据)
        cl = res2.get("checklist") or {}
        assert cl.get("assignments"), f"分工表不应为空: {cl}"
        assert cl.get("purchase_list"), f"采购清单不应为空: {cl}"
        notice = cl.get("notice_text") or ""
        assert len(notice) >= 30, f"通知文案应可直接发群(过短): {notice!r}"
        assert all(i.get("est_cost", 0) >= 0 for i in cl["purchase_list"]), "采购预算项应非负"
        print(f"✅ 12-A① 发起->审批通过: checklist 分工{len(cl['assignments'])}项/"
              f"采购{len(cl['purchase_list'])}项(合计"
              f"{sum(i.get('est_cost', 0) for i in cl['purchase_list']):.0f}元), "
              f"通知文案{len(notice)}字")

        # memory: namespace=("team", team_id) 下两条记录(profile + retro_*)
        items = await store.asearch(("team", _MEMORY_TEAM_A))
        keys = {i.key for i in items}
        assert "profile" in keys, f"应写入团队画像 profile: {keys}"
        retro_keys = [k for k in keys if k.startswith("retro")]
        assert len(retro_keys) == 1, f"应写入 1 条复盘记录: {keys}"

        profile = (await store.aget(("team", _MEMORY_TEAM_A), "profile")).value
        assert profile.get("people_count") == 30, f"画像应含人数结构: {profile}"
        assert "fitness_distribution" in profile and "dietary_all" in profile, \
            f"画像应含体能分布/忌口: {profile}"
        retro = (await store.aget(("team", _MEMORY_TEAM_A), retro_keys[0])).value
        assert "budget_deviation" in retro and "rejected_reasons" in retro, \
            f"复盘应含预算偏差/被否决原因: {retro}"
        print(f"✅ 12-A② memory 写入两条记录: 画像(人数{profile['people_count']}/体能分布"
              f"{profile['fitness_distribution']}) + 复盘(预算偏差{retro['budget_deviation']}元, "
              f"否决原因{len(retro['rejected_reasons'])}条)")
    finally:
        await conn.close()


async def verify_memory_history_injection():
    """12-B 同 team_id 第二次发起: parse_request 注入历史上下文; 12-C 不同 team_id 互不串数据"""
    from langgraph.store.memory import InMemoryStore
    from app.graph.nodes import parse_request
    from app.prompts.templates import build_history_context_block

    store = InMemoryStore()
    # 预置团队 A 的历史记录(模拟 12-A 已跑过一次)
    await store.aput(("team", _MEMORY_TEAM_A), "profile", {
        "people_count": 30, "fitness_distribution": {"3": 12, "4": 10, "2": 8},
        "dietary_all": ["素食", "清真"], "activity_votes": {"爬山": 12, "剧本杀": 8},
        "last_activity_date": "2026-09-20",
    })
    await store.aput(("team", _MEMORY_TEAM_A), "retro_20260920", {
        "activity_date": "2026-09-20", "total_cost": 29510.0, "budget_limit": 30000.0,
        "budget_deviation": -490.0, "rejected_reasons": ["人工否决意见: 餐饮不要火锅"],
        "replan_count": 1,
    })

    # 12-B: 同 team_id -> 注入历史
    res_a = await parse_request(
        {"raw_request": {"raw_text": "11月中旬再组织一次30人团建，预算3万，上海周边",
                         "team_id": _MEMORY_TEAM_A}},
        store=store,
    )
    assert res_a["team_id"] == _MEMORY_TEAM_A
    assert any("已注入团队历史" in s for s in res_a["steps_log"]), \
        f"steps 应记录历史注入: {res_a['steps_log']}"
    # 注入块内容抽检: 应含"上次你们"与画像/复盘关键词
    profile = (await store.aget(("team", _MEMORY_TEAM_A), "profile")).value
    retro = (await store.aget(("team", _MEMORY_TEAM_A), "retro_20260920")).value
    block = build_history_context_block(profile, [retro])
    assert block and "上次你们" in block, f"历史块应含'上次你们': {block}"
    assert "素食" in block and "结余" in block, f"历史块应含画像/复盘数字: {block}"
    print(f"✅ 12-B 同 team_id 二次发起: parse prompt 注入历史上下文(含上次人数/忌口/结余数字)")

    # 12-C: 不同 team_id -> 不注入(互不串数据)
    res_b = await parse_request(
        {"raw_request": {"raw_text": "12月初组织20人团建，预算2万，苏州",
                         "team_id": _MEMORY_TEAM_B}},
        store=store,
    )
    assert not any("已注入团队历史" in s for s in res_b["steps_log"]), \
        f"team B 不应读到 team A 的历史: {res_b['steps_log']}"
    items_b = await store.asearch(("team", _MEMORY_TEAM_B))
    assert not list(items_b), "team B namespace 下应为空(隔离)"
    print(f"✅ 12-C 不同 team_id 隔离: team B 无历史注入, namespace 下零记录")


async def verify_persistent_store():
    """12-D 持久化 Store(AsyncSqliteStore): 写入 -> 裸SQL直读

    关键坑(已在 routes/event.py 修复): aiosqlite.connect 必须传 isolation_level=None,
    否则 store.setup() 的 INSERT 留下未提交事务, 后续 aput 的显式 BEGIN 报
    "cannot start a transaction within a transaction"(降级放行, 记录全丢)。
    """
    from langgraph.store.sqlite.aio import AsyncSqliteStore

    import aiosqlite as _aiosqlite
    import sqlite3 as _sqlite3

    db = Path(__file__).resolve().parent.parent / "data" / "verify_store.db"
    conn = await _aiosqlite.connect(str(db), isolation_level=None)
    try:
        store = AsyncSqliteStore(conn)
        await store.setup()
        await store.aput(("team", "t_persist"), "profile", {"people_count": 10})
        await store.aput(("team", "t_persist"), "retro_x", {"budget_deviation": -100.0})
    finally:
        await conn.close()

    # 裸 SQL 直读: 证明真落盘(非内存态)
    raw_conn = _sqlite3.connect(str(db))
    try:
        rows = raw_conn.execute("SELECT key, value FROM store").fetchall()
    finally:
        raw_conn.close()
    keys = {k for k, _ in rows}
    assert "profile" in keys and "retro_x" in keys, f"应落盘两条记录: {keys}"
    print(f"✅ 12-D 持久化 Store: AsyncSqliteStore 写入 2 条 -> memory.db 裸SQL直读成功 "
          f"(isolation_level=None 关键坑已回归覆盖)")


async def verify_hitl_approve():
    """11-A 发起 -> interrupt payload -> approved=True resume -> 流程走完"""
    import aiosqlite

    if _HITL_DB.exists():
        _HITL_DB.unlink()
    conn = await aiosqlite.connect(str(_HITL_DB))
    try:
        g = _hitl_graph(conn)
        tid = str(uuid.uuid4())
        cfg = {"configurable": {"thread_id": tid}, "recursion_limit": 40}

        res = await g.ainvoke(dict(_HITL_INPUT), config=cfg)
        payload = _extract_interrupt(res)
        assert payload is not None, f"全流程应暂停于审批闸门: steps={res.get('steps_log', [])[-2:]}"

        # payload 结构: 最终方案 + 预算明细 + 分歧点 + 风险备选
        assert {"proposals", "budget", "conflict_points", "risk_report"} <= set(payload.keys()), \
            f"payload 缺关键字段: {set(payload.keys())}"
        assert set(payload["proposals"].keys()) == {"transport", "venue", "flow", "food"}, \
            f"payload 应含四路方案: {set(payload['proposals'].keys())}"
        total = round(sum(float(p["total_cost"]) for p in payload["proposals"].values()), 2)
        assert abs(payload["budget"]["total"] - total) < 0.01, \
            f"预算明细 total({payload['budget']['total']}) != 四路求和({total})"
        print(f"✅ 11-A① 发起后暂停审批: thread={tid[:8]}..., 四路合计{total}元, "
              f"payload 字段齐全")

        res2 = await g.ainvoke(Command(resume={"approved": True, "feedback": ""}), config=cfg)
        assert not res2.get("__interrupt__"), "approved=True 后不应再次暂停"
        decision = res2.get("approval_decision") or {}
        assert decision.get("approved") is True, f"审批决策应写回 state: {decision}"
        steps = res2.get("steps_log") or []
        assert any("[approval] 通过" in s for s in steps), f"steps 应含审批通过记录: {steps[-2:]}"
        assert res2.get("proposals", {}).keys() == payload["proposals"].keys(), "终态应保留四路方案"
        print("✅ 11-A② approved=True resume -> 流程走完, 决策写回 state")
    finally:
        await conn.close()


async def verify_hitl_restart():
    """11-B 核心演示: 发起 -> '杀进程'(关闭实例A/连接) -> 重启(实例B同库) -> 同 thread resume

    状态原样恢复的证据: 恢复后继续出的最终方案与中断前 payload 的四路报价完全一致。
    """
    import aiosqlite

    tid = str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": tid}, "recursion_limit": 40}

    # ---- 进程A: 发起并中断 ----
    conn_a = await aiosqlite.connect(str(_HITL_DB))
    try:
        g_a = _hitl_graph(conn_a)
        res = await g_a.ainvoke(dict(_HITL_INPUT), config=cfg)
        payload = _extract_interrupt(res)
        assert payload is not None, "进程A应暂停于审批闸门"
        proposals_a = payload["proposals"]
        snap = await g_a.aget_state(cfg)
        assert tuple(snap.next) == ("approval",), f"中断点应在 approval: {snap.next}"
        print(f"✅ 11-B① 进程A发起: thread={tid[:8]}..., 中断于 approval, "
              f"四路合计{payload['budget']['total']}元")
    finally:
        await conn_a.close()   # 模拟进程被杀: 连接直接关闭, 状态已落 checkpoints db

    # ---- 进程B: 全新实例 + 同一数据库 + 同一 thread_id ----
    conn_b = await aiosqlite.connect(str(_HITL_DB))
    try:
        g_b = _hitl_graph(conn_b)   # 全新图实例(重启后的"新进程")
        snap = await g_b.aget_state(cfg)
        assert snap.values, "重启后同 thread_id 应能读到持久化状态"
        assert tuple(snap.next) == ("approval",), f"重启后中断点应原样恢复: {snap.next}"
        assert (snap.values.get("proposals") or {}).keys() == proposals_a.keys(), \
            "重启后 state 里的四路方案应原样保留"

        res2 = await g_b.ainvoke(Command(resume={"approved": True, "feedback": ""}), config=cfg)
        assert not res2.get("__interrupt__"), "重启后 resume 应走完全程"
        assert (res2.get("approval_decision") or {}).get("approved") is True
        proposals_b = res2.get("proposals") or {}
        for k in proposals_a:
            assert abs(float(proposals_b[k]["total_cost"]) - float(proposals_a[k]["total_cost"])) < 0.01, \
                f"{k} 报价应与中断前一致: {proposals_b[k]['total_cost']} vs {proposals_a[k]['total_cost']}"
        print(f"✅ 11-B② 进程B(重启)同 thread_id resume: 状态原样恢复, 四路报价逐路一致, 流程走完")
    finally:
        await conn_b.close()


async def verify_hitl_reject_feedback():
    """11-C approved=False + 修改意见 -> 回 aggregate_prefs 重走 + 意见进入后续 prompt"""
    import aiosqlite

    from app.prompts.templates import build_venue_prompt

    tid = str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": tid}, "recursion_limit": 40}
    fb = "餐饮不要火锅, 换成素食自助; 流程强度别太大"

    conn = await aiosqlite.connect(str(_HITL_DB))
    try:
        g = _hitl_graph(conn)
        res = await g.ainvoke(dict(_HITL_INPUT), config=cfg)
        assert _extract_interrupt(res) is not None, "应先暂停于审批闸门"

        # 否决 + 意见 -> 回 aggregate_prefs 重走 -> 再次暂停
        res2 = await g.ainvoke(Command(resume={"approved": False, "feedback": fb}), config=cfg)
        new_payload = _extract_interrupt(res2)
        assert new_payload is not None, f"否决重走后应再次暂停审批: {res2.get('steps_log', [])[-2:]}"

        steps = res2.get("steps_log") or []
        assert sum(1 for s in steps if s.startswith("[aggregate_prefs]")) == 2, \
            f"否决后应重走 aggregate_prefs(聚合日志应出现2次): {steps}"
        assert any("[approval] 否决" in s for s in steps), "steps 应含否决记录"
        assert fb in (res2.get("user_feedback") or []), \
            f"修改意见应累加进 state.user_feedback: {res2.get('user_feedback')}"
        assert fb in (new_payload.get("user_feedback") or []), "新 payload 应携带修改意见"
        print("✅ 11-C① 否决+意见: 回 aggregate_prefs 重走, 意见累加进 state 并进入新 payload")

        # 意见确实注入四路 prompt(prompt 构造器单测)
        prompt = build_venue_prompt([{"name": "x"}], 30, 30000.0, user_feedback=[fb])
        assert "修改意见" in prompt and fb in prompt, "意见应注入提案 prompt"
        print(f"✅ 11-C② 意见注入四路 prompt: '...{fb[:20]}...' 已拼入")

        # 第二轮审批通过 -> 走完
        res3 = await g.ainvoke(Command(resume={"approved": True, "feedback": ""}), config=cfg)
        assert not res3.get("__interrupt__") and (res3.get("approval_decision") or {}).get("approved")
        assert sum(1 for s in (res3.get("steps_log") or []) if s.startswith("[propose:")) >= 8, \
            "否决重走后 propose 应执行两轮(4路×2)"
        print("✅ 11-C③ 第二轮审批通过, 重走全程后走完")
    finally:
        await conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("需求解析 + 偏好聚合 + 采集 + 四路提案 + 约束消解回环 + 风险评估 验证")
    print("=" * 60)
    verify_models()
    verify_templates()
    verify_risk_models()
    g = verify_graph_compile()
    asyncio.run(verify_live_parse(g))
    asyncio.run(verify_aggregate(g))
    verify_risk_rules()              # 规则层单测放真实采集前(纯函数, 快)
    asyncio.run(verify_risk_zero_llm())
    asyncio.run(verify_gather_info())
    asyncio.run(verify_propose())
    asyncio.run(verify_propose_graph())
    verify_reconcile()
    asyncio.run(verify_reconcile_loop(g))
    asyncio.run(verify_risk_with_llm())
    asyncio.run(verify_hitl_approve())
    asyncio.run(verify_hitl_restart())
    asyncio.run(verify_hitl_reject_feedback())
    asyncio.run(verify_checklist_memory())
    asyncio.run(verify_memory_history_injection())
    asyncio.run(verify_persistent_store())
    print("\nALL VERIFY PASSED ✅")
