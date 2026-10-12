"""输入层测试: 用户输入这一步的校验 / 结构化分流 / team_id 归一

覆盖的都是"用户输入没被检查"这一类缺陷:
- /plan 结构化入参的必填与边界(缺人数/日期/出发地 -> 422, 过去日期 -> 422, 超长 -> 422)
- 日期归一化(斜杠/点/中文/带时间 -> ISO; 缺年份视为抽取失败, 不猜)
- parse_request 的结构化分支**零 LLM 调用**(守住"表单数据不再让 LLM 猜一遍")
- 过去日期在解析层被计入 missing_fields(下游不再拿昨天的日期去查天气/车票)
- _resolve_team_id 只复用服务端签发的不透明 id(手填的不能当越权读取别人记忆的钥匙)

设计原则(与 test_error_handling.py 一致):
- **不触网**: 结构化路径本就不该调 LLM, 这里把 invoke_structured 换成
  一调就炸的假函数, 既省额度又把"零 LLM"变成断言
- 无第三方依赖: 标准库 unittest

运行:
    cd backend && python -m unittest discover -s tests -v
"""

import asyncio
import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes.event import PlanStartBody, _resolve_team_id, router
from app.graph import nodes as nodes_module
from app.models.schemas import EventRequest


def _future(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _valid_plan_body(**overrides) -> dict:
    """一份最小合法 /plan 请求体(结构化), 供边界用例改单个字段"""
    body = {
        "people_count": 30,
        "date": _future(),
        "origin": "上海",
    }
    body.update(overrides)
    return body


class TestPlanBodyValidation(unittest.TestCase):
    """/plan 结构化入参: 必填、日期语义、长度上限"""

    def test_minimal_valid_body_passes(self):
        body = PlanStartBody(**_valid_plan_body())
        self.assertEqual(body.people_count, 30)
        self.assertIsNone(body.budget)
        self.assertIsNone(body.date_end)

    def test_missing_required_fields_are_rejected(self):
        """缺人数/日期/出发地: 旧版是"照跑完整流程", 现在模型层直接拒绝"""
        for field in ("people_count", "date", "origin"):
            with self.subTest(field=field):
                payload = _valid_plan_body()
                payload.pop(field)
                with self.assertRaises(ValidationError) as ctx:
                    PlanStartBody(**payload)
                self.assertIn(field, str(ctx.exception))

    def test_blank_origin_rejected(self):
        """纯空白出发地不是"填了"(min_length 对空白无效, 这里靠长度+strip 语义)"""
        with self.assertRaises(ValidationError):
            PlanStartBody(**_valid_plan_body(origin=""))

    def test_past_date_rejected(self):
        """过去日期: 放行会让天气/火车票去查昨天的日期"""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with self.assertRaises(ValidationError) as ctx:
            PlanStartBody(**_valid_plan_body(date=yesterday))
        self.assertIn("不能早于今天", str(ctx.exception))

    def test_date_end_before_date_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PlanStartBody(**_valid_plan_body(
                date=_future(10), date_end=_future(5)))
        self.assertIn("不能早于", str(ctx.exception))

    def test_multi_day_range_accepted(self):
        body = PlanStartBody(**_valid_plan_body(date=_future(10), date_end=_future(12)))
        self.assertEqual(body.date_end, date.today() + timedelta(days=12))

    def test_oversized_raw_text_rejected(self):
        """原文只作留痕, 但不设上限就能撑爆日志/下游 prompt"""
        with self.assertRaises(ValidationError):
            PlanStartBody(**_valid_plan_body(raw_text="团" * 501))

    def test_too_many_preferences_rejected(self):
        # people_count 放大到上限: 这里要验的是**条数上限**, 别让 headcount 之和的
        # 校验先一步拦下(那样断言的就是另一条规则了)
        pref = {"person_name": "张三", "activity_prefs": ["爬山"]}
        with self.assertRaises(ValidationError):
            PlanStartBody(**_valid_plan_body(people_count=500, preferences=[pref] * 201))
        # 200 条以内正常
        body = PlanStartBody(**_valid_plan_body(people_count=500, preferences=[pref] * 200))
        self.assertEqual(len(body.preferences), 200)

    def test_overlong_person_name_rejected(self):
        with self.assertRaises(ValidationError):
            PlanStartBody(**_valid_plan_body(
                preferences=[{"person_name": "张" * 51}]))


class TestHttpStatusCodes(unittest.TestCase):
    """HTTP 层: 边界输入不该进图(旧版空需求会一路跑到审批闸门)"""

    @classmethod
    def setUpClass(cls):
        # 只挂 event 路由: 绕开 main.py 的 startup 配置校验(无 key 也能测校验层)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        cls.client = TestClient(app)

    def test_plan_without_required_fields_returns_422(self):
        resp = self.client.post("/api/event/plan", json={})
        self.assertEqual(resp.status_code, 422)
        detail = resp.json()["detail"]
        reported = {d["loc"][-1] for d in detail}
        self.assertEqual(reported, {"people_count", "date", "origin"})

    def test_plan_with_past_date_returns_422(self):
        resp = self.client.post("/api/event/plan", json=_valid_plan_body(
            date=(date.today() - timedelta(days=3)).isoformat()))
        self.assertEqual(resp.status_code, 422)

    def test_parse_with_blank_text_returns_400(self):
        """/parse 是自由文本入口: 空白由程序化前置校验挡下"""
        resp = self.client.post("/api/event/parse", json={"raw_text": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["missing_fields"], ["raw_text"])

    def test_parse_with_oversized_text_returns_422(self):
        resp = self.client.post("/api/event/parse", json={"raw_text": "团" * 501})
        self.assertEqual(resp.status_code, 422)


class TestDateNormalization(unittest.TestCase):
    """日期归一: 必须落成 ISO, 否则下游字符串比较静默失效"""

    def test_common_notations_normalized(self):
        cases = {
            "2026-10-01": "2026-10-01",
            "2026/10/01": "2026-10-01",
            "2026.10.01": "2026-10-01",
            "2026年10月1日": "2026-10-01",
            "2026-10-01T09:00:00": "2026-10-01",
            "  2026-10-01  ": "2026-10-01",
        }
        for raw, expect in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    EventRequest(raw_text="x", date=raw).date, expect)

    def test_missing_year_rejected_instead_of_guessed(self):
        """缺年份属于抽取失败: 退回重填, 不能替用户猜一个年份"""
        with self.assertRaises(ValidationError):
            EventRequest(raw_text="x", date="10月1日")

    def test_impossible_date_rejected(self):
        with self.assertRaises(ValidationError):
            EventRequest(raw_text="x", date="2026-02-30")

    def test_empty_date_becomes_none(self):
        self.assertIsNone(EventRequest(raw_text="x", date="").date)
        self.assertIsNone(EventRequest(raw_text="x", date=None).date)


class TestStructuredParseIsLlmFree(unittest.TestCase):
    """parse_request 结构化分支: 零 LLM(表单已有的精确字段不再让模型猜一遍)"""

    def _run(self, state: dict) -> dict:
        # 结构化路径一旦调 LLM 就立刻失败 —— 把"零 LLM"变成断言而不是承诺
        with mock.patch.object(nodes_module, "invoke_structured",
                               side_effect=AssertionError("结构化输入不该调 LLM")), \
             mock.patch.object(nodes_module, "get_chat_model",
                               side_effect=AssertionError("结构化输入不该建模型")):
            return asyncio.run(nodes_module.parse_request(state, store=None))

    def test_structured_input_assembles_without_llm(self):
        out = self._run({"raw_request": {
            "raw_text": "30人团建",
            "team_id": "team_" + "a" * 32,
            "structured": {
                "people_count": 30,
                "date": _future(20),
                "budget": 30000.0,
                "origin": "上海",
                "destination": "杭州",
                "transport_pref": "高铁",
                "activity_type": "团建",
            },
        }})
        parsed = out["parsed_request"]
        self.assertEqual(parsed["people_count"], 30)
        self.assertEqual(parsed["budget"], 30000.0)
        self.assertEqual(parsed["origin"], "上海")
        self.assertEqual(parsed["destination"], "杭州")
        self.assertEqual(parsed["transport_pref"], "高铁")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertNotIn("date", parsed["missing_fields"])

    def test_budget_semantics_kept_verbatim(self):
        """总预算必须原样落地: 前端传的就是总预算, 不该被任何抽取/换算改写"""
        out = self._run({"raw_request": {
            "raw_text": "",
            "team_id": "team_" + "b" * 32,
            "structured": {"people_count": 30, "date": _future(20),
                           "budget": 30000.0, "origin": "上海"},
        }})
        self.assertEqual(out["parsed_request"]["budget"], 30000.0)

    def test_past_date_flagged_as_missing(self):
        """过去日期在解析层被拦下(计入 missing_fields, 路由层据此返回 400)"""
        out = self._run({"raw_request": {
            "raw_text": "上周团建",
            "team_id": "team_" + "c" * 32,
            "structured": {"people_count": 10,
                           "date": (date.today() - timedelta(days=7)).isoformat(),
                           "origin": "上海"},
        }})
        self.assertIn("date", out["missing_fields"])

    def test_structured_input_ignores_unknown_keys(self):
        """未知 key 不该让整条请求崩(上游多传字段时降级为忽略)"""
        out = self._run({"raw_request": {
            "raw_text": "",
            "team_id": "team_" + "d" * 32,
            "structured": {"people_count": 10, "date": _future(5),
                           "origin": "上海", "不存在的字段": 1},
        }})
        self.assertEqual(out["parsed_request"]["people_count"], 10)

    def test_activity_type_falls_back_to_keywords(self):
        """结构化没给活动类型时用关键词兜底(仍不调 LLM)"""
        out = self._run({"raw_request": {
            "raw_text": "30人团建,预算3万",
            "team_id": "team_" + "e" * 32,
            "structured": {"people_count": 30, "date": _future(20), "origin": "上海"},
        }})
        self.assertEqual(out["activity_type"], "团建")

    def test_free_text_still_goes_through_llm(self):
        """自由文本路径保留 LLM 抽取(evals/纯文本入口依赖它), 且注入当前日期"""
        sent = {}

        def fake_invoke_structured(schema, prompt, *a, **kw):
            sent["prompt"] = prompt
            return EventRequest(raw_text="", people_count=20,
                                date=_future(30), origin="北京")

        with mock.patch.object(nodes_module, "invoke_structured",
                               side_effect=fake_invoke_structured):
            out = asyncio.run(nodes_module.parse_request(
                {"raw_request": {"raw_text": "下周末20人团建,北京出发",
                                 "team_id": "team_" + "f" * 32}},
                store=None))

        self.assertEqual(out["parsed_request"]["people_count"], 20)
        # 模板要求按当前时间推断相对日期 -> 必须把服务器当前日期给模型
        self.assertIn(date.today().isoformat(), sent["prompt"])
        self.assertIn("下周末20人团建", sent["prompt"])


class TestStructuredGraphRun(unittest.TestCase):
    """结构化输入跑通真图(轻量门控): 解析零 LLM, 且无偏好 -> 全程零 LLM、零外网

    单测 parse_request 只能证明这个节点不调 LLM; 这里再证明它产出的 state
    在真图里能一路走到终点(入口路由认识 structured、pref_summary/gather_info
    下游都消费得了), 否则"零 LLM"只是节点内的局部事实。
    """

    def test_structured_input_runs_through_real_graph(self):
        from langgraph.checkpoint.memory import InMemorySaver

        from app.graph import build_event_graph
        from app.graph import graph as graph_module

        async def fake_gather_info(state):
            # 采集桩: 本用例只验证输入层到流程编排的接通, 不碰高德
            return {"basic_info": {}, "steps_log": ["[gather_info] 测试桩"]}

        with mock.patch.object(nodes_module, "invoke_structured",
                               side_effect=AssertionError("轻量流程不该调 LLM")), \
             mock.patch.object(nodes_module, "get_chat_model",
                               side_effect=AssertionError("轻量流程不该建模型")), \
             mock.patch.object(graph_module, "gather_info", side_effect=fake_gather_info):
            graph = build_event_graph(checkpointer=InMemorySaver())
            res = asyncio.run(graph.ainvoke(
                {
                    "raw_request": {
                        "raw_text": "30人团建",
                        "team_id": "team_" + "a" * 32,
                        "structured": {"people_count": 30, "date": _future(20),
                                       "budget": 30000.0, "origin": "上海",
                                       "destination": "杭州"},
                    },
                    "generate_plan": False,   # 轻量门控: 不进入四路提案
                },
                config={"configurable": {"thread_id": "t-structured"}},
            ))

        self.assertEqual(res["parsed_request"]["people_count"], 30)
        self.assertEqual(res["parsed_request"]["budget"], 30000.0)
        self.assertEqual(res["parsed_request"]["destination"], "杭州")
        self.assertTrue(
            any(str(s).startswith("[parse_request] 结构化输入") for s in res["steps_log"]),
            f"未走到结构化分支: {res['steps_log']}",
        )
        # 无偏好 -> aggregate_prefs 走空分支, 不该产出冲突点
        self.assertEqual(res["pref_summary"].conflict_points, [])


class TestTeamIdResolution(unittest.TestCase):
    """team_id 只认服务端签发形态: 手填 id 不能当越权钥匙"""

    def test_server_issued_id_is_reused(self):
        issued = "team_" + "0123456789abcdef" * 2
        self.assertEqual(_resolve_team_id(issued), issued)

    def test_handwritten_id_is_replaced(self):
        """手填的团队名(如 'acme-team')换不出别人的记忆, 只能拿到一个新桶"""
        for raw in ("acme-team", "default", "team_acme", "team_ABC", "team_" + "z" * 32):
            with self.subTest(raw=raw):
                got = _resolve_team_id(raw)
                self.assertNotEqual(got, raw)
                self.assertRegex(got, r"^team_[0-9a-f]{32}$")

    def test_missing_id_generates_unique_ones(self):
        """不传 id 的两个人不该共用 ("team","default") 这个桶"""
        self.assertNotEqual(_resolve_team_id(None), _resolve_team_id(""))
        self.assertRegex(_resolve_team_id(None), r"^team_[0-9a-f]{32}$")

    def test_whitespace_is_stripped(self):
        issued = "team_" + "f" * 32
        self.assertEqual(_resolve_team_id(f"  {issued}  "), issued)


if __name__ == "__main__":
    unittest.main(verbosity=2)