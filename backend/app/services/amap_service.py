"""高德地图服务封装(通用地图接口层)

历史问题: 这里原先是 MCPTool(uvx amap-mcp-server) 的薄封装, 且
search_poi / get_weather / plan_route / geocode 四个方法都是 ``# TODO`` 桩:
无论 MCP 是否可用, 它们**永远**返回 [] / {} / None, 于是
- GET  /api/map/poi      -> 200 但 data 恒为空
- GET  /api/map/weather  -> 200 但 data 恒为空
- POST /api/map/route    -> AmapService.plan_route 返回 {}, 组 RouteResponse 时
                            触发 pydantic 校验错误 -> 每次 500
另外 server_command 里写死了 "uvx", 而 PATH 上通常没有 uvx(实际装在
~/.local/bin), 导致连"调用 MCP"这条路径也走不通。

现在统一改走 app.graph.amap_client 的 REST 直连(与策划主流程同一条数据链路):
同一套地名归一、消歧与归属过滤, 返回真实数据或明确的失败(None/空列表),
不再出现"200 + 空数据 + 成功文案"。
"""

from typing import Any, Dict, List, Optional

from ..graph import amap_client
from ..models.schemas import Location, POIInfo, RouteInfo, WeatherInfo

_amap_service = None


class AmapService:
    """高德地图服务封装类(REST 直连)"""

    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        """搜索 POI(场地/餐饮等): 返回自带坐标的真实候选

        citylimit 由 amap_client 内部固定为 true 并叠加城市归属过滤,
        因此这里保留参数只为接口兼容。
        """
        return amap_client.search_venues(city, keywords)

    def get_weather(self, city: str) -> List[WeatherInfo]:
        """查询城市天气预报(高德仅提供未来 3-4 天)"""
        return amap_client.get_weather(city)

    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "driving",
    ) -> Optional[RouteInfo]:
        """地址到地址的路线规划: 先地理编码再调 direction

        返回 None 表示确实算不出路线(地址解析失败或无可用路线),
        由路由层转成明确的业务失败, 不再用 {} 触发 500。
        """
        origin_city = origin_city or destination_city
        destination_city = destination_city or origin_city

        origin_loc = amap_client.geocode(origin_address, city=origin_city)
        if origin_loc is None:
            print(f"⚠️  路线规划: 起点地址解析失败: {origin_address}")
            return None
        destination_loc = amap_client.geocode(destination_address, city=destination_city)
        if destination_loc is None:
            print(f"⚠️  路线规划: 终点地址解析失败: {destination_address}")
            return None

        return amap_client.get_route_info(
            origin_loc, destination_loc, route_type,
            origin_city=origin_city, destination_city=destination_city,
        )

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        """地理编码(地址转坐标), 带城市上下文消歧"""
        return amap_client.geocode(address, city=city)

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """POI 详情(含图片/营业时间等); 查不到返回 {}, 不再把错误文本当数据返回"""
        return amap_client.get_poi_detail(poi_id)


def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)"""
    global _amap_service

    if _amap_service is None:
        _amap_service = AmapService()

    return _amap_service
