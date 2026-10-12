# -*- coding: utf-8 -*-
"""离线单元测试: 进程级 TTL 缓存(aapp/graph/ttl_cache.py)

只测缓存模块本身, 不碰任何客户端。重点锁住三件"改坏了不会报错、只会悄悄变贵"
的事:

1. **single-flight**: 同一个 key 并发未命中时只能有一个人打外网。旧实现是
   "谁未命中谁就去取", 6 路采集里两路撞到同一个键(或多用户同时查同一座城市)
   就是重复请求; 上游限流时这批请求还会一起失败、一起重试。这条防线坏掉的表现
   只是"账单变高 + 偶发限流", 不会有任何报错 —— 所以必须有测试钉住。
2. **不同 key 依然并行**: single-flight 最容易被写错的方式是"一把全局大锁",
   那样 6 路采集会退化成串行(总耗时从最慢的一路变成六路之和)。用 barrier 证明
   真实并行, 而不是"看起来对"。
3. **取数人失败不能把等待者挂住**: 缓存函数抛异常时必须放行排队的人。

外加两条键构造的约束(pydantic 走 model_dump、键带版本号)与时钟选择
(单调时钟, 不受墙上时钟回拨影响)。

无第三方依赖: 标准库 unittest + threading。

运行:
    cd backend && py -3.13 -m unittest discover -s tests -v
"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel

from app.graph import ttl_cache
from app.graph.ttl_cache import TTLCache, ttl_cache as ttl_cache_deco


class _Point(BaseModel):
    """充当 amap_client.Location 的角色: 缓存函数确实会直接收模型对象"""

    longitude: float
    latitude: float


def _cached(name, ttl=60.0, **kw):
    """装一个专用的被装饰函数, 名字带后缀以免和别的用例共用同一个 TTLCache"""
    return ttl_cache_deco(name, ttl, log_prefix="unit", **kw)


class _Threads:
    """跑线程并收结果的小工具: 把"别挂住"写成可断言的超时, 而不是让测试永远转"""

    def __init__(self):
        self.values = {}
        self.errors = {}

    def run(self, n, target, timeout=5.0):
        barrier = threading.Barrier(n + 1, timeout=timeout)   # n 个工人 + 主线程

        def body(i):
            barrier.wait()          # 同时起跑: 否则先跑的早就取完了, 撞不上
            try:
                self.values[i] = target(i)
            except BaseException as exc:      # noqa: BLE001 - 要连 AssertionError 一起收
                self.errors[i] = exc

        threads = [threading.Thread(target=body, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(timeout=timeout)
        alive = [t for t in threads if t.is_alive()]
        if alive:
            raise AssertionError(f"{len(alive)} 个线程超时未返回(疑似卡在等待上)")
        return self


class TestSingleFlight(unittest.TestCase):
    """同一个 key: 只让一个人打外网, 后来者等结果"""

    def test_concurrent_misses_make_one_upstream_call(self):
        calls = []
        calls_lock = threading.Lock()

        def slow(q):
            with calls_lock:
                calls.append(q)
            time.sleep(0.2)          # 取数期间别人必须撞在锁上, 而不是也去取
            return f"结果-{q}"

        cached = _cached("__unit_flight_ok")(slow)
        got = _Threads().run(8, lambda i: cached("杭州")).values

        self.assertEqual(list(got.values()), ["结果-杭州"] * 8, "所有调用者都该拿到同一份值")
        self.assertEqual(len(calls), 1, f"只该打一次外网, 实际 {len(calls)} 次")
        cache = cached.cache_obj
        # hits 含"复用别人结果"的那些(note_coalesced_hit 会同时 +1 hits 和 +1 coalesced):
        # 除取数人之外的 7 个都该从缓存拿, 且都该是"等别人取完"而不是各自命中
        self.assertEqual(cache.hits, 7, "除取数人外的 7 个都该从缓存拿到值")
        self.assertEqual(cache.coalesced, 7, "而且都该是复用同一次请求的结果")
        self.assertEqual(cache._flights, {}, "取数权锁要回收, 否则每次新入参都留一把锁")

    def test_negative_result_also_coalesces(self):
        """负结果走的是 30s 防抖缓存 —— 等待者复用同一个空结果, 不再各自打一遍

        这条要紧: 上游故障时失败路径返回值就是空, 若这里不合并, "防抖"反而是每次
        并发都真打一遍外网, 恰好在上游最脆弱时放大压力。
        """
        calls = []
        calls_lock = threading.Lock()

        def fails_empty(q):
            with calls_lock:
                calls.append(q)
            time.sleep(0.2)
            return []

        cached = _cached("__unit_flight_empty")(fails_empty)
        got = _Threads().run(6, lambda i: cached("上海")).values

        self.assertEqual(list(got.values()), [[]] * 6)
        self.assertEqual(len(calls), 1, "空结果也要合并, 只打一次")
        self.assertEqual(cached.cache_obj.negative_skips, 1)

    def test_other_keys_still_run_in_parallel(self):
        """不同 key 必须真并行: 用 barrier 证明三个请求同时在途

        若 single-flight 被写成一把全局锁, 第一个请求会卡在 barrier 上等另外两个,
        而它们正被同一把锁挡着 —— barrier 超时(BrokenBarrierError) -> 用例失败。
        """
        n = 3
        barrier = threading.Barrier(n, timeout=3.0)

        def rendezvous(q):
            # 只有三个请求同时在跑, 这里才过得去
            barrier.wait()
            return q

        cached = ttl_cache_deco("__unit_flight_parallel", 60.0, log_prefix="unit")(
            rendezvous)
        got = _Threads().run(n, lambda i: cached(f"城市{i}")).values

        self.assertEqual(got, {i: f"城市{i}" for i in range(n)})
        self.assertEqual(cached.cache_obj._flights, {}, "取数权锁要回收")

    def test_leader_failure_does_not_park_the_waiters(self):
        """取数人抛异常 -> 排队的人必须各自往下走(重试), 不能卡死在等待上

        语义: 缓存不吞异常。失败没有可复用的值, 等待者只能各自再试一次 —— 区别在于
        它们是**串行**去试(锁排着队), 不是同时扑上去。
        """
        calls = []
        calls_lock = threading.Lock()

        def flaky(q):
            with calls_lock:
                calls.append(q)
                attempt = len(calls)
            time.sleep(0.1)
            if attempt == 1:
                raise RuntimeError("第一次取数失败")
            return "ok"

        cached = _cached("__unit_flight_raise")(flaky)
        out = _Threads().run(4, lambda i: cached("北京"))

        self.assertEqual(len(out.errors), 1, "只有恰好一个线程该看到异常")
        self.assertIsInstance(list(out.errors.values())[0], RuntimeError)
        self.assertEqual(sorted(out.values.values()), ["ok"] * 3)
        # 第一次失败 -> 第二个排队者成为新的取数人 -> 剩下的复用它
        self.assertEqual(len(calls), 2)
        self.assertEqual(cached.cache_obj._flights, {})


class TestKeyConstruction(unittest.TestCase):
    """键要稳定且带版本 —— 这两条坏掉都是"缓存静默失效", 不会报错"""

    def test_pydantic_args_are_keyed_by_field_values(self):
        """模型对象走 model_dump(), 不靠 repr 文本: 相等实例必须算出同一个键"""
        k1 = TTLCache._key("geocode", (_Point(longitude=120.15, latitude=30.28),), {})
        k2 = TTLCache._key("geocode", (_Point(longitude=120.15, latitude=30.28),), {})

        self.assertEqual(k1, k2)
        self.assertIn('"longitude": 120.15', k1)
        self.assertNotIn("longitude=", k1, "不该退回 repr 文本(str(obj) 的形态)")

    def test_pydantic_arg_hits_cache_across_instances(self):
        """功能面: 两个"长得一样"的模型实例必须命中同一条缓存(真实调用形态)

        amap_client.get_route_info / search_nearby 收的就是模型对象, 每次调用都是
        新构造的实例 —— 键要按字段值算, 否则命中率恒为 0。
        """
        n = []

        def probe(point, kind):
            n.append(1)
            return f"{kind}:{point.longitude}"

        cached = _cached("__unit_model_key")(probe)
        a = cached(_Point(longitude=120.15, latitude=30.28), "driving")
        b = cached(_Point(longitude=120.15, latitude=30.28), "driving")

        self.assertEqual(a, b)
        self.assertEqual(len(n), 1, "第二次必须命中缓存(键按字段值算)")

    def test_key_carries_version(self):
        """键里必须带版本号: 改了取数解析逻辑, 靠 +1 让旧值作废(见模块说明)"""
        key = TTLCache._key("get_weather", ("杭州",), {})
        self.assertTrue(key.startswith(f"{ttl_cache._KEY_VERSION}:"), key)

    def test_unknown_types_still_fall_back_to_str(self):
        """非模型对象退回 str(): 兼容历史行为, 别让建键把主流程带崩"""
        class Opaque:
            def __str__(self):
                return "不透明对象"

        key = TTLCache._key("f", (Opaque(),), {})
        self.assertIn("不透明对象", key)


class TestClock(unittest.TestCase):
    """计时用单调时钟: 墙上时钟往回跳一次, 旧实现会让条目永不过期"""

    def test_expiry_is_driven_by_monotonic_clock(self):
        cache = TTLCache("__unit_clock", 10.0)
        now = [1000.0]
        with mock.patch.object(ttl_cache.time, "monotonic", lambda: now[0]):
            cache.put("k", "v")
            self.assertEqual(cache.get("k"), ("v", False))

            now[0] += 11.0                      # 超过 TTL
            self.assertIsNone(cache.get("k"), "过了 TTL 就该失效")

    def test_wall_clock_is_never_consulted(self):
        """直接钉住实现: get/put 一次都不许碰 time.time()

        这条是回归测试的行为化写法 —— 墙上时钟回拨(NTP 对表、人工改表)只会让
        now - stored_at 变小甚至变负, 症状是"缓存不清了", 极难定位。
        """
        cache = TTLCache("__unit_wall_clock", 10.0)
        wall = mock.Mock(side_effect=AssertionError("缓存不该用墙上时钟"))
        with mock.patch.object(ttl_cache.time, "time", wall):
            cache.put("k", "v")
            cache.get("k")

        wall.assert_not_called()


if __name__ == "__main__":
    unittest.main()