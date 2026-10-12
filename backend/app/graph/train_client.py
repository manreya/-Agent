"""聚合数据 12306 火车票查询(站到站, 含班次/票价)

高铁/火车真实票价的第三个数据源: 12306 无官方公开免费 API,
走聚合数据(https://www.juhe.cn)的 12306 火车票查询接口, 数据官方直连。

失败语义(本轮统一, 见 app/graph/errors.py) —— 注意区分"正常业务结果"和"故障":
  - 未配置 JUHE_TRAIN_KEY -> 抛 CapabilityDisabled(可选能力未开, 不是故障),
    由 call_external 映射成 CallStatus.DISABLED; 票价由 LLM 常识估算并标注。
  - 日期缺失 / 无法解析 / 已过期 / 超出 15 天预售期 -> **返回 []**。
    这些是**正常业务结果**(接口本就不支持), 不是错误, 不该重试、不该报警。
  - 网络/超时/5xx/非 JSON   -> **原样抛出**, 由 call_external 分类并退避重试。
    旧实现这里 `except Exception: return []`, 把"聚合挂了"和"没有班次"
    抹成同一个空值。
  - 接口业务 error_code 非 0 -> 抛 TransientError(预售期外的 281701 已在上游
    本地挡掉, 到这里的多为配额/临时故障)。
  - 查到了但没有班次        -> 返回 [], 正常业务结果。

接口: GET https://apis.juhe.cn/fapigw/train/query
参数: key / search_type=1(站名) / departure_station / arrival_station /
      date(仅允许15天内) / filter=G(高铁城际) 等
计费: 约 0.02 元/次, 普通会员每天 10 次免费; 结果同参数进程级缓存。
"""

from datetime import date as _date, datetime
from typing import List, Optional

from ..config import get_settings
from .errors import DEFAULT_HTTP_TIMEOUT, CapabilityDisabled, TransientError
from .ttl_cache import ttl_cache

# 12306 预售期(含当日): 超出窗口的查询接口必然报错 281701, 本地先挡掉省一次接口费
TRAIN_PRESALE_DAYS = 15

# 进程级缓存 TTL: 票价/时刻会随调图/浮动定价调整, 且高德式永久缓存会让
# 用户看到隔天陈旧票价; 30 分钟足够挡住同一波四路并行里的重复查询
_TRAIN_TTL = 30 * 60


def _cached(func):
    """火车查询缓存(走公共 ttl_cache)

    旧实现"返回值非空就永久缓存": 未配 key / 超预售期 / 网络失败都返回 [],
    一次失败就会把该参数组合毒化整个进程。ttl_cache 对空结果只做 30s 防抖;
    而**异常不会进缓存** —— 失败不再被记成"这条线路没有班次"。
    """
    return ttl_cache(func.__name__, _TRAIN_TTL, log_prefix="train_cache")(func)


def _extract_seats(item: dict) -> dict:
    """从班次里取各席别票价/余票 -> {席别名: {price, num}}

    聚合数据把票价放在**嵌套数组** prices:[{seat_name, price, num}] 里, 不是早期
    猜的平铺字段(二等座/ze_price/...)。字段名猜错时接口照样 error_code=0 返回班次,
    但 seats 恒为空 —— 真实票价拿到了却在解析这步丢光, 属于最隐蔽的"成功但没数据"。
    """
    seats = {}
    for p in (item.get("prices") or []):
        name = str(p.get("seat_name") or "").strip()
        if not name:
            continue
        price = p.get("price")
        num = p.get("num")
        seats[name] = {
            "price": price if price not in (None, "", "--") else None,
            "num": str(num) if num not in (None, "") else None,
        }
    return seats


@_cached
def query_trains(from_city: str, to_city: str, date: Optional[str] = None,
                 train_filter: str = "G,D") -> List[dict]:
    """站到站火车班次查询: 返回精简班次列表 [{train_no, from, to, depart, arrive, duration, seats}]

    - 未配置 key: 抛 CapabilityDisabled(可选能力未开)
    - date 缺失 / 超出15天窗口 / 已过期: 返回 [](接口不支持, 正常业务结果)
      —— 预售期在本地先算, 不再把注定报错 281701 的请求发出去白花一次接口费
    - 网络失败/解析失败: 抛出分类异常, 由调用层决定重试与降级
    - from_city/to_city 传城市名(如 上海/杭州), 接口按站名匹配主流站
    """
    settings = get_settings()
    if not settings.juhe_train_key:
        raise CapabilityDisabled("JUHE_TRAIN_KEY 未配置, 火车真实票价不可用(改由 LLM 估算)")

    if not date:
        print("[train] 活动日期缺失, 跳过真实票价查询")
        return []

    try:
        target = datetime.strptime(str(date)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        # 只捕日期解析 —— 这是我知道怎么处理的(跳过查询, 交 LLM 估算)
        print(f"[train] 活动日期无法解析({date!r}), 跳过真实票价查询")
        return []
    days_ahead = (target - _date.today()).days
    if days_ahead < 0:
        print(f"[train] 活动日 {date} 已过期, 跳过真实票价查询")
        return []
    if days_ahead > TRAIN_PRESALE_DAYS:
        print(f"[train] 活动日 {date} 距今天 {days_ahead} 天, 超出 {TRAIN_PRESALE_DAYS} 天预售期, "
              f"跳过查询(票价由 LLM 按里程估算并标注'以12306为准')")
        return []

    import httpx

    # 网络/HTTP/JSON 解析异常一律不捕 -> 交给 call_external 分类
    resp = httpx.get(
        "https://apis.juhe.cn/fapigw/train/query",
        params={
            "key": settings.juhe_train_key,
            "search_type": "1",
            "departure_station": from_city,
            "arrival_station": to_city,
            "date": date,
            "filter": train_filter,
            "enable_booking": "1",
        },
        timeout=DEFAULT_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("error_code") not in (0, "0"):
        raise TransientError(
            f"[train] 接口返回异常: {data.get('error_code')} {data.get('reason', '')}",
            code="train_business",
        )

    trains = data.get("result") or []
    results = []
    # 只取前12个(接口按发车时间返回), 排序(_rank_trains)与截断(6)由 gather_info 完成——
    # 先排序再截断, 否则按时长排序无意义
    for t in trains[:12]:
        results.append({
            "train_no": t.get("train_no") or "",
            # 真实站点名(如"上海松江"/"杭州南"); 缺字段才退回传入的城市名
            "from": t.get("departure_station") or from_city,
            "to": t.get("arrival_station") or to_city,
            "depart": t.get("departure_time") or "",
            "arrive": t.get("arrival_time") or "",
            # duration 形如"01:47"(时:分), _rank_trains 正是按这个格式拆分排序
            "duration": t.get("duration") or "",
            "seats": _extract_seats(t),
        })
    print(f"[train] {from_city}->{to_city} {date} 命中{len(results)}个班次")
    return results