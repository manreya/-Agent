"""带 TTL 与容量上限的进程级缓存(三个数据客户端共用)

原实现是"调用成功就永久缓存", 有两个真实问题, 本模块一并解决:

1. **负结果毒化**: 三个客户端的失败路径都返回 `[]`/`None`, 而旧装饰器
   "返回值非空就写缓存"——于是一次网络抖动/限流就把这个参数组合毒化整个
   进程生命周期, 之后所有请求都拿空结果(高德降级/博查口碑永空白)直到重启。
   修复: `[]`/`None`/`{}` 等空结果**不写缓存**(负结果只做极短防抖, 防打爆上游)。

2. **无 TTL / 无上限**: 天气/路况/票价是时效数据, 永久缓存会让"雨天+室外"
   风险评估拿到过期预报; 博查 WebSearch 是**按次计费**的调用, TTL 越长越省钱;
   dict 无上限则长跑必然内存涨。修复: 按数据波动率分级 TTL + LRU 容量上限。

3. **冷启动惊群**: 旧实现的 `get` 未命中就直接 `func()`, 而取数期间**不持任何
   锁** —— 同一个参数并发 N 次(N 路采集里两路都要"上海市"的天气、多个用户同时
   查同一座城市), 就会**同时发 N 次外网请求**。而"冷"的时点恰恰是最贵的时点
   (冷启动、LRU 淘汰之后), 且上游一旦限流, 这批请求会一起失败、一起重试, 互相
   加剧 —— 缓存本该在这种时候保护上游, 结果反而放大了压力。
   修复: 同一个 key 只让第一个调用者去打外网, 后来者**等结果并复用**(single-flight)。

用数据波动率决定 TTL(而非一刀切):
    天气/路况/票价  分钟级  -> 短 TTL(30min~3h)
    POI/地理编码/地名  天级  -> 长 TTL(24h)
    博查 WebSearch   内容稳定 + 计费 -> 超长 TTL(12h)

统计口径挂在 /health 上(旧实现只能靠 print 判断是否生效, 无法观测)。

**这个缓存是"进程级"的**: `_REGISTRY` 是模块级全局, 每个进程一份。多 worker 部署
时 N 个进程 = N 份互不可见的缓存(N 倍上游调用、命中率被除以 N)。要想跨进程共享,
得把存储换成 Redis 之类的公共后端 —— 那是另一件事, 本模块不做; 但键里已经预留了
`_KEY_VERSION`, 届时它就是"改了取数逻辑之后旧值必须作废"的那道闸。
"""

import functools
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

# 负结果防抖时长: 空结果也短暂缓存一小会儿, 避免上游故障时被打爆,
# 但远小于最短 TTL, 故障恢复后很快能重新取到真实数据
_NEGATIVE_TTL = 30.0

# LRU 容量上限(条目数): 防止长跑内存无界增长
_MAX_ENTRIES = 512

# 缓存键版本: **改动"上游响应 -> 缓存值"的解析逻辑后, 把它 +1**
#
# 缓存里存的不是原始响应, 而是**解析后的结果**。解析逻辑一改(比如 POI 归属判定、
# 城市归一), 老进程里那些用旧逻辑算出来的值仍然"没过期"(POI 24h), 同一个输入
# 会拿到旧口径的结果 —— 表现为"代码改了但没生效", 且**只对以前查过的输入不生效**,
# 排查时极难看出来。
#
# 今天进程一重启缓存就没了, 所以它主要是一道**防呆 + 为将来铺路**的闸: 一旦缓存
# 换成跨进程共享的后端(Redis), 它就是唯一能让旧值作废的机制。
_KEY_VERSION = "v2"


class TTLCache:
    """线程安全 TTL + LRU 缓存(单实例可被多线程 to_thread 并发调用)"""

    def __init__(self, name: str, ttl: float, max_entries: int = _MAX_ENTRIES):
        self.name = name
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self._data: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.Lock()
        # 每个 key 一把"取数权"锁: 值 -> [锁, 引用计数]。只在 key 未命中时用到,
        # 用完(计数归零)即回收 —— 否则"每个不同入参都留一把锁", dict 本身就成了泄漏。
        # 用 RLock 而非 Lock: 万一将来有人写出"同参数递归调用"的缓存函数, 同线程
        # 重入不会死锁(退化成老行为: 重复取一次), 比直接挂住强。
        self._flights: Dict[str, list] = {}
        self.hits = 0
        self.misses = 0
        self.negative_skips = 0    # 因结果为空而未写入缓存的次数
        self.coalesced = 0         # 因"已有人在取同一个 key"而等待并复用其结果的次数

    @staticmethod
    def _json_default(o: Any) -> Any:
        """缓存键里遇到非 JSON 类型时怎么序列化

        pydantic 模型(amap_client 的 Location / 各种入参模型)优先走 `model_dump()`。
        旧实现一律 `str(o)`, 拿到的是对象的 repr 文本 —— 一旦模型加了个带地址或
        时间戳的字段, repr 就变, **同一个地点算出两个键**, 表现只是"缓存好像没用"
        (静默 0% 命中), 很难查到缓存头上。`model_dump()` 是结构化输出, 而且外层
        json.dumps 的 sort_keys 会递归归一字典键序, 键就与字段声明顺序无关了。

        其余类型仍退回 `str()`: 能跑的历史行为不动, 不为了洁癖让建键过程直接炸掉。
        """
        dump = getattr(o, "model_dump", None)
        if callable(dump):
            try:
                return dump()
            except Exception:      # 防御性: dump 出不来就退回 str, 别让缓存把主流程带崩
                pass
        return str(o)

    @staticmethod
    def _key(func_name: str, args, kwargs) -> str:
        payload = json.dumps([args, kwargs], sort_keys=True, ensure_ascii=False,
                             default=TTLCache._json_default)
        return f"{_KEY_VERSION}:{func_name}:{payload}"

    @staticmethod
    def _is_empty(value: Any) -> bool:
        """负结果判定: None / 空容器 / 空字符串都算(这些都可能是失败降级的产物)"""
        if value is None:
            return True
        if isinstance(value, (list, tuple, set, dict, str)) and len(value) == 0:
            return True
        return False

    def get(self, key: str) -> Optional[tuple]:
        """命中返回 (值, 是否负结果); 未命中或已过期返回 None"""
        return self._get(key, count=True)

    def _get(self, key: str, count: bool = True) -> Optional[tuple]:
        """get 的实现; count=False 用于"等锁之后的二次确认"—— 那次查询不该再计一次 miss

        计时用 `time.monotonic()` 而非 `time.time()`: 后者是墙上时钟, NTP 往回对一次
        表(或夏令时/人工改表), `now - stored_at` 就变成负数, 条目**永远不过期**。
        缓存逻辑只关心"过了多久", 单调时钟才是对的量尺。
        """
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                if count:
                    self.misses += 1
                return None
            stored_at, value, negative = item
            if now - stored_at > (_NEGATIVE_TTL if negative else self.ttl):
                # 过期: 丢弃并继续往下走重新取
                self._data.pop(key, None)
                if count:
                    self.misses += 1
                return None
            # LRU: 命中后移到末尾(最近使用)
            self._data.move_to_end(key)
            if count:
                self.hits += 1
            return value, negative

    def put(self, key: str, value: Any, negative: bool = False) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value, negative)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)   # 淘汰最久未用

    # ---- single-flight: 保证同一个 key 同时只有一个人在打外网 ----

    def acquire_flight(self, key: str) -> tuple:
        """取得该 key 的"取数权", 返回 (锁, 凭据); 用完必须 release_flight

        引用计数是为了**回收**: 不回收的话, 每个用过的入参都永久留一把锁, `_flights`
        比缓存本身还大(缓存有 512 上限, 它没有)。
        """
        with self._lock:
            entry = self._flights.get(key)
            if entry is None:
                entry = [threading.RLock(), 0]
                self._flights[key] = entry
            entry[1] += 1
            return entry[0], entry

    def release_flight(self, key: str, entry: list) -> None:
        with self._lock:
            entry[1] -= 1
            if entry[1] <= 0:
                self._flights.pop(key, None)

    def note_coalesced_hit(self) -> None:
        """等待者拿到别人填好的值: 算一次命中, 并单独计数(证明 single-flight 在干活)"""
        with self._lock:
            self.hits += 1
            self.coalesced += 1

    def clear(self) -> int:
        """清空缓存内容(不动命中统计), 返回清掉的条数

        运维需要手动失效时用(如改了 key 想立刻重取), 测试里也用来隔离用例 ——
        负结果会做 30s 防抖, 用例之间会互相看到上一条缓存的空结果。
        """
        with self._lock:
            n = len(self._data)
            self._data.clear()
            return n

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ttl_seconds": self.ttl,
                "entries": len(self._data),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "negative_skipped": self.negative_skips,
                "coalesced": self.coalesced,
            }


# 名字 -> TTLCache 实例(按被装饰函数名注册, 供 /health 汇总)
_REGISTRY: Dict[str, TTLCache] = {}


def get_cache(name: str) -> Optional[TTLCache]:
    return _REGISTRY.get(name)


def all_stats() -> Dict[str, Dict[str, Any]]:
    """所有已注册缓存的统计(挂 /health 用)"""
    return {name: cache.stats() for name, cache in sorted(_REGISTRY.items())}


def reset_stats() -> None:
    """清零命中统计(不删缓存内容)—— 运维排查时用"""
    for cache in _REGISTRY.values():
        with cache._lock:
            cache.hits = 0
            cache.misses = 0
            cache.negative_skips = 0
            cache.coalesced = 0


def ttl_cache(name: str, ttl: float, max_entries: int = _MAX_ENTRIES,
              log_prefix: str = "cache") -> Callable:
    """缓存装饰器工厂

    Args:
        name:   缓存标识(同名注册到 _REGISTRY, /health 可见); 建议用被装饰函数名
        ttl:    正结果的存活秒数, 按数据波动率给(见模块 docstring)
        log_prefix: 日志前缀(区分是哪个客户端的缓存命中)

    负结果(空容器/None)**不写正缓存**, 只做 _NEGATIVE_TTL 秒防抖, 避免
    "一次网络抖动毒化整个进程"的旧 bug。

    同一个 key 并发未命中时**只放一个人去打外网**, 后来者等它取完直接复用
    (single-flight, 见模块 docstring 第 3 条)。锁是**按 key**的: 不同 key 之间
    照旧全并行, 不会把 N 路采集串行化。

    注意被装饰的函数会被**阻塞等待**(等别人取完) —— 所以它必须在工作线程里调用
    (本项目统一经 `asyncio.to_thread`), 别直接放在事件循环上。
    """
    cache = TTLCache(name, ttl, max_entries)
    _REGISTRY[name] = cache

    def decorator(func):
        def _report_hit(value, negative):
            # 负结果命中: 短期复用, 但不打印 HIT 以免刷屏
            if not negative:
                print(f"[{log_prefix}] HIT {func.__name__} (跳过外网调用)")
            return value

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = TTLCache._key(func.__name__, args, kwargs)

            # 快路径: 命中就直接返回, 路径上没有任何锁竞争
            got = cache.get(key)
            if got is not None:
                return _report_hit(*got)

            # 慢路径: 未命中。先排队拿"取数权", 再二次确认 —— 等锁的这段时间里,
            # 排在前面的那个人很可能已经把值填好了, 那就直接用, 不再发外网请求。
            flight, ticket = cache.acquire_flight(key)
            try:
                with flight:
                    got = cache._get(key, count=False)
                    if got is not None:
                        cache.note_coalesced_hit()
                        print(f"[{log_prefix}] COALESCE {func.__name__} "
                              f"(复用正在进行的请求)")
                        return _report_hit(*got)

                    result = func(*args, **kwargs)
                    empty = TTLCache._is_empty(result)
                    with cache._lock:
                        if empty:
                            cache.negative_skips += 1
                    # 无论正负都写入: 空结果走短 TTL 防抖(负结果不毒化长缓存)
                    cache.put(key, result, negative=empty)
                    return result
            finally:
                # 取数失败(抛异常)也要放行: 排队的人会各自变成下一个"取数人"重试,
                # 不会有人卡在等待上。释放顺序无所谓死锁 —— 两个锁的获取次序恒为
                # "先 flight 后 _lock", 不存在反向持有。
                cache.release_flight(key, ticket)
        # 暴露 cache 实例: 便于测试断言与运维手动清缓存
        wrapper.cache_obj = cache
        return wrapper
    return decorator
