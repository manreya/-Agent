"""地图服务API路由

数据源: app.graph.amap_client 高德 REST 直连(与策划主流程同一条链路),
自带地名归一化与城市归属过滤; 结果为空时如实回报, 不再"200 + 空数据 + 成功文案"。

失败处置: **只捕分类过的失败**(上游抖动 -> 503 / 能力未启用 -> 501 / 坏响应 -> 502),
其余异常放行给 FastAPI 记栈 —— 详见 app/api/errors.py。
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from ...models.schemas import (
    POISearchRequest,
    POISearchResponse,
    RouteRequest,
    RouteResponse,
    WeatherResponse
)
from ...services.amap_service import get_amap_service
from ...graph.errors import AppError
from ..errors import to_http_error

router = APIRouter(prefix="/map", tags=["地图服务"])


@router.get(
    "/poi",
    response_model=POISearchResponse,
    summary="搜索POI",
    description="根据关键词搜索POI(兴趣点); 城市名会自动归一化(如『杭州西湖区』-> 杭州市)"
)
async def search_poi(
    keywords: str = Query(..., description="搜索关键词", examples=["团建场地"]),
    city: str = Query(..., description="城市", examples=["杭州"]),
    citylimit: bool = Query(True, description="是否限制在城市范围内")
):
    """
    搜索POI

    Args:
        keywords: 搜索关键词
        city: 城市
        citylimit: 是否限制在城市范围内

    Returns:
        POI搜索结果(空结果时 success=True 但 message 说明原因, 便于排查)
    """
    try:
        # 获取服务实例
        service = get_amap_service()

        # 搜索POI
        pois = service.search_poi(keywords, city, citylimit)

        return POISearchResponse(
            success=True,
            message=(f"POI搜索成功, 命中{len(pois)}条" if pois
                     else f"POI搜索无结果: 城市『{city}』或关键词『{keywords}』未匹配到自带坐标的候选"),
            data=pois
        )

    except AppError as e:
        raise to_http_error(e, "POI搜索")
    # 其余异常不捕: 程序缺陷要能在日志里看到栈, 而不是被压成一句 500 文案


@router.get(
    "/weather",
    response_model=WeatherResponse,
    summary="查询天气",
    description="查询指定城市的天气信息(高德仅提供未来3-4天预报)"
)
async def get_weather(
    city: str = Query(..., description="城市名称", examples=["杭州"])
):
    """
    查询天气

    Args:
        city: 城市名称

    Returns:
        天气信息(高德预报范围仅未来3-4天, 空结果时 message 会说明)
    """
    try:
        # 获取服务实例
        service = get_amap_service()

        # 查询天气
        weather_info = service.get_weather(city)

        return WeatherResponse(
            success=True,
            message=(f"天气查询成功, 预报{len(weather_info)}天" if weather_info
                     else f"天气查询无结果: 城市『{city}』未匹配到预报"),
            data=weather_info
        )

    except AppError as e:
        raise to_http_error(e, "天气查询")
    # 其余异常不捕: 同上, 交 FastAPI 记栈


@router.post(
    "/route",
    response_model=RouteResponse,
    summary="规划路线",
    description="规划两点之间的路线"
)
async def plan_route(request: RouteRequest):
    """
    规划路线
    
    Args:
        request: 路线规划请求
        
    Returns:
        路线信息
    """
    try:
        # 获取服务实例
        service = get_amap_service()
        
        # 规划路线
        route_info = service.plan_route(
            origin_address=request.origin_address,
            destination_address=request.destination_address,
            origin_city=request.origin_city,
            destination_city=request.destination_city,
            route_type=request.route_type
        )

        # 算不出路线时明确回报失败: 原来把 {} 塞进 response_model 会直接 500
        if route_info is None:
            return RouteResponse(
                success=False,
                message=(f"路线规划失败: 无法解析「{request.origin_address}」->"
                         f"「{request.destination_address}」, 请确认起终点地址与城市"),
                data=None
            )

        return RouteResponse(
            success=True,
            message="路线规划成功",
            data=route_info
        )
        
    except AppError as e:
        raise to_http_error(e, "路线规划")
    # 其余异常不捕: 同上, 交 FastAPI 记栈


@router.get(
    "/health",
    summary="健康检查",
    description="检查地图服务是否正常"
)
async def health_check():
    """健康检查: 报告数据源(高德 REST 直连)是否可用"""
    try:
        service = get_amap_service()
        # 用一次真实只读查询探活(进程级缓存, 重复调用无额外外网开销)
        probe = service.geocode("上海")

        return {
            "status": "healthy" if probe else "degraded",
            "service": "map-service",
            "data_source": "amap-rest-v3",
            "probe_geocode": "ok" if probe else "failed",
        }
    except AppError as e:
        # 探活端点问的就是"能不能用", 因此分类过的失败一律回 503(不再是一律 500),
        # 且只回 user_message —— 探活响应不往外吐上游细节
        raise HTTPException(status_code=503,
                            detail=f"服务不可用({e.code}): {e.user_message}")
    # 认不出的异常放行: 探活代码自己有 bug, 不该被报成"数据源不健康"

