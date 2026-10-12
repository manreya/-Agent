"""高德地图调用与结果解析

统一封装: REST 直连 -> 结果解析 -> Pydantic 模型。
(原 MCP 分支保留但已不被主流程使用: MCP client 依赖 uvx 子进程, 见 _get_mcp_tool。
 原 MCP 调用入口 call_amap 已删除 —— 它没有任何调用方, 而且 `except Exception:
 返回 {}` 正是本次要消灭的那种写法: 把"接口挂了"和"真没数据"抹成同一个空值。)

本模块同时承担"地名可信性"的第一道闸门, 针对三类真实踩过的坑:
1. 城市参数非法/带修饰(如"杭州西湖区")时, 高德 place/text 会**静默忽略**
   citylimit 返回全国混排结果(实测首条落在新疆伊犁, 距杭州 3505km)。
   -> resolve_city() 先把自由文本地名归一到规范城市名, search_venues() 再按
      adcode/cityname 做归属过滤, 过滤掉的绝不进候选池。
2. 目的地是"具体地点"而不是城市时(莫干山/乌镇/千岛湖/朱家角/临安/古北水镇),
   把地名当 city 传参同样会拿到全国混排(实测"莫干山" 15 条候选全在新疆/云南,
   距离中位 1362km), 而归属过滤会把这些必然不匹配的结果**全部删掉** -> 空池 ->
   方案写"地点待定"。这是"修坑 1 时顺手造出来的新坑": 过滤方向对, 但前提
   (目的地是个城市)不成立。
   -> resolve_city() 返回 kind(city/place) 区分两种口径; 具体地点改用
      "{地名} {关键词}"检索 + 同城 + 半径过滤, 不再传 city/citylimit。
3. geocode 无城市上下文时, 歧义地名(陆家嘴/中关村/新街口/西湖区/朝阳区)
   会落到外省村庄级点位(实测"陆家嘴"->云南昆明, 距离偏差 1964km)。
   -> geocode() 支持 city(上下文城市提示) 与 near(参照点) 双重消歧:
      先用 city 限定; 该城市没有此地名时回落全国, 并在多个候选里选离
      near(通常是目的地)最近的一个。

**geocode 对坑 2 无能为力**, 这点值得记住: 实测 geocode("莫干山") 的 10 条记录
全是甘肃/广西的同名住宅区, geocode("乌镇") 全是陕西/西藏的同名乡镇 —— 景区/
古镇这类名字在 geocode 里根本没有"正确的那一条"。能认出它们的是 place/text 的
全国热度首位(莫干山风景名胜区/乌镇风景区), 所以 _canon_from_hot_poi 才是这类
地名的唯一出路。
"""

import functools
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_settings
from ..models.schemas import Location, POIInfo, RouteInfo, WeatherInfo
from .errors import (
    DEFAULT_HTTP_TIMEOUT,
    AppError,
    CallStatus,
    ConfigError,
    PermanentError,
    TransientError,
    call_external,
)
from .ttl_cache import all_stats as cache_stats, ttl_cache

_mcp_tool = None

# 行政区划后缀: 用于比较"杭州"/"杭州市"/"浙江省杭州市"是否同一城市
_ADMIN_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "盟", "市", "省", "县", "区")
# 地名里的冗余修饰词: resolve_city 首次解析失败时剥掉再试
_LOCALITY_NOISE = ("周边", "附近", "一带", "同城", "地区", "市内", "城区", "当地")

# 地理编码可信度低的 level(高德把同名村庄/兴趣点也返回, 极易落到外省)
_UNRELIABLE_LEVELS = ("村庄", "兴趣点", "未知", "")

# geocode 的 level 里, 只有这两个等级代表"地名本身就是一个城市"(整市范围可检)
_CITY_LEVELS = ("省", "市")
# place/text 的 type 尾段里代表"这是一个城市"的分类(实测 "杭州" -> 地市级地名)
_CITY_TYPE_TAILS = ("省级地名", "地市级地名")
# 具体地点(景区/古镇/离岛/区县)检索半径(km): 超出视为邻市或远郊, 不进候选池。
# 实测真实候选落在 0~30km(千岛湖 8~15 / 乌镇 0~10 / 西湖区 13~19), 而错配候选
# 落在 46~138km(临安 -> 滨江萧山, 古北水镇 -> 海淀)。35 是这两簇之间的空档。
_SCOPE_RADIUS_KM = 35.0
# 具体地点口径下, "{地名} {关键词}" 召回不足该数时补一次"{地名}"宽搜
# (实测 "莫干山 团建场地" 仅 1 条, 而 "莫干山" 有 15 条, 全是 35km 内的真候选)
_SCOPE_KEYWORD_MIN = 2

# 坐标正确但语义上不可能当团建场地的 POI: 宽搜"{地名}"会把这些一并带回
# (实测古北水镇带回 公交站/停车场/游客中心, 临安带回 地铁站, 崇明岛带回道路名),
# 混进候选池会让提案把"地铁站"写成活动地点。按高德 type 的两段判断:
#   大类命中前缀(交通设施服务/道路附属设施)一律剔除 —— 地铁站/公交站/停车场/
#   长途汽车站/收费站/服务区全在这一层, 逐个列名字会漏(实测"公交站"漏掉了
#   "公交车站");
#   少数散在别的类目下的(道路名在"地名地址信息;交通地名"下)按子串兜住。
_NON_VENUE_TYPE_PREFIXES = ("交通设施服务", "道路附属设施")
_NON_VENUE_TYPE_HINTS = ("交通地名", "售票处", "信息咨询中心", "公共厕所", "加油站")

# 出发地离目的地超过该距离(km)且 level 不可信时, 显式告警(不静默产出两千公里的驾车报价)
_IMPLAUSIBLE_ORIGIN_KM = 300.0

# ========== 缓存 TTL 分级(按数据波动率, 见 ttl_cache 模块说明) ==========
# POI/地名/地理编码: 天级变化 -> 24h
_AMAP_TTL_DEFAULT = 24 * 3600
# 天气预报: 高德预报仅 3-4 天且频繁刷新, 过期预报会让 risk 节点"雨天+室外"误判 -> 30min
_AMAP_TTL_WEATHER = 30 * 60
# 驾车路线: 受实时路况影响, 转场耗时不能给隔天的数 -> 3h
_AMAP_TTL_ROUTE = 3 * 3600


def amap_cache(func):
    """高德工具调用缓存装饰器(默认 TTL 24h, 适用于 POI/地名类天级数据)

    内部走公共 ttl_cache: 空结果(网络失败降级产物)只做 30s 防抖不写长缓存 ——
    旧实现"返回值非空就永久缓存"会被一次网络抖动毒化整个进程生命周期。
    天气/路线等时效数据用 amap_cache_ttl 单独指定更短的 TTL。
    """
    return ttl_cache(func.__name__, _AMAP_TTL_DEFAULT, log_prefix="amap_cache")(func)


def amap_cache_ttl(ttl: float):
    """高德缓存装饰器(指定 TTL): 用于天气/路线等时效数据"""
    def decorator(func):
        return ttl_cache(func.__name__, ttl, log_prefix="amap_cache")(func)
    return decorator


# ============ 字段容错与地名归一 ============

def _as_str(value: Any) -> str:
    """高德空字段形态不一(``[]`` / ``""`` / ``None``), 统一取字符串

    实测 biz_ext.cost / district / address 都可能是空列表。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ",".join(str(v) for v in value if isinstance(v, (str, int, float))).strip()
    if value is None:
        return ""
    return str(value).strip()


def _norm_admin(name: Any) -> str:
    """行政区名归一(去掉 市/省/自治州 等后缀), 用于同一城市比较"""
    s = _as_str(name)
    for suffix in _ADMIN_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def _strip_locality_words(name: str) -> str:
    """剥掉"周边/附近/一带"等修饰词(如 "杭州周边" -> "杭州")"""
    out = name
    for word in _LOCALITY_NOISE:
        out = out.replace(word, "")
    return out.strip()


def _distance_km(a: Optional[Location], b: Optional[Location]) -> Optional[float]:
    """两点球面距离(km); 任一为空返回 None"""
    if a is None or b is None:
        return None
    p1, p2 = math.radians(a.latitude), math.radians(b.latitude)
    dphi = p2 - p1
    dlambda = math.radians(b.longitude - a.longitude)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _resolve_uvx() -> str:
    """定位 uvx 可执行文件: PATH -> 当前Python的Scripts目录 -> uv默认安装位置

    注: 旧的 AmapService 直接写死 "uvx", PATH 上没有时整条链路静默失效;
    本函数是唯一正确的 uvx 定位方式, 新代码请统一用它。
    """
    found = shutil.which("uvx")
    if found:
        return found
    candidates = [
        Path(sys.executable).parent / "Scripts" / "uvx.exe",
        Path.home() / ".local" / "bin" / "uvx.exe",
        Path.home() / ".cargo" / "bin" / "uvx.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "找不到 uvx 命令, 请先安装 uv: pip install uv"
    )


def _get_mcp_tool():
    """懒加载 MCPTool 单例(首次调用时才启动 MCP server)

    说明: 主流程已全部改走 REST 直连(见模块 docstring), 该分支仅供
    历史接口兜底; 需要 uvx + amap-mcp-server 才能工作。
    """
    global _mcp_tool
    if _mcp_tool is None:
        from ..integrations.mcp import MCPTool

        settings = get_settings()
        if not settings.amap_api_key:
            raise ValueError("高德地图API Key未配置,请在.env文件中设置AMAP_API_KEY")

        _mcp_tool = MCPTool(
            name="amap",
            description="高德地图服务",
            server_command=[_resolve_uvx(), "amap-mcp-server"],
            env={"AMAP_MAPS_API_KEY": settings.amap_api_key},
            auto_expand=True,
        )
        print("✅ 高德MCP工具初始化成功")
    return _mcp_tool


def _parse_location(raw: Any) -> Optional[Location]:
    """解析高德 'lng,lat' 字符串为 Location"""
    if isinstance(raw, dict):
        try:
            return Location(longitude=float(raw["longitude"]), latitude=float(raw["latitude"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, str) and "," in raw:
        try:
            lng, lat = raw.split(",", 1)
            return Location(longitude=float(lng), latitude=float(lat))
        except ValueError:
            return None
    return None


def _amap_status_error(path: str, info: str) -> AppError:
    """高德业务错误码 -> 分类

    status != "1" 一定是错误(无结果时高德仍返回 status=1 + 空列表), 但要区分
    "这把 key 不行"(永久, 重试无意义) 和 "打太快了"(限流, 退避重试有意义)。
    """
    upper = info.upper()
    # 频率/并发类: 短时间内可恢复 -> 交给 call_external 退避重试
    if any(k in upper for k in ("CUQPS", "FREQUENCY", "QUOTA", "CONCURRENT")):
        return TransientError(f"[amap.{path}] 限流/配额: {info}", code="amap_quota")
    # 凭据/权限/日额度耗尽: 重试无意义, 必须冒出来被人看到
    if any(k in upper for k in ("INVALID_USER_KEY", "USERKEY_PLAT_NOMATCH",
                                "INVALID_USER_SCODE", "SERVICE_NOT_AVAILABLE",
                                "INVALID_USER_IP", "OVER_LIMIT", "DAILY")):
        return PermanentError(f"[amap.{path}] 凭据/权限错误: {info}", code="amap_auth")
    # 认不出具体码: 按可恢复处理(有次数与总时长上限兜底, 不会无限重试)
    return TransientError(f"[amap.{path}] 业务错误: {info}", code="amap_business")


def _rest_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """高德 REST API 直连(用于 MCP 返回缺字段的接口, 如 text_search 不含坐标)

    失败语义(本模块统一): **所有失败都以分类异常抛出**, 不再返回 {} 静默降级 ——
    否则"接口挂了"和"这个城市没有场地"在下游永远分不出来。
      - 缺 key        -> ConfigError(永久)
      - 网络/超时/5xx -> TransientError, 由 call_external 退避重试
      - 业务错误码    -> 按 info 分类(限流可重试 / 凭据问题永久)

    重试策略: 2 次 + 指数退避, 而 retry_budget=6s 兜住总时长 —— 限流这类业务错误
    是**立即**返回的, 6 秒预算足够退避两次; 而真正的超时(单次 10s)会直接超出预算
    而放弃重试, 避免把用户等待翻倍。这正是"总时长上限不能省"的用处。
    """
    import httpx

    settings = get_settings()
    if not settings.amap_api_key:
        raise ConfigError("高德地图API Key未配置, 请在.env文件中设置AMAP_API_KEY")

    def _once() -> Dict[str, Any]:
        resp = httpx.get(
            f"https://restapi.amap.com/v3/{path}",
            params={**params, "key": settings.amap_api_key},
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "1":
            return data
        # 业务错误: 抛分类异常, 由 call_external 决定重不重试
        raise _amap_status_error(path, str(data.get("info") or ""))

    result = call_external(_once, kind=f"amap.{path}", retries=2,
                           backoff=0.8, retry_budget=6.0)
    if result.usable and isinstance(result.value, dict):
        return result.value
    # 降级/失败: 向上抛, 由调用边界统一处置(不再在这里静默成 {})
    raise result.error or TransientError(f"[amap.{path}] 调用未成功: {result.reason}")


# ============ 地理编码(带消歧) ============

def _geocode_records(address: str, city: Optional[str] = None) -> List[dict]:
    """调 geocode/geo, 返回原始记录列表(失败/无结果返回 [])"""
    params: Dict[str, Any] = {"address": address}
    if city:
        params["city"] = city
    data = _rest_get("geocode/geo", params)
    return [g for g in (data.get("geocodes") or []) if isinstance(g, dict)]


def _record_location(record: dict) -> Optional[Location]:
    return _parse_location(record.get("location"))


def _name_related(query: str, *candidates: Any) -> bool:
    """判断解析结果与用户输入是否是同一个地方(含被包含关系)

    为什么需要: 高德 geocode 会**模糊匹配**——把 "不存在的城市名xyz" 解成
    锡林郭勒盟, 于是后续 place/text 会一本正经地返回内蒙古的场地。加一道
    "解析出的省/市/区必须与输入有包含关系"的校验即可拦住这类输入。
    例: "杭州西湖区"->杭州市 ✓(被包含); "临安"->杭州市/临安区 ✓(区县同名);
        "不存在的城市名xyz"->锡林郭勒盟 ✗(无交集)。
    """
    q = _norm_admin(_strip_locality_words(_as_str(query)))
    if not q:
        return False
    for cand in candidates:
        c = _norm_admin(cand)
        if not c:
            continue
        if q == c or q in c or c in q:
            return True
    return False


def _kind_of(level: str = "", poi_type: str = "") -> str:
    """目的地的检索口径: "city"=整市范围, "place"=具体地点(景区/古镇/区县)

    为什么要分: 用户写的"杭州"和"莫干山"是两种东西 —— 前者该在全市搜场地,
    后者该在它周围 35km 内搜。把后者当城市查, 高德会静默返回全国混排。
    """
    if _as_str(level) in _CITY_LEVELS:
        return "city"
    tail = _as_str(poi_type).split(";")[-1]
    return "city" if tail in _CITY_TYPE_TAILS else "place"


def _canon(name: str, query: str, *, province: str, city: str, district: str,
           adcode: str, level: str, location: Optional[Location],
           poi_type: str = "", source: str) -> Dict[str, Any]:
    """组装目的地归一结果(两个来源共用同一套字段, 避免下游出现两套形态)"""
    city_name = city or province
    kind = _kind_of(level, poi_type)
    where = f"{province}{city_name}{district}"
    # display 给人看/进 prompt: 具体地点带上行政归属("莫干山（浙江省湖州市德清县）"),
    # 城市本身就不必画蛇添足
    display = f"{query}（{where}）" if kind == "place" and where and query != city_name \
        else (city_name or query)
    return {
        "name": city_name or query,
        "adcode": adcode,
        "province": province,
        "district": district,
        "level": level,
        "location": location,
        "kind": kind,
        "display": display,
        "source": source,          # "geocode" | "poi": 留痕, 便于诊断与断言
        "query": query,
    }


def _canon_from_geocode(query: str, records: List[dict]) -> Optional[Dict[str, Any]]:
    """geocode 口径归一(城市/区县名走这条); 与输入无包含关系时返回 None 交给下一段"""
    if not records:
        return None

    # 优先取行政区级别最高的记录(省/市), 避免命中同名村庄
    def _rank(rec: dict) -> int:
        level = _as_str(rec.get("level"))
        return {"省": 0, "市": 1, "区县": 2}.get(level, 3)

    ordered = sorted(records, key=_rank)
    record = ordered[0]

    # 跨城重名(如"西湖区"在杭州与南昌都有): 用 place/text 的全国热度首位消歧,
    # 与 geocode 消歧同一套判据, 避免用户想写杭州却被解到南昌。
    distinct = {_norm_admin(r.get("city") or r.get("province")) for r in ordered}
    distinct.discard("")
    if len(distinct) > 1:
        hot = _hot_poi(query)
        if hot is not None:
            where = f"{_as_str(hot.get('province'))}{_as_str(hot.get('city'))}{_as_str(hot.get('district'))}"
            for rec in ordered:
                rcity = _norm_admin(rec.get("city") or rec.get("province"))
                if rcity and rcity in where:
                    if rec is not record:
                        print(f"[resolve_city] 「{query}」跨城重名, 按 POI 热度首位改判为 "
                              f"{_as_str(rec.get('province'))}{_as_str(rec.get('city'))}")
                    record = rec
                    break

    city_name = _as_str(record.get("city")) or _as_str(record.get("province"))
    district = _as_str(record.get("district"))
    province = _as_str(record.get("province"))

    if not _name_related(query, city_name, province, district):
        return None
    return _canon(query, query, province=province, city=city_name, district=district,
                  adcode=_as_str(record.get("adcode")), level=_as_str(record.get("level")),
                  location=_record_location(record), source="geocode")


def _canon_from_hot_poi(original: str, query: str) -> Optional[Dict[str, Any]]:
    """非城市地名(景区/古镇/离岛)归一: 用全国热度首位 POI 反查行政归属

    为什么不能只靠 geocode: 实测 geocode("莫干山") 返回的 10 条**全是甘肃/广西的
    同名住宅区**, geocode("乌镇") 全是陕西/西藏的同名乡镇 —— 这类地名的 geocode
    结果整体不可用。而 place/text 的热度首位是 "莫干山风景名胜区"(浙江湖州德清),
    每次都准。所以先拿它反查归属, 再照常返回结构化字段。

    闸门: 热度首位 POI 的**名字**必须与输入有包含关系。这一道拦住的是"高德模糊
    匹配到别处"—— 实测 "不存在的城市名xyz" -> 烟袋斜街(北京),
    "阿巴阿巴阿巴" -> 阿巴义餐厅(河北廊坊), 都会被这里拒绝。
    """
    hot = _hot_poi(query)
    if not hot:
        return None
    poi_name = _as_str(hot.get("name"))
    if not _name_related(query, poi_name):
        print(f"⚠️  [resolve_city] 「{original}」热度首位 POI 是「{poi_name}」, "
              f"与输入无关联, 按解析失败处理")
        return None

    province = _as_str(hot.get("province"))
    city = _as_str(hot.get("city"))
    district = _as_str(hot.get("district"))
    if not (city or province):
        return None

    # 具体地点的锚点 = 该 POI 自己的坐标(不是父城市中心), 距离过滤与
    # "距目的地多远"的提示都以它为原点
    location = hot.get("location") if isinstance(hot.get("location"), Location) else None
    if location is None:
        return None
    print(f"[resolve_city] 「{original}」不是城市, 按具体地点归一: "
          f"{poi_name}（{province}{city}{district}）, 检索半径 {_SCOPE_RADIUS_KM:.0f}km")
    return _canon(original, query, province=province, city=city, district=district,
                  adcode=_as_str(hot.get("adcode")), level="",
                  location=location, poi_type=_as_str(hot.get("type")), source="poi")


_HINT_SOURCE = "hint"


def _hint_fields(hint: Any) -> Dict[str, str]:
    """地点入参 -> 纯字符串字典 {province, city, district, adcode, detail}

    入参可能是 dict(来自 raw_request["structured"] 或 Send payload), 也可能是
    PlaceInput 模型(直调时)。统一成字典有两个好处: 调用方不必知道形态;
    缓存键稳定(ttl_cache._key 用 json.dumps(default=str), 直接传模型对象会
    退化成 repr, 加字段就会冷启动整个缓存)。
    """
    if hint is None:
        return {}
    if not isinstance(hint, dict):
        dump = getattr(hint, "model_dump", None)
        hint = dump() if callable(dump) else {}
    if not isinstance(hint, dict):
        return {}
    return {k: _as_str(hint.get(k))
            for k in ("province", "city", "district", "adcode", "detail")}


def _pick_admin_record(address: str, city: str = "") -> Optional[dict]:
    """按名字解析行政区记录(取与输入相关、行政级别最高的那条); 无则 None

    与 _canon_from_geocode 共用 _name_related 与级别排序, 但**不做热度首位
    消歧** —— 用户已经给了省市上下文, 不需要再猜一次。
    """
    records = _geocode_records(address, city) if city else _geocode_records(address)
    if not records and city:
        records = _geocode_records(address)
    related = [r for r in records if isinstance(r, dict)
               and _name_related(address, r.get("district"), r.get("city"), r.get("province"))]
    if not related:
        return None
    rank = {"省": 0, "市": 1, "区县": 2}
    return sorted(related, key=lambda r: rank.get(_as_str(r.get("level")), 3))[0]


def _canon_from_hint(original: str, hint: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """用户选了省市县 -> 归属以**选择**为准, 不再靠"热度首位"押注

    与另两条路的根本差别: 省市县与 adcode 来自用户的选择, 而不是从地名猜出来。
    用**名字**而不是前端带的 adcode 去解析, 是因为名字比区划码稳(撤县设区改码
    不改名)—— 前端静态表即使滞后, 归属依然正确。

    detail(如"老君山")只当**锚点坐标与检索词**。它的坐标仍走 _hot_poi +
    _name_related: 景区/古镇名在 geocode 里没有"正确的那一条"(见模块 docstring,
    geocode("莫干山") 回甘肃/广西同名住宅区), 拿 geocode 当锚点会锚到别人家。
    hint 的作用是**校验**这条发现路径的结论, 不是取消它。
    """
    province = hint.get("province", "")
    city_in = hint.get("city", "")
    district_in = hint.get("district", "")
    detail = hint.get("detail", "")
    if not (province or city_in or district_in):
        return None

    # ---- 1) 归属: 选中的区县 > 市 > 省; 用名字解析拿规范名与 adcode ----
    admin = district_in or city_in or province
    rec = _pick_admin_record(admin, city_in or province)
    if rec is None:
        rec = _pick_admin_record(admin)
    if rec is None:
        print(f"⚠️  [resolve_city] 用户选择的「{admin}」解析不出行政区记录, "
              f"按名字使用(无 adcode/坐标)")
    province = _as_str((rec or {}).get("province")) or province
    city_name = (_as_str((rec or {}).get("city")) or city_in
                 or _as_str((rec or {}).get("province")) or province)
    district = _as_str((rec or {}).get("district")) or district_in
    adcode = _as_str((rec or {}).get("adcode"))
    level = _as_str((rec or {}).get("level"))
    anchor = _record_location(rec) if rec else None

    # ---- 2) 口径由"有没有填 detail"决定 ----
    note = ""
    if detail:
        # 有具体地点 -> place 口径, 锚点=该地点。level 传空串让 _kind_of 判成
        # place(与 _canon_from_hot_poi 同一招), 不受行政区级别影响
        query, canon_level = detail, ""
        hot = _hot_poi(detail)
        h_code = _as_str((hot or {}).get("adcode"))
        if hot and _name_related(detail, hot.get("name")) and hot.get("location") is not None:
            h_prov = _as_str(hot.get("province"))
            h_city = _as_str(hot.get("city"))
            h_dist = _as_str(hot.get("district"))
            anchor = hot.get("location")
            # 冲突闸门: detail 的实际落点与所选不同城 -> 告警且**归属仍按所选**,
            # 宁可候选池为空(用户看得见、能改), 也不静默替换成另一个城市
            if h_code and adcode and _adcode_match(h_code, adcode) is False:
                note = (f"「{detail}」实际位于{h_prov}{h_city}{h_dist}, 与所选"
                        f"「{province}{city_name}{district}」不一致 —— 归属仍按所选, "
                        f"场地候选可能为空, 请核对目的地或「更具体的地点」")
                print(f"⚠️  [resolve_city] {note}")
            else:
                # 一致或无法判定: 用 detail 的归属细化(老君山 -> 栾川县)
                province = h_prov or province
                city_name = h_city or city_name
                district = h_dist or district
                adcode = adcode or h_code
        else:
            print(f"⚠️  [resolve_city] 更具体地点「{detail}」定位不到, "
                  f"退回「{admin}」的坐标作锚点")
    else:
        # 没填 detail -> 按行政级别走既定口径(市=整市, 区县=半径内)
        query = district_in or city_in or province
        canon_level = level or ("区县" if district_in else "市")

    canon = _canon(original, query, province=province, city=city_name, district=district,
                   adcode=adcode, level=canon_level, location=anchor,
                   source=_HINT_SOURCE)
    if note:
        canon["note"] = note      # 由 gather_info 读出来写进 steps_log
    print(f"[resolve_city] 「{original}」按用户选择归一: {canon['display']} "
          f"[{canon['kind']}/{canon['source']}], adcode={adcode or '-'}")
    return canon


@amap_cache
def resolve_city(name: Optional[str], hint: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """自由文本地名 -> 规范目的地信息(供检索口径/归属过滤/距离锚点使用)

    返回 ``{"name": "湖州市", "adcode": "", "province": "浙江省",
    "district": "德清县", "level": "", "location": Location, "kind": "place",
    "display": "莫干山（浙江省湖州市德清县）", "source": "poi", "query": "莫干山"}``;
    解析不出或与输入无关联时返回 None(调用方需按"目的地不可信"处理)。

    kind 决定检索口径:
      - "city"(杭州/临安之外的纯市名): 走 city + citylimit 全市检索;
      - "place"(莫干山/乌镇/千岛湖/朱家角/临安/西湖区): 走"{地名} {关键词}"
        + 同城市 + 半径过滤, 见 search_venues。

    为什么需要归一: 高德 place/text 的 city 参数只认规范城市名, 传 "杭州西湖区"
    这类串会被**静默忽略**(citylimit 失效)而返回全国结果; 传 "莫干山" 更是直接
    返回新疆/云南的店。两条来源(geocode / 热度首位 POI)各自覆盖一半地名。

    hint(前端选的省市县 + 可选 detail)给了就**不猜**: 归属按 hint 的**名字**
    解析(名字比 adcode 稳), detail 只当锚点坐标与检索词。hint 为 None 时
    行为与加此参数前逐字节相同(见 _canon_from_hint)。
    """
    hint_fields = _hint_fields(hint)
    if not name and not hint_fields:
        return None
    original = _as_str(name)
    hint_admin = (hint_fields.get("province") or hint_fields.get("city")
                  or hint_fields.get("district"))
    detail = hint_fields.get("detail")
    if detail and not hint_admin:
        # hint 只给了 detail(没选省市县): 以 detail 为准。调用方传进来的 name 可能
        # 只是**兜底的出发地** —— gather_info 在目的地为空时取 origin_name 当同城
        # 缺省, 拿它当地名会把"老君山"整个丢掉, 而那正是用户唯一表达出来的东西
        original = detail
    elif not original:
        # 前端只选了省市县、没拼出扁平串时也能用
        original = hint_admin or detail or ""
    if not original:
        return None

    # ---- 路径 0: 用户选了省市县 -> 归属以选择为准, 不猜(命中即返回, 跳过下面两条) ----
    if hint_fields:
        canon = _canon_from_hint(original, hint_fields)
        if canon is not None:
            return canon
        print(f"⚠️  [resolve_city] 地点入参不含省市县({hint_fields}), 回退到地名解析")

    query = original
    records = _geocode_records(query)
    if not records:
        cleaned = _strip_locality_words(query)
        if cleaned and cleaned != query:
            print(f"[resolve_city] 「{query}」无结果, 去修饰后重试「{cleaned}」")
            records = _geocode_records(cleaned)
            query = cleaned

    canon = _canon_from_geocode(query, records)
    if canon is not None:
        return canon

    # geocode 判不出来(整体错配或名字对不上): 改用热度首位 POI 反查归属。
    # 关键在**顺序**: 先 geocode(拿到规范城市名, 让 city 参数可用), 再 POI
    # (geocode 对景区名整体不可用); 只用 POI 会让 "杭州" 退化成按上城区半径搜。
    canon = _canon_from_hot_poi(original, query)
    if canon is not None:
        return canon

    print(f"⚠️  [resolve_city] 地名无法归一, 将按原样使用: {original!r}")
    return None


@amap_cache
def geocode(
    address: str,
    city: Optional[str] = None,
    near: Optional[Location] = None,
) -> Optional[Location]:
    """地理编码: 地址/城市名 -> 坐标(REST直连 v3/geocode/geo), 带双重消歧

    参数:
        address: 待解析地名(可能是用户随手写的"陆家嘴""西湖区")
        city:    上下文城市提示。给了就先在该城市内解析; 该城市没有此地名时
                 回落全国解析(跨城出发地如"苏州"依然可用)。
        near:    参照点(通常是目的地坐标)。全国解析出多个同名候选时, 取离
                 near 最近的一个 —— 这是"陆家嘴"不再落到云南的关键。

    同时打印可信度告警: level 为村庄/兴趣点等低可信级别, 或结果离 near
    过远时, 明确提示出发地可能解析错了, 而不是静默产出一份两千公里的报价。
    """
    addr = _as_str(address)
    if not addr:
        return None

    if city:
        records = _geocode_records(addr, city)
        if records:
            loc = _record_location(records[0])
            if loc is not None:
                return loc
        print(f"[geocode] 「{addr}」在「{city}」内无结果, 回落全国解析")

    records = _geocode_records(addr)
    if not records:
        print(f"⚠️  地理编码无结果: {address}")
        return None

    candidates = [(rec, _record_location(rec)) for rec in records]
    candidates = [(rec, loc) for rec, loc in candidates if loc is not None]
    if not candidates:
        print(f"⚠️  地理编码结果无坐标: {address}")
        return None

    chosen_rec: Optional[dict] = candidates[0][0]
    chosen_loc = candidates[0][1]
    chosen_where = ""

    if len(candidates) > 1:
        # 多个同名候选: 高德 geocode 按行政级别排序, 第 0 条经常是外省村庄。
        # 消歧优先级: ① place/text 全国热度首位(实测"陆家嘴"->上海浦东、
        # "中关村"->北京海淀、"新街口"->南京 全部选对) ② 离参照点 near 最近。
        top = _top_poi_location(addr)
        if top is not None:
            _, chosen_loc, chosen_where = top
            chosen_rec = None
            print(f"[geocode] 「{addr}」有 {len(candidates)} 个同名候选, "
                  f"按高德 POI 热度首位选定: {chosen_where}")
        elif near is not None:
            def _key(pair):
                d = _distance_km(pair[1], near)
                return d if d is not None else 1e9

            chosen_rec, chosen_loc = min(candidates, key=_key)
            others = [
                f"{_norm_admin(r.get('province'))}{_norm_admin(r.get('city'))}"
                + (f"({_distance_km(l, near):.0f}km)" if _distance_km(l, near) is not None else "")
                for r, l in candidates if r is not chosen_rec
            ]
            print(f"[geocode] 「{addr}」有 {len(candidates)} 个同名候选, 按离目的地最近选定: "
                  f"{_norm_admin(chosen_rec.get('province'))}{_norm_admin(chosen_rec.get('city'))}; "
                  f"其它候选: {', '.join(others)}")

    if chosen_rec is None:
        # 走的是 POI 热度首位分支: 该点位来自高德热度排序, 可信度高于村庄级 geocode
        dist = _distance_km(chosen_loc, near)
        if dist is not None and dist > _IMPLAUSIBLE_ORIGIN_KM * 3:
            print(f"⚠️  [geocode] 「{addr}」解析到 {chosen_where}, 距参照点 {dist:.0f}km, "
                  f"请确认出发地是否写对")
        return chosen_loc

    level = _as_str(chosen_rec.get("level"))
    where = (f"{_as_str(chosen_rec.get('province'))}{_as_str(chosen_rec.get('city'))}"
             f"{_as_str(chosen_rec.get('district'))}")
    if level in _UNRELIABLE_LEVELS:
        print(f"⚠️  [geocode] 「{addr}」解析到低可信级别({level or '未知'}): {where} "
              f"-> 该地名可能有歧义, 建议提示用户写明城市")
    dist = _distance_km(chosen_loc, near)
    if dist is not None and dist > _IMPLAUSIBLE_ORIGIN_KM and level in _UNRELIABLE_LEVELS:
        print(f"⚠️  [geocode] 「{addr}」距参照点 {dist:.0f}km 且级别不可信({level or '未知'}), "
              f"解析结果可能错误: {where}")
    return chosen_loc


@amap_cache
def _hot_poi(name: str) -> Optional[Dict[str, Any]]:
    """全国 place/text 搜该地名, 取热度首位, 返回**结构化**归属字段; 无结果返回 None

    返回 ``{"name", "location": Location, "province", "city", "district",
    "adcode", "type"}``。高德对"陆家嘴/中关村/新街口/莫干山/乌镇"这类通名与
    景区名, 首位总是知名度最高的那个(上海陆家嘴/北京中关村/南京新街口/
    湖州莫干山/嘉兴乌镇)。

    必须返回**结构化的省市县**而不是一个拼接串: 下游要用 city 当 place/text 的
    city 参数、用 district/city 做归属过滤、用坐标当距离锚点 —— 拼成一个字符串
    就只能打印, 用不了(_top_poi_location 那个旧形态正是"救命分支形同虚设"的原因)。
    """
    data = _rest_get("place/text", {"keywords": name, "offset": 1, "extensions": "all"})
    pois = [p for p in (data.get("pois") or []) if isinstance(p, dict)]
    if not pois:
        return None
    loc = _parse_location(pois[0].get("location"))
    if loc is None:
        return None
    return {
        "name": _as_str(pois[0].get("name")) or name,
        "location": loc,
        "province": _as_str(pois[0].get("pname")),
        "city": _as_str(pois[0].get("cityname")),
        "district": _as_str(pois[0].get("adname")),
        "adcode": _as_str(pois[0].get("adcode")),
        "type": _as_str(pois[0].get("type")),
    }


@amap_cache
def _top_poi_location(name: str):
    """``(地点名, Location, "省市县")`` 三元组 —— geocode 消歧打印用; 无结果返回 None

    只要一个能打印的"在哪儿", 用 _hot_poi 包一层即可; 需要结构化字段的新代码
    请直接用 _hot_poi。
    """
    hot = _hot_poi(name)
    if not hot:
        return None
    return (hot["name"], hot["location"],
            f"{hot['province']}{hot['city']}{hot['district']}")


@amap_cache
def get_poi_detail(poi_id: str) -> Dict[str, Any]:
    """POI 详情(v3/place/detail, extensions=all 含图片/营业时间等)

    **查不到这条 POI** 返回 {}(正常业务结果, 路由层转 404);
    调用本身失败则抛分类异常, 不再用 {} 把故障也伪装成"没这条数据"。
    """
    if not poi_id:
        return {}
    data = _rest_get("place/detail", {"id": poi_id, "extensions": "all"})
    pois = data.get("pois") or []
    return pois[0] if pois and isinstance(pois[0], dict) else {}


# ============ 城市归属过滤 ============

def _adcode_match(poi_adcode: str, city_adcode: str) -> Optional[bool]:
    """按行政区代码判断两个点位是否同城; 信息不足返回 None

    关键坑: 直辖市的 geocode 返回的是省级码(上海市=310000), 而 POI 带的是
    区级码(浦东新区=310115), 直接比前 4 位(3101 vs 3100)会把上海全市候选
    误杀。因此:
    - 目标码以 "0000" 结尾(省/直辖市/州级) -> 只比前 2 位(省级前缀);
    - 否则比前 4 位(地级市前缀, 如 杭州市 330100 vs 淳安县 330127)。
    """
    if len(poi_adcode) < 2 or len(city_adcode) < 2:
        return None
    if city_adcode.endswith("0000"):
        return poi_adcode[:2] == city_adcode[:2]
    if len(poi_adcode) >= 4 and len(city_adcode) >= 4:
        return poi_adcode[:4] == city_adcode[:4]
    return poi_adcode[:2] == city_adcode[:2]


def _poi_in_city(item: dict, canon: Optional[Dict[str, Any]]) -> bool:
    """判断 POI 是否真属于目标城市(防"城市参数非法 -> 全国混排"泄漏)

    优先用 adcode 比较(见 _adcode_match), 退化为 pname/cityname 归一比较。
    canon 为空(城市无法归一)时不做判断, 由调用方决定是否放行。
    """
    if not canon:
        return True
    poi_adcode = _as_str(item.get("adcode"))
    city_adcode = _as_str(canon.get("adcode"))
    verdict = _adcode_match(poi_adcode, city_adcode)
    if verdict is not None:
        return verdict

    city_key = _norm_admin(canon.get("name"))
    if not city_key:
        return True
    if _norm_admin(item.get("cityname")) == city_key:
        return True
    # 直辖市/省直辖场景: pname 与 cityname 同值
    return _norm_admin(item.get("pname")) == city_key


def _drop_non_venues(items: List[dict]) -> List[dict]:
    """剔掉"坐标对但不可能当场地"的 POI(站牌/道路/收费设施等), 并打印被剔的"""
    kept: List[dict] = []
    dropped: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        poi_type = _as_str(item.get("type"))
        head = poi_type.split(";")[0]
        if head in _NON_VENUE_TYPE_PREFIXES \
                or any(hint in poi_type for hint in _NON_VENUE_TYPE_HINTS):
            dropped.append(f"{item.get('name')}({poi_type})")
            continue
        kept.append(item)
    if dropped:
        print(f"[search_venues] 剔除 {len(dropped)} 条非场地类 POI(站牌/道路/收费设施等): "
              f"{', '.join(dropped[:3])}{'...' if len(dropped) > 3 else ''}")
    return kept


def _parse_poi(item: dict, default_type: str, default_address: str = "") -> Optional[POIInfo]:
    """高德 POI 原始 dict -> POIInfo(无坐标的候选对规划无用, 直接丢弃)"""
    loc = _parse_location(item.get("location"))
    if loc is None:
        return None
    # biz_ext 形如 {"rating": "4.5", "cost": "80"}, 可能缺字段/为空串/"[]"
    biz = item.get("biz_ext") or {}
    rating = biz.get("rating")
    cost = biz.get("cost")
    if not isinstance(rating, str) or rating in ("", "[]"):
        rating = None
    if not isinstance(cost, str) or cost in ("", "[]"):
        cost = None
    return POIInfo(
        id=str(item.get("id", "")),
        name=item.get("name", ""),
        type=item.get("type", "") or default_type,
        address=_as_str(item.get("address")) or default_address,
        location=loc,
        tel=_as_str(item.get("tel")) or None,
        rating=rating,
        cost=cost,
    )


def _split_scope(items: List[dict], canon: Dict[str, Any],
                 anchor: Optional[Location]) -> Tuple[List[tuple], List[tuple], List[dict]]:
    """具体地点口径三分: 圈内 / 同城但超半径 / 城市都不对

    只按半径滤不行 —— 没有锚点(坐标缺失)时半径无从算起, 只能靠同城判断兜底;
    只按同城滤也不行 —— 一个地级市可能有 2 万平方公里(临安区的候选和滨江区的
    候选同城, 相隔 60km)。两道一起才既挡得住全国混排, 又圈得住"就在这儿附近"。
    """
    kept: List[tuple] = []
    far: List[tuple] = []
    wrong: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not _poi_in_city(item, canon):
            wrong.append(item)
            continue
        dist = _distance_km(_parse_location(item.get("location")), anchor) \
            if anchor is not None else None
        (kept if dist is None or dist <= _SCOPE_RADIUS_KM else far).append((item, dist))
    return kept, far, wrong


def _search_scope(canon: Dict[str, Any], keywords: str) -> List[POIInfo]:
    """具体地点口径(景区/古镇/离岛/区县)的检索与过滤

    - keywords 用 "{地名} {关键词}" 而不是把地名当 city 传参;
    - **不传 city/citylimit**: 实测 "临安 团建场地" + city=杭州市 + citylimit 返回
      0 条, 不传 city 返回 15 条(全是临安区的真候选)。高德的 citylimit 与
      关键词里的地名会互相打架, 索性不用 —— 混排风险由同城+半径两道过滤兜住;
    - "{地名} {关键词}" 召回不足下限时补一次"{地名}"宽搜: 实测 "莫干山 团建场地"
      只回 1 条(修完照样报"候选不足"), 而 "莫干山" 回 15 条且全在 7km 内;
    - 半径内仍不足下限时放宽为"同城最近的若干条"并显式打印 —— 宁可给一条标注了
      通勤代价的真候选, 也不给空池让下游写"地点待定"。
    """
    dest = _as_str(canon.get("query")) or _as_str(canon.get("name"))
    anchor = canon.get("location")
    if not isinstance(anchor, Location):
        anchor = None

    def _fetch(kw: str) -> List[dict]:
        data = _rest_get("place/text", {
            "keywords": kw, "offset": 15, "extensions": "all"})
        return [p for p in (data.get("pois") or []) if isinstance(p, dict)]

    raw = _drop_non_venues(_fetch(f"{dest} {keywords}".strip()))
    kept, far, wrong = _split_scope(raw, canon, anchor)
    # 放宽闸门看的是**同城**条数, 不是原始条数: 高德对"{区县} {关键词}"经常返回
    # **别的城市**的场地(实测 "栾川县 团建场地" 回 4 条全是北京的团建场馆), 原始
    # 条数 4 >= 下限会骗过闸门 -> 永远不补宽搜 -> 自家 0 条候选且给不出原因。
    # 实测同城为 0 时补一次裸名宽搜能把栾川县从 0 条救回 3 条。
    if len(kept) + len(far) < _SCOPE_KEYWORD_MIN:
        wide = _drop_non_venues(_fetch(dest))
        seen = {_as_str(p.get("id")) for p in raw}
        added = [p for p in wide if _as_str(p.get("id")) not in seen]
        print(f"[search_venues] 「{dest} {keywords}」同城仅 {len(kept) + len(far)} 条"
              f"(原始 {len(raw)}, 非同城 {len(wrong)}), 补一次「{dest}」宽搜"
              f"(新增 {len(added)} 条)")
        if added:
            raw += added
            kept, far, wrong = _split_scope(raw, canon, anchor)

    if len(kept) < _SCOPE_KEYWORD_MIN and far:
        far.sort(key=lambda pair: pair[1])
        add = far[: _SCOPE_KEYWORD_MIN - len(kept)]
        print(f"⚠️  [search_venues] 「{dest}」{_SCOPE_RADIUS_KM:.0f}km 内仅 {len(kept)} 条候选, "
              f"放宽取同城最近的 {len(add)} 条(通勤代价交给提案层说明): "
              + ", ".join(f"{p.get('name')}({d:.0f}km)" for p, d in add))
        kept += add

    pois: List[POIInfo] = []
    for item, _dist in kept:
        poi = _parse_poi(item, "场地", default_address=_as_str(canon.get("name")))
        if poi is not None:
            pois.append(poi)   # 无坐标的候选对规划无用

    print(f"[search_venues] 「{dest}」按具体地点口径检索"
          f"({_as_str(canon.get('province'))}{_as_str(canon.get('name'))}"
          f"{_as_str(canon.get('district'))}, 半径{_SCOPE_RADIUS_KM:.0f}km): "
          f"入池 {len(pois)} 条" + (f", 超半径丢弃 {len(far)} 条" if far else "")
          + (f", 非同城丢弃 {len(wrong)} 条" if wrong else ""))
    if not pois:
        print(f"⚠️  [search_venues] 「{dest}」无候选入池 —— 交由上层报『候选不足』"
              f"而不是给错数据")
    return pois


@amap_cache
def search_venues(destination: str, keywords: str = "团建场地",
                  hint: Optional[Any] = None) -> List[POIInfo]:
    """搜索场地候选(REST直连, 返回自带坐标)

    目的地分两种口径, 走两条路(旧实现只有"城市"一种, 于是景区/古镇/区县目的地
    必然空池 —— "莫干山/乌镇/千岛湖/朱家角/古北水镇"五个实测全部 0 候选):

    - **城市**(杭州/上海): 归一到规范城市名 -> city + citylimit 全市检索
      -> 按 adcode/cityname 归属过滤。这一步同时修掉"传『杭州西湖区』被静默
      忽略 -> 返回新疆/丽江/昆明"的全国混排;
    - **具体地点**(莫干山/乌镇/临安/西湖区): 交 _search_scope 按"{地名} {关键词}"
      + 同城 + 半径检索。

    归一失败时退化为"按原字符串比较 pname/cityname", 匹配不上就丢掉 —— 非法城市名
    会让高德静默返回全国结果(实测 city="不存在的城市名" 返回内蒙古的店铺), 宁可
    为空也不给错数据。extensions=all 以附带 biz_ext(评分/人均消费)。

    hint 必须与 gather_info 给 resolve_city 的**同一个**(前端选的省市县 + detail):
    只加给 resolve_city 会在检索这一步重新开始猜, 两条路的 canon 分叉 —— 正是
    "交通按上海算、场地按莫干山排"那个 bug。同一 hint -> 缓存命中同一对象。
    """
    raw_dest = _as_str(destination)
    # hint 为空时走旧调用形态: 缓存键不含 hint 关键字, 老缓存继续命中
    canon = resolve_city(raw_dest, hint) if hint else resolve_city(raw_dest)
    if canon and canon.get("kind") == "place":
        return _search_scope(canon, keywords)

    query_city = (canon or {}).get("name") or raw_dest
    if canon and _norm_admin(query_city) != _norm_admin(raw_dest):
        print(f"[search_venues] 城市归一: 「{raw_dest}」-> 「{query_city}」")
    if canon is None:
        # 城市名解析不出来时**不能不过滤**: 非法城市名会让 place/text 静默返回
        # 全国结果(实测 city="不存在的城市名" 返回内蒙古的店铺)。这里退化为
        # "按原字符串比较 pname/cityname", 匹配不上就丢掉 -> 宁可为空也不给错数据。
        canon = {"name": raw_dest, "adcode": "", "province": "", "level": "",
                 "kind": "city", "query": raw_dest}

    data = _rest_get("place/text", {
        "keywords": keywords,
        "city": query_city,
        "citylimit": "true",
        "offset": 15,
        "extensions": "all",
    })
    raw_pois = _drop_non_venues(data.get("pois") or [])
    pois: List[POIInfo] = []
    dropped: List[str] = []
    for item in raw_pois:
        if not isinstance(item, dict):
            continue
        if not _poi_in_city(item, canon):
            dropped.append(
                f"{item.get('name')}({_as_str(item.get('pname'))}{_as_str(item.get('cityname'))})"
            )
            continue
        poi = _parse_poi(item, "场地", default_address=query_city)
        if poi is None:
            continue  # 无坐标的候选对规划无用
        pois.append(poi)

    if dropped:
        print(f"⚠️  [search_venues] 「{raw_dest}」结果里有 {len(dropped)} 条不属于目标城市, 已丢弃: "
              f"{', '.join(dropped[:3])}{'...' if len(dropped) > 3 else ''}")
    if raw_pois and not pois:
        print(f"⚠️  [search_venues] 「{raw_dest}」返回 {len(raw_pois)} 条但全部不属于目标城市 "
              f"(城市名可能不规范), 候选池为空 —— 交由上层报『候选不足』而不是给错数据")
    return pois


@amap_cache_ttl(_AMAP_TTL_WEATHER)
def get_weather(city: str) -> List[WeatherInfo]:
    """查询天气预报(REST直连 v3/weather/weatherInfo, extensions=all 返回未来3-4天预报)

    注: 原走 MCP maps_weather, 但 MCP client 依赖 fastmcp>=2.0 且 uvx 子进程
    启动开销大; weather REST 与 POI 搜索同为只读查询, 统一走 _rest_get。
    城市同样先归一, 否则"上海周边"之类写法查不到。
    """
    canon = resolve_city(city)
    query_city = (canon or {}).get("name") or _as_str(city)
    data = _rest_get("weather/weatherInfo", {"city": query_city, "extensions": "all"})
    forecasts = data.get("forecasts") or []
    if not forecasts:
        print(f"⚠️  天气查询无结果: {city} ({data.get('info', '')})")
        return []

    # casts 在 forecasts[0] 里, 但做两层防御
    casts = forecasts[0].get("casts", []) if isinstance(forecasts[0], dict) else []
    if not casts:
        casts = forecasts

    result = []
    for c in casts:
        try:
            result.append(WeatherInfo(
                date=c.get("date", ""),
                day_weather=c.get("dayweather", ""),
                night_weather=c.get("nightweather", ""),
                day_temp=c.get("daytemp", 0),
                night_temp=c.get("nighttemp", 0),
                wind_direction=c.get("daywind", "") or c.get("nightwind", ""),
                wind_power=c.get("daypower", "") or c.get("nightpower", ""),
            ))
        except Exception:
            continue
    return result


@amap_cache
def search_nearby(
    location: Location, radius: int = 3000, keywords: str = "餐厅"
) -> List[POIInfo]:
    """周边搜索: 指定坐标附近 radius 米内的 POI(REST直连 v3/place/around)

    供 food 提案的受控搜索工具使用: 位置/半径由系统锁定, LLM 只能传关键词。
    周边搜天然被坐标限定, 不需要城市归属过滤。
    """
    data = _rest_get("place/around", {
        "location": f"{location.longitude},{location.latitude}",
        "radius": radius,
        "keywords": keywords,
        "sortrule": "distance",
        "offset": 5,
        "extensions": "all",
    })
    pois = []
    for item in data.get("pois", []) or []:
        if not isinstance(item, dict):
            continue
        poi = _parse_poi(item, "餐饮")
        if poi is not None:
            pois.append(poi)
    return pois


@amap_cache_ttl(_AMAP_TTL_ROUTE)
def get_route_info(
    origin: Location,
    destination: Location,
    route_type: str = "driving",
    origin_city: Optional[str] = None,
    destination_city: Optional[str] = None,
) -> Optional[RouteInfo]:
    """路线规划: 出发地->目的地 距离/耗时(REST直连 v3/direction/*)

    driving/walking 结构一致(route.paths[0]); transit 走 integrated 接口,
    结果在 route.transits[0], 且必须带城市参数。未识别的类型回退 driving。
    """
    if route_type not in ("driving", "walking", "transit"):
        route_type = "driving"

    origin_coord = f"{origin.longitude},{origin.latitude}"
    destination_coord = f"{destination.longitude},{destination.latitude}"

    if route_type == "transit":
        params: Dict[str, Any] = {
            "origin": origin_coord,
            "destination": destination_coord,
            "extensions": "base",
        }
        # 公交/地铁方案必须给城市(高德要求 city/cityd)
        if origin_city:
            params["city"] = _as_str(origin_city)
        if destination_city:
            params["cityd"] = _as_str(destination_city)
        data = _rest_get("direction/transit/integrated", params)
        transits = ((data.get("route") or {}).get("transits")) or []
        if not transits:
            print("⚠️  公共交通路线无结果(可能缺城市参数或两地无直达)")
            return None
        segment = transits[0]
    else:
        data = _rest_get(f"direction/{route_type}", {
            "origin": origin_coord,
            "destination": destination_coord,
            "extensions": "base",
        })
        paths = ((data.get("route") or {}).get("paths")) or []
        if not paths:
            print(f"⚠️  路线规划无结果: {route_type}")
            return None
        segment = paths[0]

    try:
        distance = float(segment.get("distance", 0))          # 米
        duration = int(float(segment.get("duration", 0)))     # 秒
    except (TypeError, ValueError):
        return None
    return RouteInfo(
        distance=distance,
        duration=duration,
        route_type=route_type,
        description=f"{route_type} 约 {distance / 1000:.1f} 公里 / {duration // 60} 分钟",
    )
