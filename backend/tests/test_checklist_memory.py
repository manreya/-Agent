"""任务清单与团队记忆测试: 读写往返 / 记录身份 / 措辞与事实 / 兜底与校验

覆盖这一轮改造里几条**会改变行为**的口径:
- 记忆的两条解析路径都要读(结构化路径是产品实际走的那条, 却从来没读过)
- 记录键 = 业务身份(活动日), 同一场活动重跑只留一条; 库里的旧格式重复记录读时合掉
- 历史记录里计划阶段的钱不能叫"总花费"; 预算正好打平要照实说, 不能当没数据
- 清单: 采购项的归属由程序规范化(认不出置空), 费用是估算这一事实由程序填
- 清单: "不超该路报价"从一条模型指令变成程序校验, 结果摆在清单里而不是只进日志
- 清单: 模型那一次失败要有程序兜底, 且兜底内容只含方案里已有的事实
- 记忆写入的结果要说出来(成功/降级放行两种分支不能长得一样), 并随交付物一起送到页面

运行:
    cd backend && py -3.13 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest import mock

from langgraph.store.memory import InMemoryStore

from app.graph import nodes as nodes_module
from app.graph.nodes import (
    _HISTORY_RETRO_CAP, _dedupe_retros, _fallback_checklist, _finalize_checklist,
    _load_team_history, checklist, memory, parse_request,
)
from app.prompts.templates import build_history_context_block


# ---------- 构造用的小工具 ----------

def _plan(date="2026-10-18", people=30, budget=30000.0, total=27000.0) -> dict:
    """一份足以跑通"写记忆"的状态(报价合计可控, 便于测预算偏差)"""
    return {
        "team_id": "team-1",
        "parsed_request": {"date": date, "people_count": people, "budget": budget},
        "proposals": {
            "transport": {"category": "transport", "total_cost": total * 0.3,
                          "items": [{"name": "大巴包车", "cost": total * 0.3}]},
            "food": {"category": "food", "total_cost": total * 0.7,
                     "items": [{"name": "农家菜", "cost": total * 0.7}]},
        },
        "pref_summary": {"people_count": people, "fitness_distribution": {"2": 5},
                         "dietary_all": ["素食"], "activity_votes": {"爬山": 10}},
        "approval_decision": {"approved": True},
        "conflict_report": {},
        "replan_count": 0,
    }


def _retro(activity_date, recorded_at, plan_total=100.0, dev=None) -> dict:
    out = {"stage": "planned", "activity_date": activity_date,
           "plan_total_cost": plan_total, "recorded_at": recorded_at}
    if dev is not None:
        out["plan_budget_deviation"] = dev
    return out


def _run_checklist(state) -> dict:
    return asyncio.run(checklist(state))


class TestMemoryWriteIdentity(unittest.TestCase):
    """记录的身份是"哪一次活动", 不是"什么时候写的" """

    def _store(self) -> InMemoryStore:
        return InMemoryStore()

    def test_key_is_activity_date_not_timestamp(self):
        store = self._store()
        asyncio.run(memory(_plan(date="2026-10-18"), store=store))
        keys = sorted(i.key for i in asyncio.run(store.asearch(("team", "team-1"))))
        self.assertEqual(keys, ["profile", "retro_2026-10-18"])

    def test_rerun_of_same_activity_overwrites(self):
        """同一场活动重跑: 记录被覆盖, 不是又长出一条无法分辨的"""
        store = self._store()
        asyncio.run(memory(_plan(total=27000.0), store=store))
        asyncio.run(memory(_plan(total=24000.0), store=store))
        retros = [i for i in asyncio.run(store.asearch(("team", "team-1")))
                  if i.key.startswith("retro")]
        self.assertEqual(len(retros), 1, "同一活动日只该留一条记录")
        self.assertEqual(retros[0].value["plan_total_cost"], 24000.0,
                         "留下的应该是最后写进去的那份")

    def test_record_says_it_is_a_plan_not_a_spend(self):
        """记录写于活动发生之前: 必须自我说明"这是计划", 免得被读成消费记录"""
        store = self._store()
        asyncio.run(memory(_plan(), store=store))
        retro = asyncio.run(store.aget(("team", "team-1"), "retro_2026-10-18")).value
        self.assertEqual(retro["stage"], "planned")
        self.assertIn("plan_total_cost", retro)
        self.assertNotIn("total_cost", retro)

    def test_zero_deviation_is_recorded_not_dropped(self):
        """正好打平 = 0, 与"没给预算"= None 是两件事, 不能都是"没有值" """
        store = self._store()
        state = _plan(budget=27000.0, total=27000.0)
        asyncio.run(memory(state, store=store))
        retro = asyncio.run(store.aget(("team", "team-1"), "retro_2026-10-18")).value
        self.assertEqual(retro["plan_budget_deviation"], 0.0)
        self.assertIsNotNone(retro["plan_budget_deviation"])

    def test_no_store_skips_gracefully(self):
        out = asyncio.run(memory(_plan(), store=None))
        self.assertEqual(out["steps_log"], ["[memory] 无 Store, 跳过团队记忆写入"])
        self.assertFalse(out["memory_status"]["written"], "没写成也要如实说没写成")

    def test_success_reports_what_was_written(self):
        """成功分支要能说出写到了哪个键: 交付页拿它当"已存档"的凭据"""
        store = self._store()
        out = asyncio.run(memory(_plan(date="2026-10-18"), store=store))
        self.assertTrue(out["memory_status"]["written"])
        self.assertEqual(out["memory_status"]["retro_key"], "retro_2026-10-18")
        self.assertEqual(out["memory_status"]["activity_date"], "2026-10-18")

    def test_write_failure_is_reported_not_swallowed(self):
        """写入失败降级放行 —— 但结论必须是"没写成", 不能与成功一个样"""
        class _BrokenStore(InMemoryStore):
            async def aput(self, *_a, **_k):
                raise RuntimeError("disk full")

        out = asyncio.run(memory(_plan(), store=_BrokenStore()))
        status = out["memory_status"]
        self.assertFalse(status["written"])
        self.assertEqual(status["reason"], "write_failed")
        self.assertIn("disk full", status["error"], "原因要带上, 否则排查只能靠翻日志")


class TestMemoryStatusReachesTheDeliveredPlan(unittest.TestCase):
    """结论要从节点一路走到交付物: 中间断在哪一环, 页面就又得猜"""

    def _final_state(self, written: bool) -> dict:
        return {
            "proposals": {"transport": {"category": "transport", "total_cost": 1.0, "items": []}},
            "pref_summary": None,
            "conflict_report": {},
            "risk_report": {},
            "parsed_request": {},
            "memory_status": {"written": written, "reason": None if written else "write_failed"},
        }

    def test_status_is_carried_into_event_plan(self):
        from app.api.routes.event import _event_plan_from_state

        ok = _event_plan_from_state(self._final_state(True))
        self.assertTrue(ok.memory_status["written"])
        bad = _event_plan_from_state(self._final_state(False))
        self.assertFalse(bad.memory_status["written"], "降级放行必须随交付物一起说出去")


class TestMemoryReadOnBothPaths(unittest.TestCase):
    """读取必须发生在**产品实际走的那条路**上

    这条是这轮最要紧的回归: 读取过去只挂在自由文本路径, 而前端表单走的
    结构化路径在读取之前就返回了 —— 记忆写了一个多月, 一次都没被读过。
    """

    def setUp(self):
        self.store = InMemoryStore()
        asyncio.run(memory(_plan(), store=self.store))

    def test_structured_path_reads_history(self):
        out = asyncio.run(parse_request({
            "raw_request": {
                "team_id": "team-1",
                "structured": {"people_count": 30, "date": "2026-11-20", "origin": "上海"},
            },
        }, store=self.store))
        history = out.get("team_history")
        self.assertIsNotNone(history, "结构化路径也必须读到团队历史")
        self.assertEqual(history["profile"]["people_count"], 30)
        self.assertEqual([r["activity_date"] for r in history["retros"]], ["2026-10-18"])

    def test_text_path_still_injects_into_prompt(self):
        """文本路径的老行为不许在重构里丢: 历史要进解析提示词"""
        seen = {}

        class _Fake:
            missing_fields = []
            activity_type = "团建"
            date = "2026-11-20"
            date_end = None
            people_count = 30
            budget = None
            origin = "上海"
            raw_text = ""
            destination = None

            def model_dump(self):
                return {"people_count": 30}

        def fake_invoke(_schema, prompt):
            seen["prompt"] = prompt
            return _Fake()

        with mock.patch.object(nodes_module, "invoke_structured", side_effect=fake_invoke):
            out = asyncio.run(parse_request({
                "raw_request": {"team_id": "team-1", "raw_text": "11月20日上海团建30人"},
            }, store=self.store))
        self.assertIn("团队历史记忆", seen["prompt"], "历史上下文块必须进了提示词")
        self.assertIsNotNone(out.get("team_history"))

    def test_structured_path_without_store_is_none(self):
        out = asyncio.run(parse_request({
            "raw_request": {
                "team_id": "team-x",
                "structured": {"people_count": 5, "date": "2026-11-20", "origin": "上海"},
            },
        }, store=None))
        self.assertIsNone(out.get("team_history"))

    def test_history_read_failure_does_not_break_parse(self):
        """读历史失败只该留日志: 它只影响"参考上次习惯", 不该让本次策划失败"""
        class _Broken:
            async def aget(self, *_a, **_k):
                raise RuntimeError("store down")

            async def asearch(self, *_a, **_k):
                raise RuntimeError("store down")

        out = asyncio.run(parse_request({
            "raw_request": {
                "team_id": "team-1",
                "structured": {"people_count": 30, "date": "2026-11-20", "origin": "上海"},
            },
        }, store=_Broken()))
        self.assertEqual(out["parsed_request"]["people_count"], 30)


class TestHistoryRecordHygiene(unittest.TestCase):
    """读回来的时候: 旧格式重复记录合掉、只留最近几条、按时间倒序"""

    def test_legacy_duplicates_collapse_to_newest(self):
        old = _retro("2026-10-18", "2026-10-01 10:00:00", plan_total=100.0)
        new = _retro("2026-10-18", "2026-10-02 10:00:00", plan_total=24000.0)
        kept = _dedupe_retros([old, new])
        self.assertEqual(len(kept), 1, "同一活动日的旧记录要合掉")
        self.assertEqual(kept[0]["plan_total_cost"], 24000.0)

    def test_records_without_activity_date_are_kept(self):
        """说不清是哪次活动的记录不参与去重, 但也不能凭空丢掉"""
        kept = _dedupe_retros([_retro("2026-10-18", "2026-10-01 10:00:00"),
                               {"recorded_at": "2026-10-02 10:00:00", "plan_total_cost": 5}])
        self.assertEqual(len(kept), 2)

    def test_newest_records_win_over_the_cap(self):
        """超过上限时丢的是最旧的, 不是最新的(limit 截断不能替我们决定"最近几条")"""
        store = InMemoryStore()
        for i in range(1, _HISTORY_RETRO_CAP + 4):
            asyncio.run(store.aput(("team", "t"), f"retro_2026-10-{i:02d}",
                                   _retro(f"2026-10-{i:02d}", f"2026-10-{i:02d} 09:00:00")))
        history = asyncio.run(_load_team_history(store, "t"))
        dates = [r["activity_date"] for r in history["retros"]]
        self.assertEqual(len(dates), _HISTORY_RETRO_CAP)
        self.assertEqual(dates, sorted(dates, reverse=True), "必须按时间倒序, 不能是存储顺序")
        self.assertIn("2026-10-07", dates)
        self.assertNotIn("2026-10-01", dates)


class TestHistoryWording(unittest.TestCase):
    """历史记录是提示词材料: 措辞必须与事实一致(计划 ≠ 已花)"""

    def test_plan_money_is_not_called_spend(self):
        block = build_history_context_block(None, [_retro("2026-10-18", "x", plan_total=27000.0)])
        self.assertIn("计划报价合计27000元", block)
        self.assertIn("无实际花费记录", block)
        self.assertNotIn("总花费", block)

    def test_latest_record_is_not_labelled_earlier(self):
        block = build_history_context_block(
            {"people_count": 30, "last_activity_date": "2026-10-18"},
            [_retro("2026-10-18", "x"), _retro("2026-09-01", "y")])
        # 每条记录都带上自己的活动日, 读的人能看出"上次你们"说的是哪一场
        self.assertIn("最近一次活动(活动日2026-10-18)", block)
        self.assertIn("活动日2026-10-18的计划", block)
        self.assertIn("活动日2026-09-01的计划", block)
        self.assertIn("活动日2026-09-01", block)

    def test_zero_deviation_is_stated(self):
        block = build_history_context_block(None, [_retro("2026-10-18", "x", dev=0.0)])
        self.assertIn("正好打平", block)

    def test_missing_budget_says_nothing_about_deviation(self):
        """没给预算 ≠ 打平: 一个字都不要提"""
        block = build_history_context_block(None, [_retro("2026-10-18", "x")])
        self.assertNotIn("打平", block)
        self.assertNotIn("超预算", block)
        self.assertNotIn("结余", block)

    def test_legacy_record_fields_still_render(self):
        """库里旧格式(字段名 total_cost/budget_deviation)必须照样读得出来"""
        block = build_history_context_block(None, [{
            "activity_date": "2026-09-01", "total_cost": 1200.0, "budget_deviation": 200.0}])
        self.assertIn("计划报价合计1200元", block)
        self.assertIn("计划超预算200元", block)


class TestChecklistFinalize(unittest.TestCase):
    """程序收口: 归属规范化 / 估算标记由代码填 / 报价校验真的跑起来"""

    PROPOSALS = {"transport": {"total_cost": 1000.0}, "food": {"total_cost": 2000.0}}

    def _finalize(self, purchases):
        return _finalize_checklist({"purchase_list": purchases}, self.PROPOSALS)

    def test_unknown_category_is_cleared_and_reported(self):
        data = self._finalize([{"item": "横幅", "est_cost": 100, "category": "不知道"}])
        self.assertIsNone(data["purchase_list"][0]["category"], "认不出的归属不许猜一个")
        self.assertTrue(any("未参与" in w for w in data["warnings"]))

    def test_estimate_flag_is_filled_by_code(self):
        """模型就算声明"这个价是问来的", 程序也不采信 —— 清单里的钱从没被询过价"""
        data = self._finalize([{"item": "大巴", "est_cost": 900, "category": "transport",
                                "est_cost_is_estimate": False}])
        self.assertTrue(data["purchase_list"][0]["est_cost_is_estimate"])

    def test_over_quote_is_caught_with_numbers(self):
        data = self._finalize([{"item": "大巴", "est_cost": 1300, "category": "transport"}])
        joined = " ".join(data["warnings"])
        self.assertIn("超过该路报价", joined)
        self.assertIn("1300", joined)   # 超了多少要能看出来, 不能只说"超了"
        self.assertIn("300", joined)

    def test_within_quote_has_no_warning(self):
        data = self._finalize([{"item": "大巴", "est_cost": 900, "category": "transport"}])
        self.assertEqual(data["warnings"], [])

    def test_empty_purchase_list_is_reported(self):
        data = self._finalize([])
        self.assertTrue(any("采购清单为空" in w for w in data["warnings"]))

    def test_warnings_from_fallback_survive(self):
        """兜底时写下的说明不能被校验警告挤掉"""
        data = _finalize_checklist(
            {"purchase_list": [], "degraded": True, "warnings": ["模型生成失败"]},
            self.PROPOSALS)
        self.assertIn("模型生成失败", data["warnings"])
        self.assertTrue(data["degraded"])


class TestChecklistFallback(unittest.TestCase):
    """模型那次失败之后, 清单不能是空的"""

    def test_fallback_only_uses_facts_from_the_plan(self):
        fb = _fallback_checklist({
            "parsed_request": {"date": "2026-10-18", "people_count": 30, "origin": "上海"},
            "pref_summary": {"dietary_all": ["素食"], "people_count": 30},
            "proposals": {"transport": {"items": [{"name": "大巴包车", "cost": 3000}]}},
        }, "RuntimeError: boom")
        self.assertTrue(fb["degraded"])
        self.assertEqual(fb["purchase_list"][0]["item"], "大巴包车")
        self.assertEqual(fb["purchase_list"][0]["category"], "transport")
        self.assertIn("2026-10-18", fb["notice_text"])
        self.assertIn("30 人", fb["notice_text"])
        self.assertIn("素食", fb["notice_text"])
        self.assertTrue(fb["assignments"], "分工表不能为空")
        self.assertTrue(all(a["owner"] == "待指定" for a in fb["assignments"]),
                        "不许编造具体人名")

    def test_fallback_invents_no_time_when_date_missing(self):
        fb = _fallback_checklist({"parsed_request": {"people_count": 30}, "proposals": {}}, "boom")
        self.assertNotIn("时间:", fb["notice_text"])
        self.assertIn("请回复确认参加", fb["notice_text"])


class TestChecklistNode(unittest.TestCase):
    """节点级: 模型给了什么就核什么, 模型崩了就兜"""

    STATE = {
        "parsed_request": {"date": "2026-10-18", "people_count": 30, "budget": 5000},
        "pref_summary": {"dietary_all": [], "people_count": 30},
        "proposals": {"transport": {"category": "transport", "total_cost": 1000.0,
                                    "items": [{"name": "大巴包车", "cost": 1000.0}]}},
    }

    def test_model_output_is_finalized(self):
        class _Fake:
            def model_dump(self):
                return {"assignments": [{"role": "物资采购", "owner": "待指定"},
                                        {"role": "物资采购", "owner": "待指定"}],
                        "purchase_list": [{"item": "大巴", "est_cost": 5000,
                                           "category": "transport"}],
                        "notice_text": "通知"}

        with mock.patch.object(nodes_module, "invoke_structured", return_value=_Fake()):
            out = _run_checklist(self.STATE)
        data = out["checklist"]
        self.assertFalse(data["degraded"])
        self.assertTrue(data["purchase_list"][0]["est_cost_is_estimate"])
        self.assertTrue(any("超过该路报价" in w for w in data["warnings"]))
        self.assertTrue(any("警告" in s for s in out["steps_log"]),
                        "警告要进步骤日志, 不能只留在响应体里")

    def test_model_failure_degrades_instead_of_raising(self):
        """用户已经点过"通过"了: 这里失败不能空手而归"""
        with mock.patch.object(nodes_module, "invoke_structured",
                               side_effect=RuntimeError("模型超时")):
            out = _run_checklist(self.STATE)
        data = out["checklist"]
        self.assertTrue(data["degraded"])
        self.assertTrue(data["purchase_list"], "兜底清单必须仍有采购项")
        self.assertTrue(any("程序兜底" in s for s in out["steps_log"]))


if __name__ == "__main__":
    unittest.main()