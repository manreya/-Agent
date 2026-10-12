# -*- coding: utf-8 -*-
"""离线单元测试: 四路提案的切片契约、失败隔离、回炉纪律与餐饮路的统一纪律

锁住五件"改坏了不一定报错"的事:

1. **一路失败不该废掉另外三路**(`_proposal_node`)。
   四路是同一次 Send 扇出, 旧行为里谁抛异常整批就带着异常结束 —— 已经跑完的
   三路(以及它们花掉的真金白银)全部作废。但降级**必须有边界**: 必需配置缺失
   和程序缺陷都得原样抛出, 否则"字段名写错"会被抹成"这维没数据" —— 那正是
   errors.py 开头第 3 条要修的病。**这条边界是本文件最要紧的断言。**

2. **降级提案要说"失败", 不能说成"没有合适的选项"**。
   两者在人眼里是完全不同的结论(一个该重试, 一个是正常结论)。文案混同不会
   报错, 只会让人拿着"本方案不含餐饮"当"这一带没餐厅"读。

3. **餐饮路的调用纪律与其余三路一致**: 走统一的调用入口(次数/退避/总时长),
   模型给的参数当场校验(而不是拖到收尾才炸), 网络层放弃时落到程序化兜底而不是
   把整路打挂。

4. **切片形状由类型说死, 目的地只有一个说法**。
   旧版切片是裸 dict: 生产端写 `train_datas`、消费端读 `train_data`, 两边都不
   报错, 只是 prompt 里少一整块真实班次, 模型开始凭常识估算 —— 没有一行日志。
   同时四路各自把**不同的东西**塞进同一个键名 `destination`(交通路塞用户原话,
   场地路塞归一展示名, 餐饮路一个都没拿到)。现在多写一个键当场抛, 目的地是
   一个四路共用的对象, 各取一面。

5. **回炉不无条件覆盖, 且只重派相关的那几路**。
   回炉的合并是"后写覆盖前写", 于是第二轮的空壳/降级结果会盖掉第一轮那份有
   数字的报价(reconcile 只拦得住"空明细", 拦不住"少算")。冲突现在带归属,
   只把该修的那几路送回炉 —— 让本来没问题的几路跟着重跑, 只会把一个已被人工
   看过的方案重新掷一次骰子。

无第三方依赖: 标准库 unittest。全程不出网(get_chat_model / invoke_llm /
搜索工具都被替身接管)。

运行:
    cd backend && py -3.13 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from app.graph import nodes as N
from app.graph.errors import (CallResult, CapabilityDisabled, ConfigError,
                              TransientError)
from app.graph.model import LLM_SDK_MAX_RETRIES, LLM_TIMEOUT_SECONDS
from app.prompts.templates import (build_conflict_feedback, build_transport_prompt,
                                   build_venue_prompt)

# ============================ 替身 ============================


def _ai(args, name="Proposal", call_id="call_1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _degraded(reason="上游一直超时"):
    """网络层按纪律放弃后的结局(与 call_external 的产物同构)"""
    return CallResult.degraded(TransientError(reason, code="llm_timeout"), attempts=2)


class _ScriptedInvokeLLM:
    """替身 invoke_llm: 按剧本逐个交付 CallResult, 并记下每轮发出去的 messages

    真的 invoke_llm 会退避 sleep, 这里替掉是为了让用例快且确定 —— 它自己的
    "重试/放弃"行为在 test_error_handling 里已经钉过。
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.sent = []

    def __call__(self, llm, messages, budget):
        self.sent.append(list(messages))
        out = self.outcomes[min(len(self.sent) - 1, len(self.outcomes) - 1)]
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, CallResult):
            return out
        return CallResult.of(out)


class _FakeFoodTool:
    """替身搜索工具: 返回一份固定的周边餐厅结果, 不出网"""

    def __init__(self, results=None):
        self.invoked = []
        self.results = results if results is not None else [
            {"name": "老王土菜馆", "address": "场地旁", "type": "中餐",
             "rating": 4.6, "cost": 68},
            {"name": "川味小馆", "address": "场地对面", "type": "川菜",
             "rating": 4.4, "cost": 55},
        ]

    def invoke(self, args):
        self.invoked.append(args)
        return json.dumps({"status": "ok", "results": self.results, "note": ""},
                          ensure_ascii=False)


class _FakeBoundLLM:
    """替身 ChatModel: bind_tools 返回自己(节点只用 invoke)"""

    def __init__(self):
        self.bound = []

    def bind_tools(self, tools, tool_choice=None):
        self.bound.append([getattr(t, "name", type(t).__name__) for t in tools])
        return self


# ============================ 载荷 ============================

def _dest(raw="杭州", display=None, canon=None, place=None):
    """目的地切片的形态(_proposal_sends 产出, 四路共用同一个对象)"""
    return {"raw": raw, "display": display or raw,
            "canon": canon or {}, "place": place or {}}


def _venue_payload(n=2):
    return {
        "venues": [{"name": f"场地{i}", "address": "某路1号", "category": "团建基地",
                    "rating": 4.5, "ticket_price": 100, "distance_km": 3.0 + i}
                   for i in range(n)],
        "people_count": 12, "budget": 8000, "destination": _dest(),
        "activity_votes": {}, "activity_type": "团建",
    }


_FOOD_PAYLOAD = {
    "venues": [{"name": "场地0", "address": "某路1号", "category": "团建基地",
                "rating": 4.5, "ticket_price": 100,
                "location": {"longitude": 120.15, "latitude": 30.28}}],
    "dietary_all": [], "people_count": 12, "budget": 8000, "guides": [],
    "date": "2026-10-01", "date_end": "2026-10-01",
    "destination": _dest(),
}


def _proposal_args(items=(), description="ok", cost=0.0):
    return {"category": "food", "items": list(items), "total_cost": cost,
            "description": description}


# ============================ 1. 失败隔离 ============================

class TestRouteIsolation(unittest.IsolatedAsyncioTestCase):
    """一路炸了只废这一路; 但降级有边界, 该抛的必须抛"""

    async def test_transient_failure_degrades_only_that_route(self):
        with mock.patch.object(N, "invoke_structured",
                               side_effect=TransientError("高德超时", code="timeout")):
            out = await N.propose_venue(_venue_payload())

        self.assertEqual(list(out["proposals"]), ["venue"], "槽位必须保留")
        prop = out["proposals"]["venue"]
        self.assertEqual(prop["category"], "venue", "category 要认槽位, 不能因失败就漂移")
        self.assertEqual(prop["items"], [])
        self.assertEqual(prop["total_cost"], 0.0)
        self.assertTrue(out["steps_log"], "降级必须留下日志")

    async def test_degraded_description_says_failed_not_no_option(self):
        """降级文案必须与"候选不足"区分开 —— 两者在人眼里是不同的结论"""
        with mock.patch.object(N, "invoke_structured", side_effect=TransientError("超时")):
            failed = (await N.propose_venue(_venue_payload()))["proposals"]["venue"]

        # 候选不足走的是另一条路径(程序化拒绝, 零 LLM), 文案是"候选不足"
        shortage = (await N.propose_venue(_venue_payload(n=0)))["proposals"]["venue"]

        self.assertIn("失败", failed["description"])
        self.assertIn("候选不足", shortage["description"])
        self.assertNotEqual(failed["description"], shortage["description"])

    async def test_capability_disabled_degrades_but_is_not_called_a_failure(self):
        """可选能力未开不是故障(errors.py 明说): 既不抛, 也不能写成"生成失败\""""
        with mock.patch.object(N, "invoke_structured",
                               side_effect=CapabilityDisabled("未配置 BOCHA_API_KEY")):
            out = await N.propose_venue(_venue_payload())

        desc = out["proposals"]["venue"]["description"]
        self.assertIn("未启用", desc)
        self.assertNotIn("失败", desc)

    async def test_config_error_propagates(self):
        """必需配置缺失: 四路全空只会拼出一份"看着完整"的空壳 -> 必须让节点失败"""
        with mock.patch.object(N, "invoke_structured",
                               side_effect=ConfigError("LLM_API_KEY 未配置")):
            with self.assertRaises(ConfigError):
                await N.propose_venue(_venue_payload())

    async def test_programming_defect_propagates(self):
        """认不出的异常一律不许降级: 否则"字段名写错"会变成"这维没数据\""""
        for exc in (ValueError("字段名写错"), KeyError("venues_gone"),
                    RuntimeError("解包数量不对")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(N, "invoke_structured", side_effect=exc):
                    with self.assertRaises(type(exc)):
                        await N.propose_venue(_venue_payload())

    async def test_all_four_routes_share_one_return_shape(self):
        """四路(含餐饮)返回同一个形状 —— 汇合处才不需要"这一路是不是特例"的分支"""
        boom = mock.patch.object(N, "invoke_structured", side_effect=TransientError("boom"))
        with boom, mock.patch.object(N, "get_chat_model", return_value=_FakeBoundLLM()), \
                mock.patch.object(N, "invoke_llm", _ScriptedInvokeLLM(_degraded())), \
                mock.patch.object(N, "_make_food_search_tool", lambda loc: _FakeFoodTool()):
            outs = [await N.propose_venue(_venue_payload()),
                    await N.propose_transport({"people_count": 12, "origin": "上海",
                                               "destination": _dest()}),
                    await N.propose_flow({"destination": _dest(), "activity_votes": {}}),
                    await N.propose_food(_FOOD_PAYLOAD)]

        for out in outs:
            self.assertEqual(set(out), {"proposals", "steps_log"})
            self.assertEqual(len(out["proposals"]), 1)
            self.assertIsInstance(out["steps_log"], list)


# ============================ 2. 餐饮路: 统一纪律 ============================

class TestFoodRouteDiscipline(unittest.IsolatedAsyncioTestCase):
    """餐饮路与其余三路共用同一套调用纪律与同一条校验口径"""

    def setUp(self):
        p = mock.patch.object(N, "_make_food_search_tool", lambda loc: _FakeFoodTool())
        p.start()
        self.addCleanup(p.stop)

    async def _run_food(self, invoker, **overrides):
        payload = dict(_FOOD_PAYLOAD)
        payload.update(overrides)
        with mock.patch.object(N, "get_chat_model", return_value=_FakeBoundLLM()), \
                mock.patch.object(N, "invoke_llm", invoker):
            return await N.propose_food(payload)

    async def test_network_giveup_falls_back_programmatically(self):
        """网络层放弃 -> 用已搜到的真实餐厅兜底, **不是**把整路打挂"""
        invoker = _ScriptedInvokeLLM(
            _ai({"keywords": "餐厅"}, name="search_nearby_food"),   # 先搜一轮
            _degraded(),                                            # 再调就放弃
        )
        out = await self._run_food(invoker)

        prop = out["proposals"]["food"]
        self.assertGreater(len(prop["items"]), 0, "兜底必须给出明细, 不能是空提案")
        self.assertIn("程序化兜底", prop["description"])
        # 搜到的真实餐厅名必须进方案(花了钱的检索结果不能丢)
        names = " ".join(i["name"] for i in prop["items"])
        self.assertIn("老王土菜馆", names + json.dumps(prop, ensure_ascii=False))

    async def test_invalid_proposal_args_get_repaired_next_round(self):
        """模型给的参数当场校验: 不合格就把字段级修正指令回填, 而不是收下再说

        旧实现是 `raw = args` 直接收下, 一路拖到循环外重建时才炸 —— 那时已经出不了
        循环, 只能整路失败。
        """
        bad = _ai({"category": "food",
                   "items": [{"name": "老王土菜馆"}]})          # 缺 cost, 过不了校验
        good = _ai(_proposal_args(
            [{"name": "老王土菜馆", "cost": 680.0}], description="按忌口选的"))
        invoker = _ScriptedInvokeLLM(bad, good)

        out = await self._run_food(invoker)
        prop = out["proposals"]["food"]

        self.assertEqual(len(invoker.sent), 2, "第一轮不合格就该有第二轮, 而不是直接兜底")
        self.assertEqual(prop["total_cost"], 680.0)
        # 第二轮发出去的对话里必须带一条修正指令的 ToolMessage(哪个字段错了)
        replayed = invoker.sent[1]
        fixes = [m for m in replayed if isinstance(m, ToolMessage)]
        self.assertEqual(len(fixes), 1)
        self.assertIn("cost", str(fixes[0].content))

    async def test_correction_message_follows_protocol(self):
        """带 tool_calls 的 AIMessage 之后必须紧跟同 id 的 ToolMessage(否则端点 400)"""
        invoker = _ScriptedInvokeLLM(_ai({"category": "food", "items": [{"name": "x"}]}),
                                     _ai(_proposal_args()))
        await self._run_food(invoker)

        replayed = invoker.sent[1]
        last_ai = [i for i, m in enumerate(replayed) if isinstance(m, AIMessage)][-1]
        reply = replayed[last_ai + 1]
        self.assertIsInstance(reply, ToolMessage)
        self.assertEqual(reply.tool_call_id, replayed[last_ai].tool_calls[0]["id"])

    async def test_route_budget_exhausted_stops_early(self):
        """整条路的预算用尽 -> 不再起新轮次, 直接转程序化兜底"""
        invoker = _ScriptedInvokeLLM(_ai(_proposal_args(description="不该被用到")))
        with mock.patch.object(N, "FOOD_TOTAL_BUDGET", 0.0):
            out = await self._run_food(invoker)

        self.assertEqual(len(invoker.sent), 0, "预算为 0 时一次请求都不该发")
        self.assertIsNotNone(out["proposals"]["food"])

    async def test_salvage_path_is_validated_too(self):
        """最后一轮"别的工具名但参数像 Proposal"的兜底收口, 同样要过校验"""
        mostly_right = _ai({"category": "food", "items": [{"name": "老王", "cost": 500.0}],
                            "description": "兜底收下"}, name="finalize")
        out = await self._run_food(_ScriptedInvokeLLM(mostly_right, mostly_right))

        prop = out["proposals"]["food"]
        self.assertEqual(prop["total_cost"], 500.0, "能收下的就该收下, 别白白转兜底")


# ============================ 4. 切片契约(类型 + 目的地) ============================

def _send_state(**overrides):
    """一份能跑 _proposal_sends 的最小 state"""
    state = {
        "parsed_request": {"people_count": 12, "budget": 8000.0, "origin": "上海",
                           "destination": "莫干山", "transport_pref": "优先高铁",
                           "date": "2026-10-01", "date_end": "2026-10-02"},
        "basic_info": {
            "venues": [], "guides": [], "train_info": [],
            "destination_canon": {"name": "湖州市", "kind": "place", "adcode": "330521",
                                  "display": "莫干山（浙江省湖州市德清县）",
                                  "query": "莫干山",
                                  "location": {"longitude": 119.88, "latitude": 30.60}},
        },
        "raw_request": {"structured": {"destination_place": {"city": "湖州市"}}},
    }
    state.update(overrides)
    return state


class TestSendContract(unittest.TestCase):
    """四路的切片形状由 `_XxxPayload` 说死; 目的地是同一个对象, 各取一面"""

    def test_all_four_routes_carry_the_same_destination(self):
        """四路认的是同一个目的地: 原话/展示名/归一结果/入口入参都在同一个包里

        旧版是三个路三个不同的东西装在同一个键名 `destination` 下(交通路装用户
        原话、场地路装归一展示名), 餐饮路**一个都没拿到** —— 于是"四路说的是不是
        同一个地方"只能靠人肉核对。
        """
        sends = {s.node: s.arg for s in N._proposal_sends(_send_state())}
        self.assertEqual(set(sends), {"propose_transport", "propose_venue",
                                      "propose_flow", "propose_food"})
        for node, arg in sends.items():
            with self.subTest(node=node):
                d = arg["destination"]
                self.assertEqual(d["raw"], "莫干山", "用户原话: 检索与同城/跨城判断靠它")
                self.assertEqual(d["display"], "莫干山（浙江省湖州市德清县）")
                self.assertEqual(d["canon"]["adcode"], "330521", "距离锚点所在的归一结果")
                self.assertEqual(d["place"], {"city": "湖州市"},
                                 "入口的省市县选择要透传到流程路(它自己也会检索)")

    def test_missing_optional_slice_is_not_an_error(self):
        """可选切片没给不该让整路炸: 默认值兜住(生产端少给一个键是常见形态)"""
        sends = {s.node: s.arg for s in N._proposal_sends(_send_state())}
        self.assertEqual(sends["propose_transport"]["train_data"], [])
        self.assertEqual(sends["propose_food"]["dietary_all"], [])
        self.assertIsNone(sends["propose_venue"]["conflict_report"])

    def test_only_dispatches_named_routes(self):
        """定向回炉: only 之外的路不派发(它们的结果原样留在 state 里)"""
        sends = N._proposal_sends(_send_state(), only={"flow"})
        self.assertEqual([s.node for s in sends], ["propose_flow"])

    def test_previous_result_is_carried_per_route(self):
        """回炉时每一路带上**自己**上一轮的结果(见 _keep_better_proposal)"""
        prev_venue = {"category": "venue", "items": [{"name": "场地0", "cost": 100.0}],
                      "total_cost": 100.0, "description": "上一轮"}
        sends = {s.node: s.arg
                 for s in N._proposal_sends(_send_state(proposals={"venue": prev_venue}))}
        self.assertEqual(sends["propose_venue"]["previous_proposal"], prev_venue)
        self.assertIsNone(sends["propose_flow"]["previous_proposal"],
                          "本路没有上一轮结果时就是 None, 不许凭空造一个")

    def test_generation_start_does_not_carry_the_previous_result(self):
        """一次生成的起点不带"上一轮": 那是用户刚否掉的那一版, 不能悄悄换回来

        HITL 否决后重走也走 dispatch_proposals, 而 state.proposals 里躺着的正是被
        否决的方案。若把它当"上一轮"留着, 本轮没给出明细时就会保留它 —— 等于无视
        用户的否决意见(而"这一维生成失败"至少是实话)。同一次生成里的回炉则相反:
        那时上一轮是还没给人看过的新方案, 留住它是对的。
        """
        rejected = {"category": "food", "items": [{"name": "火锅", "cost": 100.0}],
                    "total_cost": 100.0, "description": "用户刚否掉的"}
        state = _send_state(generate_plan=True, proposals={"food": rejected},
                            user_feedback=["餐饮不要火锅"])
        sends = {s.node: s.arg for s in N.dispatch_proposals(state)}
        self.assertEqual(len(sends), 4)
        for node, arg in sends.items():
            with self.subTest(node=node):
                self.assertIsNone(arg["previous_proposal"])
                self.assertEqual(arg["user_feedback"], ["餐饮不要火锅"],
                                 "否决意见本身照给, 只管住「别保留旧结果」这一件事")

    def test_reconcile_replan_does_carry_it(self):
        """回炉重派是同一轮生成内部的事: 上一轮结果必须带上(否则"别覆盖"无从判断)"""
        previous = {"category": "venue", "items": [{"name": "场地0", "cost": 100.0}],
                    "total_cost": 100.0, "description": "本轮第一版"}
        sends = N._proposal_sends(_send_state(proposals={"venue": previous}))
        by_node = {s.node: s.arg for s in sends}
        self.assertEqual(by_node["propose_venue"]["previous_proposal"], previous)

    def test_consumer_rejects_unknown_payload_key(self):
        """生产端多写/写错一个键 -> **当场抛**, 而不是静默变成 None

        旧版裸 dict 时, 生产端写 `train_datas`、消费端读 `train_data`, 两边都不
        报错: prompt 里少一整块真实班次, 模型改凭常识估算, 全程没有一行日志。
        """
        payload = _venue_payload()
        payload["train_datas"] = [{"车次": "G1234"}]     # 交通路的键串到了场地路
        with self.assertRaises(ValidationError):
            N._VenuePayload.model_validate(payload)


class TestPayloadStrictness(unittest.IsolatedAsyncioTestCase):
    """切片形状不对是**我们的**缺陷, 不许被降级成"这维没数据"

    切片解析在降级壳之外(见 _proposal_node docstring): 放进 try 就会被
    `except (AppError, ValidationError)` 收走, 于是一个字段名写错会伪装成
    "模型输出不合法", 产出空提案还带着一句"生成失败"。
    """

    async def test_payload_shape_defect_is_not_swallowed_as_degradation(self):
        payload = _venue_payload()
        payload["destination"] = "杭州"        # 字符串不是目的地切片
        with self.assertRaises(ValidationError):
            await N.propose_venue(payload)

    async def test_shape_is_checked_before_the_llm_would_even_fail(self):
        """顺序钉死: 先校验切片, 再进降级壳 —— 连"上游也会挂"都救不了它"""
        with mock.patch.object(N, "invoke_structured",
                               side_effect=TransientError("上游挂了")):
            with self.assertRaises(ValidationError):
                await N.propose_venue({"destination": "杭州"})


# ============================ 5. 回炉: 不覆盖 + 定向重派 ============================

class TestReplanKeepsTheBetterProposal(unittest.IsolatedAsyncioTestCase):
    """回炉按 key 后写覆盖前写 —— 第二轮变差时不能就这么把第一轮盖掉"""

    def test_empty_round2_keeps_round1(self):
        previous = _proposal_args([{"name": "场地0", "cost": 100.0}], cost=100.0)
        kept, note = N._keep_better_proposal("venue", _proposal_args(), previous)
        self.assertEqual(kept, previous)
        self.assertIn("保留上一轮", note)

    def test_real_round2_replaces_round1(self):
        """第二轮给出了明细 -> 照换(只挡"空", 不做什么"哪份更好"的聪明判断)"""
        previous = _proposal_args([{"name": "场地0", "cost": 100.0}], cost=100.0)
        new = _proposal_args([{"name": "场地1", "cost": 80.0}], cost=80.0)
        kept, note = N._keep_better_proposal("venue", new, previous)
        self.assertEqual(kept, new)
        self.assertIsNone(note)

    def test_no_previous_round_keeps_the_new_one(self):
        """首次派发没有上一轮: 空提案照样收下(该由 reconcile 的空明细规则拦)"""
        new = _proposal_args()
        self.assertEqual(N._keep_better_proposal("venue", new, None), (new, None))

    async def test_degraded_round2_keeps_previous_proposal(self):
        """第二轮回炉时这一路炸了 -> 保留上一轮报价, 而不是换成一个 0 元空壳

        空壳会让四路合计**少算**这一维的费用, 而问题本身还在; 一份"0 元 + 校验
        通过"的结论比"带着已知冲突交人工裁决"糟得多。
        """
        previous = {"category": "venue", "items": [{"name": "场地0", "cost": 100.0}],
                    "total_cost": 100.0, "description": "上一轮"}
        with mock.patch.object(N, "invoke_structured", side_effect=TransientError("超时")):
            out = await N.propose_venue(
                {**_venue_payload(), "previous_proposal": previous})

        prop = out["proposals"]["venue"]
        self.assertEqual(prop["total_cost"], 100.0, "上一轮的报价必须留住")
        self.assertEqual(prop["description"], "上一轮")
        self.assertIn("保留上一轮", " ".join(out["steps_log"]))


class TestConflictOwnership(unittest.TestCase):
    """每条冲突要说得出"该谁修": 定向回炉和 prompt 分流都靠它"""

    def _props(self, venue_desc="可容纳50人", venue_cost=100.0):
        def _p(cat, cost):
            return {"category": cat, "items": [{"name": f"{cat}费用", "cost": float(cost)}],
                    "total_cost": float(cost), "description": f"{cat}方案"}
        return {"transport": _p("transport", 200.0),
                "venue": {**_p("venue", venue_cost), "description": venue_desc},
                "flow": _p("flow", 300.0), "food": _p("food", 400.0)}

    def test_capacity_conflict_is_owned_by_venue(self):
        out = N.reconcile({"parsed_request": {"people_count": 30, "budget": 100000.0},
                           "proposals": self._props("可容纳10人")})
        report = out["conflict_report"]
        self.assertEqual(list(report["conflicts_by_category"]), ["venue"])
        self.assertEqual(report["conflicts_by_category"]["venue"], report["conflicts"],
                         "归属列表里就是 conflicts 的原文(不另写一份措辞)")

    def test_budget_conflict_is_global(self):
        """超支要四路一起让利, 归属不到某一路头上 -> 不出现在任何类别下"""
        out = N.reconcile({"parsed_request": {"people_count": 30, "budget": 100.0},
                           "proposals": self._props()})
        report = out["conflict_report"]
        self.assertGreater(report["over_budget"], 0)
        self.assertEqual(report["conflicts_by_category"], {}, "全局冲突没有归属")
        self.assertNotIn(report["conflicts"][0], sum(report["conflicts_by_category"].values(), []))

    def test_empty_items_conflict_is_owned_by_its_own_route(self):
        """空明细逐路成条: 只有餐饮是空壳时, 该修的只有餐饮"""
        props = self._props()
        props["food"] = {"category": "food", "items": [], "total_cost": 0.0,
                         "description": "空"}
        out = N.reconcile({"parsed_request": {"people_count": 30, "budget": 100000.0},
                           "proposals": props})
        report = out["conflict_report"]
        self.assertEqual(list(report["conflicts_by_category"]), ["food"])
        self.assertIn("餐饮 提案无有效报价", report["conflicts_by_category"]["food"][0])


class TestTargetedReplan(unittest.TestCase):
    """只重派该修的那几路: 不相关的那几路连跑都不跑(方案不被重新掷骰子)"""

    def test_targets_are_the_owning_routes(self):
        report = {"over_budget": 0.0, "conflicts": ["时间线重叠: A 与 B"],
                  "conflicts_by_category": {"flow": ["时间线重叠: A 与 B"]}}
        self.assertEqual(N._replan_targets(report), {"flow"})

    def test_budget_conflict_targets_all_four(self):
        report = {"over_budget": 100.0, "conflicts": ["超支100元: ..."],
                  "conflicts_by_category": {"venue": ["场地容量不足: ..."]}}
        self.assertIsNone(N._replan_targets(report), "全局冲突要四路全发")

    def test_report_without_ownership_targets_all_four(self):
        """没有归属信息(老报告/手工构造) -> 宁可多跑, 不可漏修"""
        report = {"over_budget": 0.0, "conflicts": ["说不清归属的冲突"],
                  "conflicts_by_category": {}}
        self.assertIsNone(N._replan_targets(report))

    def test_route_dispatches_only_the_owned_route(self):
        state = _send_state(
            conflict_report={"over_budget": 0.0, "conflicts": ["时间线重叠: A 与 B"],
                             "suggestions": [],
                             "conflicts_by_category": {"flow": ["时间线重叠: A 与 B"]}},
            replan_count=1,
        )
        sends = N.route_after_reconcile(state)
        self.assertEqual([s.node for s in sends], ["propose_flow"])
        self.assertTrue(sends[0].arg.get("conflict_report"), "回炉切片要带校验报告")

    def test_route_dispatches_all_four_on_budget_conflict(self):
        state = _send_state(
            conflict_report={"over_budget": 4000.0, "conflicts": ["超支4000元: ..."],
                             "suggestions": [], "conflicts_by_category": {}},
            replan_count=1,
        )
        self.assertEqual(len(N.route_after_reconcile(state)), 4)

    def test_route_stops_at_replan_limit(self):
        state = _send_state(
            conflict_report={"over_budget": 4000.0, "conflicts": ["超支4000元"],
                             "suggestions": [], "conflicts_by_category": {}},
            replan_count=N.MAX_REPLAN + 1,
        )
        self.assertEqual(N.route_after_reconcile(state), "risk")


class TestConflictFeedbackScoping(unittest.TestCase):
    """每路的 prompt 只看到"与它相关或全局"的冲突, 不再读别人的作业"""

    _REPORT = {
        "over_budget": 200.0,
        "conflicts": ["超支200元: 四路合计...", "场地容量不足: 声明容纳10人 < 30人",
                      "时间线重叠(D1): 「爬山」与「聚餐」"],
        "suggestions": ["场地 3200元占 40%, 可降级空间约200元"],
        "conflicts_by_category": {"venue": ["场地容量不足: 声明容纳10人 < 30人"],
                        "flow": ["时间线重叠(D1): 「爬山」与「聚餐」"]},
    }

    def test_route_sees_its_own_and_global_conflicts_only(self):
        transport = build_transport_prompt({}, 30, 5000.0, conflict_report=self._REPORT)
        self.assertIn("超支200元", transport, "全局冲突每一路都要看到")
        self.assertNotIn("场地容量不足", transport)
        self.assertNotIn("时间线重叠", transport)

        venue = build_venue_prompt([{"name": "场地0"}], 30, 5000.0,
                                   conflict_report=self._REPORT)
        self.assertIn("场地容量不足", venue, "自己的冲突必须在")
        self.assertNotIn("时间线重叠", venue)
        self.assertIn("可降级空间", venue, "建议是全局的, 不该被顺手筛掉")

    def test_all_conflicts_shown_when_ownership_is_missing(self):
        """老报告没有归属 -> 与旧行为一致地全给(少给信息只会让模型改错地方)"""
        report = {k: v for k, v in self._REPORT.items() if k != "conflicts_by_category"}
        text = build_conflict_feedback(report, "transport")
        self.assertIn("场地容量不足", text)
        self.assertIn("时间线重叠", text)

    def test_no_category_keeps_legacy_behavior(self):
        """不传 category = 全部照给(直接调用者的既有行为不能变)"""
        self.assertEqual(
            build_conflict_feedback(self._REPORT),
            build_conflict_feedback(self._REPORT, None))


# ============================ 6. 客户端上限 ============================

class TestClientLimits(unittest.TestCase):
    """单次超时与编程接口自带的重试, 必须在构建客户端时**显式**设死"""

    def test_client_gets_timeout_and_no_sdk_retries(self):
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_BASE_URL": ""}), \
                mock.patch("app.graph.model.ChatOpenAI") as fake:
            from app.graph.model import get_chat_model
            get_chat_model(0.3)

        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["request_timeout"], LLM_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["max_retries"], LLM_SDK_MAX_RETRIES)
        self.assertEqual(LLM_SDK_MAX_RETRIES, 0,
                         "编程接口自带的重试会跑在项目预算之外, 必须关掉")
        self.assertTrue(0 < LLM_TIMEOUT_SECONDS <= 120,
                        "单次上限要远小于编程接口默认的 600s 读超时")

    def test_missing_key_is_a_config_error(self):
        """缺 key 是**必需配置缺失**, 不是认不出的程序缺陷"""
        from app.graph.model import get_chat_model
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": "",
                                          "LLM_BASE_URL": "", "OPENAI_BASE_URL": ""}):
            with self.assertRaises(ConfigError):
                get_chat_model(0.3)


if __name__ == "__main__":
    unittest.main()