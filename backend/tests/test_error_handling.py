"""离线单元测试: 外部调用失败分类与处置(app/graph/errors.py)

设计原则:
- **不触网**: 全部用假函数与手工构造的 httpx 异常, 不消耗高德额度
- **不真等**: 用假时钟(时钟 + sleep 同步推进)验证退避与总预算, 测试瞬时完成
- 无第三方依赖: 标准库 unittest(pytest 未安装)

运行:
    cd backend && python -m unittest discover -s tests -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest import mock

import httpx
from pydantic import BaseModel

from app.graph.errors import (
    AppError,
    CallResult,
    CallStatus,
    CapabilityDisabled,
    ConfigError,
    ModelOutputError,
    PermanentError,
    TransientError,
    call_external,
    classify,
    disabled,
)


# ============================ 测试用工具 ============================


class FakeClock:
    """假时钟: sleep 直接推进时间, 于是退避/预算逻辑可瞬时验证"""

    def __init__(self, start: float = 0.0):
        self.t = start
        self.slept: list = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


def http_status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.invalid/x")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def raiser(exc: BaseException, times: int = 10**6):
    """返回一个前 `times` 次抛 exc、之后返回 "ok" 的函数, 并记录调用次数

    接受任意实参 —— 好让它能直接顶替客户端函数(如 search_venues(城市, 关键词))
    """
    state = {"n": 0}

    def fn(*_args, **_kwargs):
        state["n"] += 1
        if state["n"] <= times:
            raise exc
        return "ok"

    fn.calls = state  # type: ignore[attr-defined]
    return fn


# ============================ classify ============================


class TestClassify(unittest.TestCase):
    def test_timeout_is_transient_and_retryable(self):
        err = classify(httpx.TimeoutException("timed out"), kind="amap")
        self.assertIsInstance(err, TransientError)
        self.assertTrue(err.retryable)
        self.assertEqual(err.code, "timeout")
        self.assertIn("amap", str(err))          # kind 进日志前缀

    def test_connect_error_is_transient(self):
        err = classify(httpx.ConnectError("refused"), kind="bocha")
        self.assertIsInstance(err, TransientError)
        self.assertEqual(err.code, "transport")

    def test_5xx_is_transient(self):
        err = classify(http_status_error(503))
        self.assertIsInstance(err, TransientError)
        self.assertTrue(err.retryable)

    def test_429_is_transient(self):
        err = classify(http_status_error(429))
        self.assertIsInstance(err, TransientError)
        self.assertTrue(err.retryable)

    def test_4xx_is_permanent_not_retryable(self):
        err = classify(http_status_error(403))
        self.assertIsInstance(err, PermanentError)
        self.assertFalse(err.retryable)

    def test_bad_json_is_transient(self):
        import json
        try:
            json.loads("<html>gateway error</html>")
        except json.JSONDecodeError as e:
            err = classify(e)
        self.assertIsInstance(err, TransientError)
        self.assertEqual(err.code, "bad_payload")

    def test_app_error_passes_through(self):
        original = ConfigError("no key")
        self.assertIs(classify(original), original)

    def test_unknown_exception_is_NOT_caught(self):
        """核心规则: 认不出来的异常返回 None —— 程序缺陷不该被吞"""
        self.assertIsNone(classify(ValueError("too many values to unpack")))
        self.assertIsNone(classify(TypeError("bad operand")))
        self.assertIsNone(classify(KeyError("missing_field")))
        self.assertIsNone(classify(RuntimeError("boom")))

    def test_bare_valueerror_is_not_misclassified(self):
        """不能把 bare ValueError 当临时故障 —— 否则字段名写错会被静默重试"""
        err = classify(ValueError("字段名拼错了"))
        self.assertIsNone(err)


# ============================ CallResult ============================


class TestCallResult(unittest.TestCase):
    def test_of_empty_containers_is_EMPTY(self):
        for empty in ([], {}, "", None, (), set()):
            with self.subTest(value=empty):
                self.assertIs(CallResult.of(empty).status, CallStatus.EMPTY)

    def test_of_non_empty_is_OK(self):
        self.assertIs(CallResult.of([1, 2]).status, CallStatus.OK)
        self.assertIs(CallResult.of("x").status, CallStatus.OK)

    def test_empty_is_ok_but_not_has_data(self):
        r = CallResult.of([])
        self.assertTrue(r.ok)          # 调用成功
        self.assertFalse(r.has_data)   # 但没有数据
        self.assertFalse(r.is_degraded)  # 关键: EMPTY 不是降级

    def test_degraded_and_failed_are_distinguishable_from_empty(self):
        r = CallResult.degraded(TransientError("timeout"))
        self.assertTrue(r.is_degraded)
        self.assertFalse(r.ok)
        self.assertFalse(r.usable)
        self.assertIsNot(r.status, CallStatus.EMPTY)

    def test_disabled_is_not_an_error(self):
        r = disabled("未配置 BOCHA_API_KEY")
        self.assertIs(r.status, CallStatus.DISABLED)
        self.assertIsNone(r.error)      # 刻意降级, 不是故障
        self.assertFalse(r.is_degraded)  # 不该被当成故障上报
        self.assertTrue(r.usable)        # 但流程要能继续
        self.assertFalse(r.ok)           # 语义: 我们根本没调用它

    def test_as_note_is_human_readable(self):
        self.assertIn("正常", CallResult.of([1, 2, 3]).as_note("venues"))
        self.assertIn("无数据", CallResult.of([]).as_note("weather"))
        self.assertIn("未启用", disabled("no key").as_note("guides"))
        self.assertIn("降级", CallResult.degraded(TransientError("t")).as_note("trains"))


# ============================ call_external ============================


class TestCallExternal(unittest.TestCase):
    def test_success_first_try(self):
        r = call_external(lambda: [1, 2], kind="t")
        self.assertIs(r.status, CallStatus.OK)
        self.assertEqual(r.value, [1, 2])
        self.assertEqual(r.attempts, 1)

    def test_empty_return_maps_to_EMPTY_not_degraded(self):
        r = call_external(lambda: [], kind="t")
        self.assertIs(r.status, CallStatus.EMPTY)
        self.assertFalse(r.is_degraded)

    def test_transient_retried_then_succeeds(self):
        clock = FakeClock()
        fn = raiser(httpx.TimeoutException("slow"), times=1)
        r = call_external(fn, kind="t", retries=2, sleep=clock.sleep, clock=clock)
        self.assertIs(r.status, CallStatus.OK)
        self.assertEqual(r.value, "ok")
        self.assertEqual(r.attempts, 2)

    def test_transient_exhausted_becomes_DEGRADED(self):
        clock = FakeClock()
        fn = raiser(httpx.ConnectError("refused"))
        r = call_external(fn, kind="t", retries=2, sleep=clock.sleep, clock=clock)
        self.assertIs(r.status, CallStatus.DEGRADED)
        self.assertEqual(r.attempts, 3)          # 首发 + 2 次重试
        self.assertIsInstance(r.error, TransientError)

    def test_permanent_not_retried(self):
        clock = FakeClock()
        fn = raiser(http_status_error(403))
        r = call_external(fn, kind="t", retries=3, sleep=clock.sleep, clock=clock)
        self.assertIs(r.status, CallStatus.FAILED)
        self.assertEqual(r.attempts, 1)          # 一次都不重试
        self.assertEqual(clock.slept, [])        # 也没睡

    def test_unknown_exception_propagates(self):
        """程序缺陷必须冒出来, 不能被 call_external 吞成降级"""
        fn = raiser(ValueError("代码 bug"))
        with self.assertRaises(ValueError):
            call_external(fn, kind="t", retries=3)

    def test_exponential_backoff_schedule(self):
        clock = FakeClock()
        fn = raiser(httpx.TimeoutException("x"))
        call_external(fn, kind="t", retries=3, backoff=0.5,
                      max_backoff=8.0, sleep=clock.sleep, clock=clock)
        self.assertEqual(clock.slept, [0.5, 1.0, 2.0])

    def test_backoff_is_capped_by_max_backoff(self):
        clock = FakeClock()
        fn = raiser(httpx.TimeoutException("x"))
        call_external(fn, kind="t", retries=4, backoff=2.0,
                      max_backoff=3.0, sleep=clock.sleep, clock=clock)
        self.assertEqual(clock.slept, [2.0, 3.0, 3.0, 3.0])

    def test_total_budget_stops_retrying(self):
        """退避和次数都满足, 仍可能超总时长 —— 这就是 retry_budget 存在的理由"""
        clock = FakeClock()
        fn = raiser(httpx.TimeoutException("x"))
        r = call_external(fn, kind="t", retries=10, backoff=1.0,
                          max_backoff=8.0, retry_budget=5.0,
                          sleep=clock.sleep, clock=clock)
        self.assertIs(r.status, CallStatus.DEGRADED)
        self.assertEqual(r.error.code, "retry_budget_exhausted")
        self.assertLessEqual(clock.total_slept, 5.0)   # 没有超出预算
        self.assertLess(r.attempts, 11)                # 次数上限没到就停了

    def test_config_error_is_permanent(self):
        fn = raiser(ConfigError("未配置 AMAP_API_KEY"))
        r = call_external(fn, kind="t", retries=3)
        self.assertIs(r.status, CallStatus.FAILED)
        self.assertEqual(r.attempts, 1)

    def test_model_output_error_is_not_retried_here(self):
        """schema 错该回给模型改, 不该在这层裸重试"""
        fn = raiser(ModelOutputError("缺 tool_calls"))
        r = call_external(fn, kind="t", retries=3)
        self.assertIs(r.status, CallStatus.FAILED)
        self.assertEqual(r.attempts, 1)
        self.assertTrue(r.error.feed_back_to_model)


# ============================ CapabilityDisabled ============================


class TestCapabilityDisabled(unittest.TestCase):
    def test_carries_no_error_and_no_retry(self):
        clock = FakeClock()
        fn = raiser(CapabilityDisabled("未配置 BOCHA_API_KEY"))
        r = call_external(fn, kind="bocha", retries=3,
                          sleep=clock.sleep, clock=clock)
        self.assertIs(r.status, CallStatus.DISABLED)
        self.assertIsNone(r.error)      # 刻意降级, 不是故障
        self.assertEqual(r.attempts, 1)  # 不重试
        self.assertEqual(clock.slept, [])  # 不等待
        self.assertFalse(r.is_degraded)

    def test_is_not_a_config_error(self):
        """可选能力未开 != 必需配置缺失: 前者流程照跑, 后者不该继续"""
        self.assertNotIsInstance(CapabilityDisabled("x"), ConfigError)
        self.assertFalse(CapabilityDisabled("x").retryable)


# ============================ 客户端失败语义 ============================


class TestClientFailureSemantics(unittest.TestCase):
    """锁住"未配置 key / 正常无结果 / 网络故障"三者在客户端层的区分"""

    def setUp(self):
        """清空客户端缓存: 负结果有 30s 防抖, 不清的话用例会互相看到对方的空结果

        这不是测试洁癖 —— 同一参数先被"A 场景拿到 []"缓存住, 后面"B 场景本该抛
        异常"的用例就会拿到那个缓存值而不抛, 用例会以完全误导的方式变绿/变红。
        """
        from app.graph.ttl_cache import get_cache
        for name in ("query_trains", "bocha_web_search"):
            cache = get_cache(name)
            if cache is not None:
                cache.clear()

    def test_bocha_without_key_raises_capability_disabled(self):
        from unittest import mock
        from app.graph import bocha_client

        with mock.patch.object(bocha_client, "get_settings") as gs:
            gs.return_value = mock.Mock(bocha_api_key="")
            with self.assertRaises(CapabilityDisabled):
                bocha_client.web_search("杭州 团建 口碑")

    def test_train_without_key_raises_capability_disabled(self):
        from unittest import mock
        from app.graph import train_client

        with mock.patch.object(train_client, "get_settings") as gs:
            gs.return_value = mock.Mock(juhe_train_key="")
            with self.assertRaises(CapabilityDisabled):
                train_client.query_trains("上海", "杭州", None)

    def test_train_missing_date_is_business_result_not_error(self):
        """日期缺失返回 [], 是正常业务结果 —— 不该抛、不该当故障"""
        from unittest import mock
        from app.graph import train_client

        with mock.patch.object(train_client, "get_settings") as gs:
            gs.return_value = mock.Mock(juhe_train_key="fake-key")
            self.assertEqual(train_client.query_trains("上海", "杭州", None), [])

    def test_train_past_date_is_business_result_not_error(self):
        from unittest import mock
        from app.graph import train_client

        with mock.patch.object(train_client, "get_settings") as gs:
            gs.return_value = mock.Mock(juhe_train_key="fake-key")
            self.assertEqual(train_client.query_trains("上海", "杭州", "2000-01-01"), [])

    def test_amap_without_key_raises_config_error(self):
        """高德 key 是必需配置 -> ConfigError(永久), 不是可选能力"""
        from unittest import mock
        from app.graph import amap_client

        with mock.patch.object(amap_client, "get_settings") as gs:
            gs.return_value = mock.Mock(amap_api_key="")
            with self.assertRaises(ConfigError):
                amap_client._rest_get("weather/weatherInfo", {"city": "杭州"})


class TestAmapStatusClassification(unittest.TestCase):
    """业务错误码要分清"限流(可重试)"和"凭据问题(重试无意义)" """

    def test_rate_limit_is_transient(self):
        from app.graph.amap_client import _amap_status_error
        err = _amap_status_error("place/text", "CUQPS_HAS_EXCEEDED_THE_LIMIT")
        self.assertIsInstance(err, TransientError)
        self.assertTrue(err.retryable)

    def test_invalid_key_is_permanent(self):
        from app.graph.amap_client import _amap_status_error
        err = _amap_status_error("place/text", "INVALID_USER_KEY")
        self.assertIsInstance(err, PermanentError)
        self.assertFalse(err.retryable)

    def test_daily_quota_exhausted_is_permanent(self):
        """日额度耗尽当天重试无意义 -> 不该退避重试"""
        from app.graph.amap_client import _amap_status_error
        err = _amap_status_error("place/text", "USER_DAILY_QUERY_OVER_LIMIT")
        self.assertIsInstance(err, PermanentError)

    def test_unknown_business_code_defaults_transient_with_bounded_retry(self):
        from app.graph.amap_client import _amap_status_error
        err = _amap_status_error("place/text", "SOME_NEW_CODE")
        self.assertIsInstance(err, TransientError)


# ============================ 契约回归 ============================


class TestTaxonomyContract(unittest.TestCase):
    """把"四类失败下一步不同"这个契约钉死, 防止以后被改坏"""

    def test_retryability_matrix(self):
        self.assertTrue(TransientError("t").retryable)
        self.assertFalse(PermanentError("p").retryable)
        self.assertFalse(ConfigError("c").retryable)
        self.assertFalse(ModelOutputError("m").retryable)

    def test_only_model_output_feeds_back_to_model(self):
        self.assertTrue(ModelOutputError("m").feed_back_to_model)
        self.assertFalse(TransientError("t").feed_back_to_model)
        self.assertFalse(PermanentError("p").feed_back_to_model)

    def test_every_app_error_has_user_message(self):
        for err in (AppError("x"), TransientError("x"), PermanentError("x"),
                    ConfigError("x"), ModelOutputError("x")):
            with self.subTest(err=type(err).__name__):
                self.assertTrue(err.user_message)
                self.assertTrue(err.code)


# ============================ 结构化输出(invoke_structured) ============================


class _Out(BaseModel):
    """测试用 schema"""
    name: str
    count: int


def _ai(args, content: str = ""):
    from langchain_core.messages import AIMessage
    return AIMessage(content=content,
                     tool_calls=[{"name": "_Out", "args": args, "id": "call_1"}])


class _FakeLLM:
    """假 LLM: 按剧本依次回应, 并记下每次收到的消息(用于断言"修正指令进了对话")"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent: list = []

    def invoke(self, messages):
        self.sent.append(list(messages))
        item = self.responses[min(len(self.sent) - 1, len(self.responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item


class TestInvokeStructured(unittest.IsolatedAsyncioTestCase):
    """两类失败必须分开处置: 网络抖动可退避重发, 模型填错参数只能告诉它错在哪

    旧实现把两者丢进同一个计数器、重试时原样重发 prompt —— 参数错的情况下
    模型多半再错一遍, 而且日志里看不出是"服务在抖"还是"模型不会填"。
    """

    def setUp(self):
        def _no_wait(fn, **kwargs):
            return call_external(fn, sleep=lambda _s: None, **kwargs)

        p = mock.patch("app.graph.model.call_external", _no_wait)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, llm, **kwargs):
        from app.graph.model import invoke_structured
        with mock.patch("app.graph.model.get_structured_llm", return_value=llm):
            return invoke_structured(_Out, "给我一个场地", **kwargs)

    def test_success_first_try(self):
        llm = _FakeLLM(_ai({"name": "西湖", "count": 2}))
        out = self._run(llm)
        self.assertEqual(out.name, "西湖")
        self.assertEqual(len(llm.sent), 1)

    def test_network_timeout_is_retried(self):
        """网络抖动 -> 退避重发同一份对话(原样重发在这里是对的)"""
        llm = _FakeLLM(httpx.TimeoutException("hang"), _ai({"name": "西湖", "count": 1}))
        out = self._run(llm)
        self.assertEqual(out.count, 1)
        self.assertEqual(len(llm.sent), 2)

    def test_network_failure_gives_up_bounded(self):
        """网络一直坏 -> 有上限地放弃, 抛 AppError(不是无限重试)"""
        llm = _FakeLLM(httpx.ConnectError("refused"))
        with self.assertRaises(TransientError):
            self._run(llm)
        self.assertLessEqual(len(llm.sent), 6)   # 次数有上限, 没失控

    def test_schema_error_sends_field_level_correction(self):
        """参数填错 -> 把"哪个字段、收到什么"回给模型, 而不是原样重发"""
        llm = _FakeLLM(_ai({"name": "西湖", "count": "很多"}),
                       _ai({"name": "西湖", "count": 3}))
        out = self._run(llm)
        self.assertEqual(out.count, 3)

        second = llm.sent[1]
        self.assertEqual(len(second), 3)                    # 原始 + 上一轮回复 + 修正说明
        fix = str(second[-1].content)
        self.assertIn("count", fix)                         # 指出是哪个字段
        self.assertIn("很多", fix)                           # 说明收到了什么
        self.assertNotEqual(str(second[-1].content), "给我一个场地")  # 不是原样重发

    def test_missing_tool_calls_asks_again(self):
        from langchain_core.messages import AIMessage
        llm = _FakeLLM(AIMessage(content="我想了想"), _ai({"name": "西湖", "count": 1}))
        out = self._run(llm)
        self.assertEqual(out.name, "西湖")
        self.assertIn("工具", str(llm.sent[1][-1].content))

    def test_schema_error_does_not_consume_network_retries(self):
        """两类分开计数: schema 不合格是"轮", 不是"网络重试" """
        llm = _FakeLLM(_ai({"name": "西湖", "count": "x"}),
                       _ai({"name": "西湖", "count": 2}))
        self._run(llm)
        self.assertEqual(len(llm.sent), 2)   # 恰好两次请求, 没有被网络层的重试放大

    def test_persistent_schema_error_raises_model_output_error(self):
        llm = _FakeLLM(_ai({"count": "错"}))
        with self.assertRaises(ModelOutputError) as ctx:
            self._run(llm, max_retries=2)
        self.assertTrue(ctx.exception.feed_back_to_model)   # 这类错误该回给模型处理
        self.assertEqual(len(llm.sent), 3)                  # 首发 + 2 轮重填

    def test_programming_defect_is_not_swallowed(self):
        """认不出的异常必须冒出来 —— 不许被"重试几次"掩盖成一次普通的调用失败"""
        llm = _FakeLLM(RuntimeError("代码 bug: 解包数量不对"))
        with self.assertRaises(RuntimeError):
            self._run(llm)
        self.assertEqual(len(llm.sent), 1)                  # 一次都不重试

    def test_total_budget_stops_resending(self):
        """总预算耗尽 -> 停止重填(否则一轮轮叠加会把一次请求拖到几分钟)"""
        clock = {"t": 0.0}

        def _slow():
            clock["t"] += 30.0        # 每次调用凭空消耗 30s
            return _ai({"name": "西湖", "count": "错"})

        llm = _FakeLLM(_slow)
        with mock.patch("app.graph.model.time.monotonic", lambda: clock["t"]):
            with self.assertRaises(ModelOutputError):
                self._run(llm, total_budget=45.0)
        self.assertEqual(len(llm.sent), 2)   # 第 3 轮前预算已尽, 不再发


# ============================ 采集边界(gather_info) ============================


def _state(**parsed):
    base = {"origin": "上海", "destination": "杭州", "date": "2026-10-20"}
    base.update(parsed)
    return {"parsed_request": base, "activity_type": "团建"}


def _poi(name: str, rating: str = "4.5"):
    from app.models.schemas import Location, POIInfo
    return POIInfo(id=name, name=name, type="景点", address="杭州", rating=rating,
                   location=Location(longitude=120.15, latitude=30.27))


def _weather(date: str = "2026-10-20"):
    """客户端返回的是 WeatherInfo 模型(不是裸 dict)—— 假函数也要照原样返回"""
    from app.models.schemas import WeatherInfo
    return [WeatherInfo(date=date, day_weather="晴", night_weather="多云",
                        day_temp=22, night_temp=15)]


def _route():
    from app.models.schemas import RouteInfo
    return RouteInfo(distance=12000.0, duration=40, route_type="driving",
                     description="驾车 40 分钟 12 公里")


def _train(no: str = "G1"):
    return {"train_no": no, "from": "上海虹桥", "to": "杭州东", "depart": "08:00",
            "arrive": "08:45", "duration": "00:45", "seats": {"二等座": "73"}}


class TestGatherInfoBoundary(unittest.IsolatedAsyncioTestCase):
    """采集边界: 一路挂了不能掀翻其余几路, 但也**不能什么都降级**

    旧实现是裸 `asyncio.gather`: 任一路抛异常(高德超时最典型)整个节点炸掉,
    用户拿到 500, 其余几路已经取到的真实数据全部作废。
    """

    def setUp(self):
        # 重试会真的 sleep(退避 0.5s / 1.0s) —— 用例只想验证"重试跑了几次、什么时候
        # 放弃", 不想真等。注入 sleep=空操作: 走的是**真正的** call_external,
        # 退避计算与预算判定全部照旧执行, 只是不消耗时间。
        # (不能 patch time.sleep: call_external 的默认参数在定义时已绑定)
        def _no_wait(fn, **kwargs):
            return call_external(fn, sleep=lambda _s: None, **kwargs)

        p = mock.patch("app.graph.nodes.call_external", _no_wait)
        p.start()
        self.addCleanup(p.stop)

    def _patch_clients(self, **overrides):
        """把三个外部客户端模块的函数全部换成假函数(不触网), overrides 里放要制造异常的几路"""
        from app.graph import amap_client, bocha_client, train_client
        from app.models.schemas import Location

        here = Location(longitude=120.15, latitude=30.27)
        defaults = {
            "resolve_city": lambda *a, **k: {"name": "杭州", "location": here, "level": "城市"},
            "search_venues": lambda *a, **k: [_poi("西湖"), _poi("灵隐", "4.8")],
            "get_weather": lambda *a, **k: _weather(),
            "geocode": lambda *a, **k: here,
            "get_route_info": lambda *a, **k: _route(),
            "query_trains": lambda *a, **k: [_train()],
            "web_search": lambda *a, **k: [{"title": "攻略", "snippet": "不错"}],
        }
        defaults.update(overrides)
        patches = []
        for name, fn in defaults.items():
            mod = (train_client if name == "query_trains"
                   else bocha_client if name == "web_search" else amap_client)
            p = mock.patch.object(mod, name, fn)
            p.start()
            patches.append(p)
            self.addCleanup(p.stop)

    async def test_one_branch_fails_others_survive(self):
        """天气这一路超时 -> 其余几路的数据必须原样保住, 且降级可见"""
        from app.graph.nodes import gather_info

        self._patch_clients(get_weather=raiser(httpx.TimeoutException("weather hang")))
        out = await gather_info(_state())

        basic = out["basic_info"]
        self.assertEqual(len(basic["weather"]), 0)          # 这一路没数据
        self.assertEqual(len(basic["venues"]), 2)           # 其余照旧(这才是重点)
        self.assertEqual(len(basic["train_info"]), 1)
        self.assertEqual(len(basic["guides"]), 1)

        st = out["data_status"]["weather"]
        self.assertEqual(st["status"], "degraded")
        self.assertEqual(st["attempts"], 3)                 # 首发 + 2 次重试
        self.assertIn("降级", "".join(out["steps_log"]))     # 显式标注, 不静默

    async def test_ok_and_degraded_are_both_recorded(self):
        """data_status 是"每路结局"的清单, 不是"只有出事的才记" """
        from app.graph.nodes import gather_info

        self._patch_clients(get_weather=raiser(httpx.ConnectError("refused")))
        out = await gather_info(_state())

        for name in ("resolve_city", "resolve_city_origin", "venues", "weather",
                     "geocode", "trains", "guides", "direction"):
            self.assertIn(name, out["data_status"], f"{name} 没有结局记录")
        self.assertEqual(out["data_status"]["venues"]["status"], "ok")
        self.assertEqual(out["data_status"]["direction"]["status"], "ok")

    async def test_empty_is_not_degraded(self):
        """真没数据(正常业务结果)与调用挂了必须分得开 —— 这是本次重构的核心"""
        from app.graph.nodes import gather_info

        self._patch_clients(search_venues=lambda *a, **k: [])
        out = await gather_info(_state())

        self.assertEqual(out["data_status"]["venues"]["status"], "empty")
        self.assertEqual(out["basic_info"]["venues"], [])
        self.assertNotIn("本次降级", "".join(out["steps_log"]))

    async def test_disabled_capability_is_not_a_fault(self):
        """没配博查 key: 记"未启用", 不记"降级", 流程照跑"""
        from app.graph.nodes import gather_info

        self._patch_clients(
            web_search=raiser(CapabilityDisabled("BOCHA_API_KEY 未配置")))
        out = await gather_info(_state())

        self.assertEqual(out["data_status"]["guides"]["status"], "disabled")
        self.assertNotIn("本次降级", "".join(out["steps_log"]))
        self.assertIn("未启用", "".join(out["steps_log"]))
        self.assertEqual(len(out["basic_info"]["train_info"]), 1)   # 别的路不受影响

    async def test_programming_defect_propagates(self):
        """字段名写错这类 bug 必须冒出来, 不许被降级抹成"这维没数据" """
        from app.graph.nodes import gather_info

        self._patch_clients(search_venues=raiser(ValueError("字段名拼错了")))
        with self.assertRaises(ValueError):
            await gather_info(_state())

    async def test_missing_required_config_raises_not_degrades(self):
        """必需配置缺失必须让节点失败: 六路皆空时下游只会编出一份假方案"""
        from app.graph.nodes import gather_info

        self._patch_clients(
            search_venues=raiser(ConfigError("高德地图API Key未配置")))
        with self.assertRaises(ConfigError):
            await gather_info(_state())

    async def test_resolve_city_failure_falls_back_to_raw_text(self):
        """地名归一失败退回原文并标注 —— 搜偏好过 500, 但必须写明原因"""
        from app.graph.nodes import gather_info

        self._patch_clients(resolve_city=raiser(httpx.TimeoutException("amap hang")))
        out = await gather_info(_state())

        self.assertEqual(out["data_status"]["resolve_city"]["status"], "degraded")
        self.assertEqual(len(out["basic_info"]["venues"]), 2)     # 采集仍继续


# ============================ 工具层(search_nearby_food) ============================


class TestFoodSearchTool(unittest.TestCase):
    """工具返回必须自带状态: 让模型分得清"周边没餐厅"和"搜索挂了"

    旧实现把搜索结果直接 json.dumps 成裸列表, 且工具内部没有保护:
    搜索失败时异常穿出, 整条餐饮路被打挂; 即便没打挂, 模型看到的也只是空列表,
    会把"搜索失败"当成"附近确实没有餐厅", 于是安静地把这一顿按 0 元报出来。
    """

    def _tool(self, **overrides):
        from app.graph import amap_client
        from app.graph.nodes import _make_food_search_tool
        from app.models.schemas import Location

        defaults = {"search_nearby": lambda *a, **k: [_poi("楼外楼", "4.7")]}
        defaults.update(overrides)
        for name, fn in defaults.items():
            p = mock.patch.object(amap_client, name, fn)
            p.start()
            self.addCleanup(p.stop)
        return _make_food_search_tool(Location(longitude=120.15, latitude=30.27))

    def _payload(self, **overrides):
        import json
        return json.loads(self._tool(**overrides).invoke({"keywords": "餐厅"}))

    def test_ok_carries_results(self):
        out = self._payload()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["results"][0]["name"], "楼外楼")

    def test_empty_is_a_business_result(self):
        out = self._payload(search_nearby=lambda *a, **k: [])
        self.assertEqual(out["status"], "empty")
        self.assertEqual(out["results"], [])
        self.assertIn("正常结果", out["note"])       # 别让模型当成故障

    def test_search_failure_is_degraded_not_empty(self):
        """这一条是重点: 搜索失败**不等于**附近没有餐厅"""
        out = self._payload(search_nearby=raiser(TransientError("高德限流", code="amap_quota")))
        self.assertEqual(out["status"], "degraded")
        self.assertEqual(out["results"], [])
        self.assertIn("不代表附近没有餐厅", out["note"])

    def test_tool_does_not_swallow_programming_errors(self):
        u = self._tool(search_nearby=raiser(ValueError("字段名拼错了")))
        with self.assertRaises(ValueError):
            u.invoke({"keywords": "餐厅"})


if __name__ == "__main__":
    unittest.main(verbosity=2)