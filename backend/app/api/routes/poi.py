"""POI相关API路由

失败处置与 map.py 一致: **只捕分类过的失败**(见 app/api/errors.py),
其余异常放行给 FastAPI 记栈 —— 程序缺陷不该被压成一句 500 文案。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from ...services.amap_service import get_amap_service
from ...services.unsplash_service import get_unsplash_service
from ...graph.errors import AppError
from ..errors import to_http_error

router = APIRouter(prefix="/poi", tags=["POI"])


class POIDetailResponse(BaseModel):
    """POI详情响应"""
    success: bool
    message: str
    data: Optional[dict] = None


@router.get(
    "/detail/{poi_id}",
    response_model=POIDetailResponse,
    summary="获取POI详情",
    description="根据POI ID获取详细信息,包括图片"
)
async def get_poi_detail(poi_id: str):
    """
    获取POI详情
    
    Args:
        poi_id: POI ID
        
    Returns:
        POI详情响应; 查不到返回 404(不再把错误文本塞进 data 当成功返回)
    """
    try:
        amap_service = get_amap_service()

        # 调用高德 POI 详情 REST 接口(v3/place/detail)
        result = amap_service.get_poi_detail(poi_id)

        if not result:
            raise HTTPException(status_code=404, detail=f"未找到 POI 详情: {poi_id}")

        return POIDetailResponse(
            success=True,
            message="获取POI详情成功",
            data=result
        )

    except HTTPException:
        raise                      # 查不到这条 POI 的 404, 是业务结果不是故障
    except AppError as e:
        raise to_http_error(e, "获取POI详情")
    # 其余异常不捕: 交 FastAPI 记栈, 别把程序 bug 压成一句 500 文案


@router.get(
    "/search",
    summary="搜索POI",
    description="根据关键词搜索POI"
)
async def search_poi(keywords: str, city: str = "北京"):
    """
    搜索POI

    Args:
        keywords: 搜索关键词
        city: 城市名称

    Returns:
        搜索结果
    """
    try:
        amap_service = get_amap_service()
        result = amap_service.search_poi(keywords, city)

        return {
            "success": True,
            "message": (f"搜索成功, 命中{len(result)}条" if result
                        else f"搜索无结果: 城市『{city}』或关键词『{keywords}』未匹配到候选"),
            "data": result
        }

    except AppError as e:
        raise to_http_error(e, "搜索POI")
    # 其余异常不捕: 交 FastAPI 记栈


@router.get(
    "/photo",
    summary="获取景点图片",
    description="根据景点名称从Unsplash获取图片"
)
async def get_attraction_photo(name: str):
    """
    获取景点图片

    Args:
        name: 景点名称

    Returns:
        图片URL
    """
    try:
        unsplash_service = get_unsplash_service()

        # 搜索景点图片
        photo_url = unsplash_service.get_photo_url(f"{name} China landmark")

        if not photo_url:
            # 如果没找到,尝试只用景点名称搜索
            photo_url = unsplash_service.get_photo_url(name)

        return {
            "success": True,
            "message": "获取图片成功",
            "data": {
                "name": name,
                "photo_url": photo_url
            }
        }

    # 图片是装饰性数据: "搜不到就返回 None" 这个降级决定已经由 unsplash_service
    # 自己做了, 路由层不必再兜一层把人家的异常也吞掉 —— 真出异常就该有栈可看
    except AppError as e:
        raise to_http_error(e, "获取景点图片")

