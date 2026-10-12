"""团建活动策划 Agent 评测运行器

用法(在 backend 目录下):
    python -m evals.run_evals

每个 golden case 跑完整图(InMemorySaver + InMemoryStore, 审批闸门自动通过), 断言:
1. 预算: total <= budget, 除非 ConflictReport 已上报超支(必然超支用例)
2. 时间线: flow 提案各时段两两不重叠(与 reconcile 同源正则, 程序断言)
3. 防幻觉: venue 提案引用的场地名必须来自 gather_info 真实 POI 候选
4. LLM-as-judge: 便宜模型(默认 LLM_JUDGE_MODEL, 未配置则用主模型)给方案合理性打 1-5 分, >=3 过

输出: 控制台报告(通过率 + 每用例断言明细) + evals/report.json;
有失败时进程退出码非 0(供 CI 使用)。
"""

import asyncio
import io
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

# 控制台强制 UTF-8: Windows 下 stdout 默认是 GBK, 报告里的 ▶/✅ 会直接抛
# UnicodeEncodeError 让整个评测跑不起来(实测)。与 _diag/verify_fixes.py 同一做法。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config  # noqa: F402,E402  触发 load_dotenv

from langgraph.checkpoint.memory import InMemorySaver  # noqa: F402
from langgraph.store.memory import InMemoryStore  # noqa: F402
from langgraph.types import Command  # noqa: F402
from pydantic import BaseModel, Field  # noqa: F402

from app.graph import build_event_graph  # noqa: F402
from app.graph.amap_client import WeatherInfo  # noqa: F402
from app.graph.model import get_chat_model  # noqa: F402
from app.graph.nodes import extract_time_ranges  # noqa: F402,E402  与 reconcile 同源时间解析

EV_DIR = Path(__file__).resolve().parent
CASES_PATH = EV_DIR / "golden_cases.json"
REPORT_PATH = EV_DIR / "report.json"

_RECURSION_LIMIT = 40
_MAX_RESUME_ROUNDS = 3   # 审批闸门最多自动通过 3 次(回炉重走会再次到闸门)


# ============ LLM-as-judge ============

class JudgeVerdict(BaseModel):
    """judge 结构化输出 schema(bind_tools + tool_choice='required', 禁止 with_structured_output)"""
    score: int = Field(..., ge=1, le=5, description="方案合理性评分 1-5(1=不可用, 3=及格, 5=优秀)")
    reason: str = Field(..., description="一句话评分理由, 指出最主要的问题或亮点")


def get_judge_llm():
    """judge 用便宜模型: env LLM_JUDGE_MODEL 优先, 未配置则回退主模型"""
    return get_chat_model(temperature=0.1, disable_thinking=True,
                          model=os.getenv("LLM_JUDGE_MODEL")).bind_tools(
        [JudgeVerdict], tool_choice="required")


_JUDGE_RUBRIC = """评分口径(必须遵守, 避免误判):
1. 本系统为四路并行提案架构(交通/场地/流程/餐饮四路互不可见)。跨类目的口径差异
   (如餐食同时出现在流程与餐饮、场地布置与道具费重叠)属于已知架构限制, 不是一票否决项;
   只要单类目内部报价合理、时间线无重叠、场地来自真实候选, 不因跨类目重复计费判 1-2 分。
2. over_budget 为负数表示结余(预算内), 不是"口径不一致"。
3. 若输入预算明显不足(必然超支用例): 评"是否诚实上报冲突 + 给出最大努力的调整与建议",
   超支本身不是罪; 餐饮按预算缩减并明确说明属于合理取舍。
4. 1-2 分只留给: 编造不存在的场地、报价明显离谱、时间线自相矛盾、方案未覆盖任何成员偏好。
5. 全天团建行程(含晚宴)排到 21:00+ 属正常安排, 不算问题。
"""


_JUDGE_RUBRIC_OVERBUDGET = """本次为"必然超支"评审模式: 输入预算明显不足(人均仅约100元),
方案在数学上不可能既覆盖 30 人又不超支。评分对象是**流程行为与交付质量**, 用锚点计分(不要整体印象打分):
- 基准 3 分: ConflictReport 如实反映超支(over_budget 与四路合计-预算一致) + 时间线无重叠 + 场地来自真实候选
- +1 分: 各路报价已按预算压力明显压缩(如餐饮人均 <= 人均预算)
- -1 分: 方案文本明显自相矛盾(声称某个数字但实际报价是另一个数字)
- -1 分: 完全未考虑预算约束且无任何说明(如人均餐饮远超人均预算)
注意: 餐饮覆盖人数少于总人数(预算不足所致)且明确说明取舍, 属合理行为不扣分;
超支本身已在 ConflictReport 中如实上报并交人工裁决, 不扣分。
"""


async def judge_plan(state: dict, mode: Optional[str] = None) -> Tuple[int, str]:
    """LLM-as-judge: 评最终方案合理性(1-5 分); mode=over_budget 时评流程行为"""
    proposals = state.get("proposals") or {}
    parsed = state.get("parsed_request") or {}
    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}
    risk = state.get("risk_report") or {}
    report = state.get("conflict_report") or {}

    rubric = _JUDGE_RUBRIC_OVERBUDGET if mode == "over_budget" else _JUDGE_RUBRIC
    prompt = f"""你是团建活动方案的评审专家。请对以下方案打 1-5 分(合理性: 预算分配/时间安排/是否照顾成员偏好/可执行性)。

{rubric}
活动需求: 人数={parsed.get('people_count')}, 日期={parsed.get('date')}, 预算={parsed.get('budget')}元
成员偏好冲突点: {json.dumps(pref.get('conflict_points') or [], ensure_ascii=False)}
程序校验结论: {json.dumps(report, ensure_ascii=False)}
风险评估: {json.dumps({'level': risk.get('level'), 'summary': risk.get('summary')}, ensure_ascii=False)}

最终方案(四路报价):
{json.dumps(proposals, ensure_ascii=False, indent=2)}

评分参考: 3=可执行但有明显可改进处; 5=可直接落地。只依据上面材料, 不得编造。"""
    llm = get_judge_llm()
    resp = await asyncio.to_thread(llm.invoke, prompt)
    calls = resp.tool_calls or []
    if not calls:
        return 0, "judge 未返回结构化结果"
    v = JudgeVerdict(**calls[0]["args"])
    return v.score, v.reason


# ============ 程序断言 ============

# 视为"服务性费用"(不指向具体场地)的词根: venue 明细里允许出现, 不算幻觉
_GENERIC_FEE_TOKENS = (
    "场地", "布置", "门票", "设备", "器材", "教练", "保险", "水电", "服务费",
    "物料", "音响", "押金", "清洁", "耗材", "餐", "饮", "住宿", "车", "油费",
    "过路费", "停车", "工作", "人员", "备用", "杂费", "策划", "主持", "道具",
    "食材", "炉具", "烧烤", "茶歇", "饮用水", "横幅", "礼品", "奖品", "药",
    # 免费开放型场地(公园/广场)的常见计费口径: 草坪/广场使用、活动区占用等
    "草坪", "广场", "活动区", "空地", "露营", "帐篷", "导览", "解说",
)


def check_budget(state: dict, expect: dict) -> Dict[str, Any]:
    """预算断言: total <= budget, 除非 ConflictReport 已上报超支"""
    proposals = state.get("proposals") or {}
    budget = (state.get("parsed_request") or {}).get("budget")
    total = round(sum(float((v or {}).get("total_cost") or 0) for v in proposals.values()), 2)
    if budget is None:
        return {"name": "预算不超", "passed": True, "detail": "未提供预算, 跳过"}
    report = state.get("conflict_report") or {}
    reported = (report.get("over_budget") or 0) > 0 or bool(report.get("conflicts"))
    if total <= float(budget):
        return {"name": "预算不超", "passed": True,
                "detail": f"四路合计{total}元 <= 预算{budget}元"}
    if expect.get("over_budget_allowed") and reported:
        return {"name": "预算不超", "passed": True,
                "detail": f"超支{total - budget:.0f}元但 ConflictReport 已上报(必然超支用例)"}
    return {"name": "预算不超", "passed": False,
            "detail": f"四路合计{total}元 > 预算{budget}元, 且 ConflictReport 未如实上报: {report}"}


def check_timeline(state: dict) -> Dict[str, Any]:
    """时间线断言: flow 各时段两两不重叠(解析不出时间的项跳过)

    与 reconcile 共用 extract_time_ranges(遍历全部匹配 + 跳过日期片段),
    避免 "2026-10-18 09:00-12:00" 被当成 26:00-10:00 而漏检重叠。
    """
    flow = (state.get("proposals") or {}).get("flow") or {}
    ranges: List[Tuple[int, int, str]] = []
    for it in flow.get("items") or []:
        text = f"{it.get('name') or ''} {it.get('note') or ''}"
        for start, end, _ in extract_time_ranges(text):
            ranges.append((start, end, it.get("name") or "?"))

    def _fmt(mins: int) -> str:
        return f"{mins // 60:02d}:{mins % 60:02d}"

    overlaps = []
    for i in range(len(ranges)):
        for j in range(i + 1, len(ranges)):
            s1, e1, n1 = ranges[i]
            s2, e2, n2 = ranges[j]
            if s1 < e2 and s2 < e1:
                overlaps.append(f"[{n1} {_fmt(s1)}-{_fmt(e1)}] × [{n2} {_fmt(s2)}-{_fmt(e2)}]")
    if not ranges:
        return {"name": "时间线不重叠", "passed": True, "detail": "flow 未解析出时段, 跳过"}
    if overlaps:
        return {"name": "时间线不重叠", "passed": False, "detail": f"重叠: {'; '.join(overlaps)}"}
    return {"name": "时间线不重叠", "passed": True,
            "detail": f"{len(ranges)}个时段两两不重叠: " + ", ".join(
                f"{_fmt(s)}-{_fmt(e)}" for s, e, _ in ranges)}


def _venue_short_forms(name: str) -> List[str]:
    """候选场地名的常见引用形态: 全名 + 分隔符前短名 + 前4字

    LLM 提案里常引用简称(如"愚农营地"代"愚农营地·团建拓展露营烧烤(嘉定愚农庄园店)")。
    """
    forms = {name, name[:4]}
    for sep in "·(（-—/":
        idx = name.find(sep)
        if idx >= 3:
            forms.add(name[:idx])
    return [f for f in forms if len(f) >= 3]


def check_venue_names(state: dict) -> Dict[str, Any]:
    """防幻觉断言: venue 提案引用的场地名必须来自 gather_info 真实 POI 候选"""
    venue = (state.get("proposals") or {}).get("venue") or {}
    candidates = [v.get("name") or "" for v in ((state.get("basic_info") or {}).get("venues") or [])]
    candidates = [c for c in candidates if c]
    if not venue or not candidates:
        return {"name": "场地名来自真实候选", "passed": True, "detail": "无 venue 提案或无候选, 跳过"}

    all_text = json.dumps(venue, ensure_ascii=False)
    referenced = [c for c in candidates
                  if any(f in all_text for f in _venue_short_forms(c))]
    suspicious: List[str] = []
    for it in venue.get("items") or []:
        name = it.get("name") or ""
        if any(f in name or (len(name) >= 4 and name in c)
               for c in candidates for f in _venue_short_forms(c)):
            continue
        if not any(t in name for t in _GENERIC_FEE_TOKENS):
            suspicious.append(name)

    if suspicious:
        return {"name": "场地名来自真实候选", "passed": False,
                "detail": f"疑似幻觉条目: {suspicious}; 真实候选池: {candidates[:5]}..."}
    if not referenced:
        return {"name": "场地名来自真实候选", "passed": False,
                "detail": f"venue 提案未引用任何真实候选(候选池: {candidates[:5]}...)"}
    return {"name": "场地名来自真实候选", "passed": True,
            "detail": f"引用真实候选: {referenced[0]}" + (f" 等{len(referenced)}个" if len(referenced) > 1 else "")}


def check_expectations(state: dict, expect: dict) -> List[Dict[str, Any]]:
    """用例 expect 声明的附加断言(风险/冲突点等)"""
    checks: List[Dict[str, Any]] = []
    risk = state.get("risk_report") or {}
    pref = state.get("pref_summary")
    if pref is not None and not isinstance(pref, dict):
        pref = pref.model_dump()
    pref = pref or {}
    report = state.get("conflict_report") or {}

    if "risk_flags_expected" in expect:
        flags = risk.get("flags") or []
        ok = bool(flags) == bool(expect["risk_flags_expected"])
        # 标记是结构化的: 展示规则来源, 而不是把整条 dict 糊上去
        brief = [f"{f.get('rule')}({f.get('severity')})" for f in flags] or "空"
        checks.append({"name": f"风险标记(期望{'有' if expect['risk_flags_expected'] else '无'})",
                       "passed": ok, "detail": f"flags={brief}"})
    if "risk_level_expected" in expect:
        ok = risk.get("level") == expect["risk_level_expected"]
        checks.append({"name": f"风险等级(期望{expect['risk_level_expected']})",
                       "passed": ok,
                       "detail": f"level={risk.get('level')}, summary={risk.get('summary')}"})
    if expect.get("risk_no_unevaluated"):
        # 规则执行表: 断言没有"未评估"的规则(除温度/风力那条占位)
        gaps = [r for r in (risk.get("rules") or [])
                if r.get("status") == "not_evaluated" and r.get("rule") != "temperature_wind"]
        gap_txt = [f"{g.get('name')}({g.get('reason')})" for g in gaps] or "无"
        checks.append({"name": "风险规则均已评估", "passed": not gaps,
                       "detail": f"未评估: {gap_txt}"})
    if expect.get("risk_alternatives_expected"):
        alts = risk.get("alternatives") or []
        checks.append({"name": "风险备选生成(带报价)", "passed": len(alts) >= 1,
                       "detail": f"{len(alts)}条备选"})
    if expect.get("conflict_points_expected"):
        cps = pref.get("conflict_points") or []
        checks.append({"name": "偏好冲突点检出", "passed": len(cps) >= 1,
                       "detail": f"{len(cps)}条: {cps[:2]}"})
    if expect.get("conflict_report_expected"):
        has = bool(report.get("conflicts")) or (report.get("over_budget") or 0) > 0
        checks.append({"name": "ConflictReport 上报", "passed": has,
                       "detail": f"over_budget={report.get('over_budget')}, conflicts={report.get('conflicts')}"})
    return checks


# ============ 用例执行 ============

def _install_weather_mock(mock: dict):
    """临时替换 get_weather 为固定预报(评测雨天场景, 不依赖真实天气)

    mock 支持 dates 数组(覆盖 LLM 年份解析不稳定, 如 10月31日 可能被解析为
    2025/2026 两个年份)或单数 date。
    """
    import app.graph.amap_client as amap_client

    dates = mock.get("dates") or [mock["date"]]

    def fake_get_weather(city: str) -> List[WeatherInfo]:
        return [WeatherInfo(date=d, day_weather=mock.get("day_weather", "小雨"),
                            night_weather=mock.get("night_weather", "阴"),
                            day_temp=mock.get("day_temp", 18), night_temp=mock.get("night_temp", 12),
                            wind_direction="东风", wind_power="3级")
                for d in dates]

    original = amap_client.get_weather
    amap_client.get_weather = fake_get_weather
    return original


async def run_full_case(case: dict) -> Dict[str, Any]:
    """full_flow 用例: 跑完整图, 审批闸门自动通过, 收集断言 + judge 评分"""
    restore = None
    if case.get("mock_weather"):
        restore = _install_weather_mock(case["mock_weather"])
    try:
        g = build_event_graph(checkpointer=InMemorySaver(), store=InMemoryStore())
        cfg = {"configurable": {"thread_id": str(uuid.uuid4())},
               "recursion_limit": _RECURSION_LIMIT}
        inp: Dict[str, Any] = {
            "raw_request": {"raw_text": case["input"]["raw_text"],
                            "team_id": case["input"].get("team_id")},
            "generate_plan": True,
        }
        if case["input"].get("preferences"):
            inp["preferences"] = case["input"]["preferences"]

        res = await g.ainvoke(inp, config=cfg)
        resumes = 0
        while res.get("__interrupt__") and resumes < _MAX_RESUME_ROUNDS:
            res = await g.ainvoke(Command(resume={"approved": True, "feedback": ""}),
                                  config=cfg)
            resumes += 1
    finally:
        if restore is not None:
            import app.graph.amap_client as amap_client
            amap_client.get_weather = restore

    checks: List[Dict[str, Any]] = []
    proposals = res.get("proposals") or {}
    checks.append({"name": "四路提案产出", "passed": set(proposals.keys()) == {"transport", "venue", "flow", "food"},
                   "detail": f"keys={sorted(proposals.keys())}"})

    if proposals:
        checks.append(check_budget(res, case.get("expect") or {}))
        checks.append(check_timeline(res))
        checks.append(check_venue_names(res))
        checks.extend(check_expectations(res, case.get("expect") or {}))

        score, reason = await judge_plan(res, mode=(case.get("expect") or {}).get("judge_mode"))
        min_score = (case.get("expect") or {}).get("judge_min_score", 3)
        checks.append({"name": f"LLM-judge 合理性>={min_score}分", "passed": score >= min_score,
                       "detail": f"{score}分: {reason}"})

    return {"id": case["id"], "name": case["name"], "passed": all(c["passed"] for c in checks),
            "checks": checks}


async def run_parse_only_case(case: dict) -> Dict[str, Any]:
    """parse_only 用例: 轻量流程(不进提案), 断言缺失字段"""
    g = build_event_graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 10}
    res = await g.ainvoke(
        {"raw_request": {"raw_text": case["input"]["raw_text"],
                         "team_id": case["input"].get("team_id")}},
        config=cfg,
    )
    missing = res.get("missing_fields") or []
    required = (case.get("expect") or {}).get("missing_fields_required") or []
    missed = [f for f in required if f not in missing]
    checks = [
        {"name": f"缺失字段含 {required}", "passed": not missed,
         "detail": f"实际 missing_fields={missing}" + (f"; 漏判: {missed}" if missed else "")},
        {"name": "不进入提案阶段", "passed": not (res.get("proposals") or {}),
         "detail": f"proposals={'空' if not res.get('proposals') else res.get('proposals', {}).keys()}"},
    ]
    return {"id": case["id"], "name": case["name"], "passed": all(c["passed"] for c in checks),
            "checks": checks}


async def main() -> int:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    print(f"{'=' * 64}\n团建活动策划 Agent 评测: {len(cases)} 个用例\n{'=' * 64}")

    results: List[Dict[str, Any]] = []
    for case in cases:
        print(f"\n▶ 用例 {case['id']} {case['name']}")
        try:
            if case["type"] == "full_flow":
                result = await run_full_case(case)
            elif case["type"] == "parse_only":
                result = await run_parse_only_case(case)
            else:
                result = {"id": case["id"], "name": case["name"], "passed": False,
                          "checks": [{"name": "用例类型", "passed": False,
                                      "detail": f"未知 type: {case['type']}"}]}
        except Exception as e:
            result = {"id": case["id"], "name": case["name"], "passed": False,
                      "checks": [{"name": "执行异常", "passed": False, "detail": f"{type(e).__name__}: {e}"}]}
        results.append(result)
        mark = "✅ PASS" if result["passed"] else "❌ FAIL"
        print(f"  {mark} {result['id']}")
        for c in result["checks"]:
            print(f"     {'✅' if c['passed'] else '❌'} {c['name']}: {c['detail']}")

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = [r for r in results if not r["passed"]]
    print(f"\n{'=' * 64}")
    print(f"评测报告: 通过率 {passed}/{total} ({passed / total * 100:.0f}%)")
    if failed:
        print("失败用例明细:")
        for r in failed:
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"  ❌ {r['id']} {c['name']}: {c['detail']}")
    print(f"{'=' * 64}")

    REPORT_PATH.write_text(
        json.dumps({"run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "pass_rate": f"{passed}/{total}", "results": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"报告已写入: {REPORT_PATH}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
