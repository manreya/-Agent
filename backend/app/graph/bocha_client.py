"""博查 AI 搜索调用与结果解析(Web Search API)

吃食口碑/住宿评价/游玩攻略的第二个数据源: 高德只有 POI 名称+人均,
没有口碑/特色/攻略类内容, 这里用博查 Web Search 补齐。

失败语义(本轮统一, 见 app/graph/errors.py):
  - 未配置 BOCHA_API_KEY -> 抛 CapabilityDisabled。**可选能力未开, 不是故障**,
    由调用层的 call_external 映射成 CallStatus.DISABLED: 不重试、不报降级,
    但对外明确"这一维没有数据"。
  - 网络/超时/5xx/非 JSON -> **原样抛出**, 由 call_external 分类并退避重试。
    旧实现这里 `except Exception: return []`, 把"接口挂了"和"真没搜到"
    抹成同一个空值, 调用方分不出来。
  - 平台业务错误码        -> 抛 TransientError(实测以配额/限流为主); 有次数与
    总时长上限兜底, 即便实为永久错误也会以降级的形式显式暴露, 不会静默。
  - 确实没有结果          -> 返回 [], 这是**正常业务结果**, 不该重试。

接口: POST https://api.bochaai.com/v1/web-search (Bearer 鉴权)
计费: 约 0.036 元/次, 新用户送 1000 次; 结果同参数进程级缓存。
"""

import json
from typing import Any, Dict, List

from ..config import get_settings
from .errors import DEFAULT_HTTP_TIMEOUT, CapabilityDisabled, TransientError
from .ttl_cache import ttl_cache

# 博查 WebSearch 缓存 TTL: 攻略/口碑内容稳定, 且这是**按次计费**的调用
# (约 0.036 元/次), 长 TTL 直接省调用费; 同参数 12h 内不重复付费查询
_BOCHA_TTL = 12 * 3600


@ttl_cache("bocha_web_search", _BOCHA_TTL, log_prefix="bocha_cache")
def web_search(query: str, count: int = 5, freshness: str = "noLimit") -> List[dict]:
    """博查 Web 搜索: 返回精简结果列表 [{title, url, snippet, site_name}]

    失败抛分类异常, 只有"确实没搜到"才返回 [] —— 详见模块 docstring。
    freshness: noLimit/oneDay/oneWeek/oneMonth/oneYear
    """
    settings = get_settings()
    if not settings.bocha_api_key:
        raise CapabilityDisabled("BOCHA_API_KEY 未配置, Web搜索(攻略/口碑)不可用")

    import httpx

    # 网络/HTTP/JSON 解析异常一律不捕 -> 交给 call_external 分类(它才知道该不该重试)
    resp = httpx.post(
        "https://api.bochaai.com/v1/web-search",
        headers={
            "Authorization": f"Bearer {settings.bocha_api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query, "count": count, "freshness": freshness},
        timeout=DEFAULT_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    code = data.get("code")
    if code not in (None, 200):
        log_id = str(data.get("log_id", ""))[:40]
        raise TransientError(f"[bocha] 平台返回异常 code={code} log_id={log_id}",
                             code="bocha_business")

    pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    results = []
    for p in pages[:count]:
        results.append({
            "title": p.get("name") or "",
            "url": p.get("url") or "",
            "snippet": (p.get("summary") or p.get("snippet") or "")[:300],
            "site_name": p.get("siteName") or "",
        })
    print(f"[bocha] 「{query}」命中{len(results)}条")
    return results