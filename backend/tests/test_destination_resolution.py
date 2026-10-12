"""离线单元测试: 目的地归一 与 非城市目的地的场地检索

覆盖的是本项目**最容易静默出错**的一层: 用户写的"莫干山"不是城市, 高德却不会
报错 —— place/text 的 city 参数只认规范城市名, 传景区名会被**直接忽略**而返回
全国混排(实测"杭州团建"的候选池里混进新疆伊犁/云南丽江的露营地, 归属过滤一
挡就变成 0 条候选)。所以这里的每条断言都对应一个曾经真实发生过的错误行为。

设计原则:
- **不触网**: mock 掉 amap_client._rest_get, 用的都是实测抓下来的高德响应片段
- **不依赖缓存**: resolve_city / search_venues 带 24h 缓存装饰器, 每个用例先清空
- 无第三方依赖: 标准库 unittest(pytest 未安装)

运行:
    cd backend && py -3.13 -m unittest discover -s tests -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 被测代码会给用户打"⚠️ 高德把地名模糊匹配到别处了"这类告警, Windows 控制台是
# GBK, 直接 print 会 UnicodeEncodeError —— 那是终端编码问题, 不是被测行为。
# 把测试进程的 stdout 换成 utf-8(replace) 才能看到真正的断言结果。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):     # 已被重定向成非文本流时忽略
        pass

import unittest
from unittest import mock

from app.graph import amap_client
from app.models.schemas import Location


# ============================ 实测响应片段(fixture) ============================
#
# 下面这些 dict 都是真的从高德抓下来的字段形状, 不是编造的: 曾经因为凭想象写
# 字段名(把 type 尾段猜成"地级市地名"而不是"地市级地名")导致城市/地点口径判错。


def _poi(name, *, pname, cityname, adname, adcode, lng, lat, poi_type="风景名胜;风景名胜;国家级景点",
         address="", rating="4.5", cost="80", pid=None):
    return {
        "id": pid or f"B{abs(hash(name)) % 10**9}",
        "name": name, "type": poi_type, "address": address,
        "pname": pname, "cityname": cityname, "adname": adname, "adcode": adcode,
        "location": f"{lng},{lat}",
        "biz_ext": {"rating": rating, "cost": cost},
    }


def _geocode(city, *, province=None, district="", adcode="", level="市", lng=120.15, lat=30.27):
    return {
        "province": province or city, "city": city, "district": district,
        "adcode": adcode, "level": level, "location": f"{lng},{lat}",
    }


# 莫干山: geocode 的 10 条记录全是甘肃/广西的同名住宅区(实测), 而 place/text
# 的热度首位永远是浙江湖州德清的那个景区 —— 这正是"必须留一条 POI 后路"的理由
MOGANSHAN_HOT = _poi("莫干山风景名胜区", pname="浙江省", cityname="湖州市",
                     adname="德清县", adcode="330521", lng=119.885724, lat=30.600077)
MOGANSHAN_GEOCODE_JUNK = [
    _geocode("兰州市", province="甘肃省", district="城关区", adcode="620102",
             level="兴趣点", lng=103.8, lat=36.06),
    _geocode("桂林市", province="广西壮族自治区", district="象山区", adcode="450304",
             level="兴趣点", lng=110.29, lat=25.27),
]

# 杭州: geocode 认得出, 走城市口径
HANGZHOU_GEOCODE = [_geocode("杭州市", province="浙江省", adcode="330100",
                             level="市", lng=120.153576, lat=30.287459)]


class _RestStub:
    """按 (端点, 关键词) 派发的假 _rest_get, 并记录所有收到的参数

    记录参数是本测试的重点之一: "到底有没有把地名当 city 传"只能从实参上验证
    —— 传错了高德不报错, 只是悄悄返回全国结果。
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, endpoint, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((endpoint, params))
        key = None
        if endpoint == "place/text":
            key = params.get("keywords")
        elif endpoint == "geocode/geo":
            key = params.get("address")
        table = self.routes.get(endpoint) or {}
        if not isinstance(table, dict):
            return table
        hit = table.get(key)
        if hit is None:
            return {}
        return hit

    def params_of(self, endpoint):
        return [p for e, p in self.calls if e == endpoint]


class _AmapOffline(unittest.TestCase):
    """所有用例的公共底座: 清缓存 + 装假 _rest_get, 保证一条外网请求都不发"""

    def setUp(self):
        for fn in (amap_client.resolve_city, amap_client.search_venues,
                   amap_client._top_poi_location, amap_client._hot_poi):
            cache = getattr(fn, "cache_obj", None)
            if cache is not None:
                cache.clear()

    def _patch_rest(self, routes):
        stub = _RestStub(routes)
        p = mock.patch.object(amap_client, "_rest_get", stub)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._clear_caches)
        return stub

    @staticmethod
    def _clear_caches():
        # _hot_poi 也算进来: 省市县选择(hint)那条路会用它做 detail 的锚点, 不清理
        # 会让用例之间互相串味(前一个用例的假响应喂给后一个)
        for fn in (amap_client.resolve_city, amap_client.search_venues,
                   amap_client._top_poi_location, amap_client._hot_poi):
            cache = getattr(fn, "cache_obj", None)
            if cache is not None:
                cache.clear()


# ============================ 1. 地名归一 ============================


class TestResolveCity(_AmapOffline):
    """resolve_city: 城市走 geocode, 具体地点走"热度首位 POI 反查归属"这条路"""

    def test_city_goes_through_geocode_and_is_kind_city(self):
        self._patch_rest({"geocode/geo": {"杭州": {"geocodes": HANGZHOU_GEOCODE}}})
        canon = amap_client.resolve_city("杭州")

        self.assertIsNotNone(canon)
        self.assertEqual(canon["kind"], "city")
        self.assertEqual(canon["source"], "geocode")
        self.assertEqual(canon["name"], "杭州市")
        self.assertEqual(canon["adcode"], "330100")
        self.assertIsInstance(canon["location"], Location)

    def test_scenic_name_is_rescued_by_hot_poi(self):
        """莫干山: geocode 全是外省同名住宅区 -> 必须改判为具体地点, 而不是返回 None

        这是"救命分支被 _name_related 杀掉"那个历史 bug 的回归测试: 旧实现拿
        geocode 结果的市/县与输入做包含比较, 景区名永远比不中, 于是 resolve_city
        返回 None -> search_venues 把"莫干山"当城市名过滤 -> 候选池 0 条。
        """
        self._patch_rest({
            "geocode/geo": {"莫干山": {"geocodes": MOGANSHAN_GEOCODE_JUNK}},
            "place/text": {"莫干山": {"pois": [MOGANSHAN_HOT, _poi(
                "莫干山民宿", pname="浙江省", cityname="湖州市", adname="德清县",
                adcode="330521", lng=119.88, lat=30.60, poi_type="住宿服务;旅馆招待所")]}},
        })
        canon = amap_client.resolve_city("莫干山")

        self.assertIsNotNone(canon, "景区名解析失败 -> 下游必然空池")
        self.assertEqual(canon["kind"], "place")
        self.assertEqual(canon["source"], "poi")
        self.assertEqual(canon["name"], "湖州市")            # 父地级市: 同城过滤用
        self.assertEqual(canon["district"], "德清县")
        self.assertEqual(canon["adcode"], "330521")
        self.assertEqual(canon["display"], "莫干山（浙江省湖州市德清县）")
        # 锚点必须是景区自己的坐标, 不是湖州市中心 —— 否则 35km 半径圈的是市区
        self.assertAlmostEqual(canon["location"].longitude, 119.885724, places=4)
        self.assertAlmostEqual(canon["location"].latitude, 30.600077, places=4)

    def test_gate_rejects_unrelated_hot_poi(self):
        """解析不出来时必须返回 None: 宁可按原文搜(可能搜偏)也不认一个错的地点

        实测高德的模糊匹配会给出"看着像"的结果: "不存在的城市名xyz" -> 北京烟袋
        斜街, "阿巴阿巴阿巴" -> 廊坊阿巴义餐厅。闸门 = 热度首位 POI 的名字必须与
        输入有包含关系, 否则这次归一按失败处理。
        """
        self._patch_rest({
            "geocode/geo": {"不存在的城市名xyz": {"geocodes": [
                _geocode("锡林郭勒盟", province="内蒙古自治区", adcode="152500", level="盟")]}},
            "place/text": {"不存在的城市名xyz": {"pois": [_poi(
                "烟袋斜街", pname="北京市", cityname="北京市", adname="西城区",
                adcode="110102", lng=116.39, lat=39.94, poi_type="风景名胜;风景名胜;景点")]}},
        })
        self.assertIsNone(amap_client.resolve_city("不存在的城市名xyz"))

    def test_gate_rejects_partial_name_overlap(self):
        """"阿巴阿巴阿巴"不能被"阿巴义餐厅"救走 —— 放宽闸门会放行垃圾输入

        这是"刻意保持严格"的说明性用例: 松一点能救回"环球影城->北京环球度假区"
        这类别名, 但代价是任何含"阿巴"二字的输入都会认到廊坊那家餐厅。两害相权,
        选择严格 —— 认不出就明说认不出(steps_log 里会写)。
        """
        self._patch_rest({
            "geocode/geo": {"阿巴阿巴阿巴": {"geocodes": []}},
            "place/text": {"阿巴阿巴阿巴": {"pois": [_poi(
                "阿巴义餐厅", pname="河北省", cityname="廊坊市", adname="广阳区",
                adcode="131003", lng=116.7, lat=39.5, poi_type="餐饮服务;中餐厅")]}},
        })
        self.assertIsNone(amap_client.resolve_city("阿巴阿巴阿巴"))

    def test_empty_input_is_none(self):
        self._patch_rest({})
        self.assertIsNone(amap_client.resolve_city(""))
        self.assertIsNone(amap_client.resolve_city(None))

    def test_garbage_hot_poi_without_admin_is_rejected(self):
        """热度首位有, 但归属字段全空 -> 也认不出来(没有省市就没法做同城过滤)"""
        self._patch_rest({
            "geocode/geo": {"某某某": {"geocodes": []}},
            "place/text": {"某某某": {"pois": [_poi(
                "某某某", pname="", cityname="", adname="", adcode="", lng=116.4, lat=39.9)]}},
        })
        self.assertIsNone(amap_client.resolve_city("某某某"))


# ============================ 2. 具体地点口径的场地检索 ============================


def _search_routes(dest_pois):
    """search_venues 的两条路都只打 place/text, 所以路由表就是关键词 -> POI 列表"""
    return {"place/text": {k: {"pois": v} for k, v in dest_pois.items()}}


class TestSearchVenuesScope(_AmapOffline):
    """search_venues: 目的地不是城市时, 不传 city, 改用"{地名} {关键词}" + 同城 + 半径"""

    def _canon_routes(self, name, pois):
        """让 resolve_city 把 name 认成具体地点(geocode 空 -> 走热度首位 POI)"""
        return {
            "geocode/geo": {name: {"geocodes": []}},
            "place/text": {name: {"pois": pois[:1]}},
        }

    def test_scope_search_passes_no_city_param(self):
        """核心断言: 绝不能把"莫干山"当 city 传 —— 高德会当作没看见并返回全国混排"""
        near = _poi("莫干山风景区", pname="浙江省", cityname="湖州市", adname="德清县",
                    adcode="330521", lng=119.885724, lat=30.600077, rating="4.6")
        local = _poi("莫干山云溪民宿", pname="浙江省", cityname="湖州市", adname="德清县",
                     adcode="330521", lng=119.89, lat=30.61, poi_type="住宿服务;旅馆招待所")
        other = _poi("莫干山云顶营地", pname="浙江省", cityname="湖州市", adname="德清县",
                     adcode="330521", lng=119.91, lat=30.62, poi_type="住宿服务;旅馆招待所")
        routes = self._canon_routes("莫干山", [near])
        routes["place/text"]["莫干山 露营地"] = {"pois": [local, other]}
        stub = self._patch_rest(routes)

        pois = amap_client.search_venues("莫干山", "露营地")

        self.assertEqual([p.name for p in pois], ["莫干山云溪民宿", "莫干山云顶营地"])
        for params in stub.params_of("place/text"):
            if params.get("keywords") == "莫干山":     # 纯地名那次是 resolve_city 发的
                continue
            self.assertNotIn("city", params, "具体地点口径不许传 city")
            self.assertNotIn("citylimit", params, "具体地点口径不许传 citylimit")

    def test_wrong_city_and_far_candidates_are_dropped(self):
        """同城 + 半径两道过滤: 一个是别的市, 一个是同市 60km 外, 都不该进池"""
        anchor = _poi("莫干山风景区", pname="浙江省", cityname="湖州市", adname="德清县",
                      adcode="330521", lng=119.885724, lat=30.600077)
        good = _poi("莫干山露营地", pname="浙江省", cityname="湖州市", adname="德清县",
                    adcode="330521", lng=119.90, lat=30.61)
        # 杭州(非湖州): cityname/adcode 都不对
        other_city = _poi("西湖露营地", pname="浙江省", cityname="杭州市", adname="西湖区",
                          adcode="330106", lng=120.13, lat=30.24)
        # 同市但 150km 外(湖州 -> 安吉以西), 半径 35km 圈不住
        too_far = _poi("远方营地", pname="浙江省", cityname="湖州市", adname="长兴县",
                       adcode="330522", lng=119.2, lat=31.6)
        routes = self._canon_routes("莫干山", [anchor])
        routes["place/text"]["莫干山 露营地"] = {"pois": [good, other_city, too_far]}
        self._patch_rest(routes)

        pois = amap_client.search_venues("莫干山", "露营地")

        names = [p.name for p in pois]
        self.assertIn("莫干山露营地", names)
        self.assertNotIn("西湖露营地", names, "非同城候选必须丢")
        # 半径外候选在"圈内不足下限"时可以放宽补位 —— 但必须排在圈内候选之后
        if len(names) > 1:
            self.assertEqual(names[0], "莫干山露营地")
            anchor_loc = amap_client._parse_location(anchor["location"])
            self.assertLess(amap_client._distance_km(pois[0].location, anchor_loc),
                            amap_client._SCOPE_RADIUS_KM)

    def test_low_recall_falls_back_to_bare_destination_search(self):
        """"莫干山 团建场地"只回 1 条 -> 补一次"莫干山"宽搜

        实测"莫干山 团建场地"只 1 条(修完还是报"候选不足"), 而"莫干山"回 15 条
        且全在 7km 内。没有这层回退, 前面的修复等于白做。
        """
        anchor = _poi("莫干山风景区", pname="浙江省", cityname="湖州市", adname="德清县",
                      adcode="330521", lng=119.885724, lat=30.600077)
        only = _poi("莫干山景区游客中心", pname="浙江省", cityname="湖州市", adname="德清县",
                    adcode="330521", lng=119.886, lat=30.601, pid="A1")
        wide = [
            _poi("莫干山芦花荡饭店", pname="浙江省", cityname="湖州市", adname="德清县",
                 adcode="330521", lng=119.89, lat=30.60, pid="A2"),
            _poi("莫干山剑池", pname="浙江省", cityname="湖州市", adname="德清县",
                 adcode="330521", lng=119.88, lat=30.59, pid="A3"),
        ]
        routes = self._canon_routes("莫干山", [anchor])
        routes["place/text"]["莫干山 团建场地"] = {"pois": [only]}
        # 宽搜把 only 又带回来一次(真实场景必然如此) —— 必须按 id 去重
        routes["place/text"]["莫干山"] = {"pois": [only, *wide]}
        stub = self._patch_rest(routes)

        pois = amap_client.search_venues("莫干山", "团建场地")

        names = [p.name for p in pois]
        self.assertEqual(len(names), 3, f"宽搜回退没生效: {names}")
        self.assertIn("莫干山剑池", names)
        self.assertEqual(names.count("莫干山景区游客中心"), 1, "两条路的结果要按 id 去重")
        # 宽搜确实发生过, 而不是恰好有 3 条
        self.assertIn("莫干山", [p.get("keywords") for p in stub.params_of("place/text")])

    def test_non_venue_pois_are_dropped(self):
        """站牌/道路/收费站坐标齐全但不可能当场地, 必须在进池前剔掉

        实测古北水镇的首位候选曾是"古北水镇(公交站)", 崇明岛的候选里有
        "崇明秀山路" —— 它们类型字符串末尾不含"公交站"字样(是"公交车站"),
        所以过滤必须按高德的大类前缀做, 不能靠猜子串。
        """
        anchor = _poi("古北水镇", pname="北京市", cityname="北京市", adname="密云区",
                      adcode="110118", lng=117.27, lat=40.66)
        station = _poi("古北水镇(公交站)", pname="北京市", cityname="北京市", adname="密云区",
                       adcode="110118", lng=117.271, lat=40.661,
                       poi_type="交通设施服务;公交车站;公交车站")
        road = _poi("古北水镇路", pname="北京市", cityname="北京市", adname="密云区",
                    adcode="110118", lng=117.272, lat=40.662,
                    poi_type="地名地址信息;交通地名;交通地名")
        plaza = _poi("古北水镇-龙凤温泉广场", pname="北京市", cityname="北京市", adname="密云区",
                     adcode="110118", lng=117.273, lat=40.663)
        routes = self._canon_routes("古北水镇", [anchor])
        routes["place/text"]["古北水镇 景点"] = {"pois": [station, road, plaza]}
        self._patch_rest(routes)

        pois = amap_client.search_venues("古北水镇", "景点")

        names = [p.name for p in pois]
        self.assertIn("古北水镇-龙凤温泉广场", names)
        self.assertNotIn("古北水镇(公交站)", names)
        self.assertNotIn("古北水镇路", names)

    def test_city_destination_keeps_city_limit_search(self):
        """城市口径不许被顺带改掉: 杭州仍然传 city + citylimit 并做归属过滤"""
        hz = _poi("杭州西溪湿地", pname="浙江省", cityname="杭州市", adname="西湖区",
                  adcode="330106", lng=120.06, lat=30.27)
        xj = _poi("伊犁露营地", pname="新疆维吾尔自治区", cityname="伊犁哈萨克自治州",
                  adname="新源县", adcode="654025", lng=84.0, lat=43.4)
        stub = self._patch_rest({
            "geocode/geo": {"杭州": {"geocodes": HANGZHOU_GEOCODE}},
            "place/text": {"团建场地": {"pois": [hz, xj]}},
        })

        pois = amap_client.search_venues("杭州", "团建场地")

        self.assertEqual([p.name for p in pois], ["杭州西溪湿地"])
        city_params = [p for p in stub.params_of("place/text")
                       if p.get("keywords") == "团建场地"]
        self.assertEqual(city_params[0]["city"], "杭州市")
        self.assertEqual(city_params[0]["citylimit"], "true")

    def test_unresolvable_city_yields_empty_not_wrong_data(self):
        """认不出的地名 -> 宁可空池报"候选不足", 也不给一份全国混排的候选"""
        self._patch_rest({
            "geocode/geo": {"不存在的城市名xyz": {"geocodes": []}},
            "place/text": {"不存在的城市名xyz": {"pois": []},
                           "团建场地": {"pois": [_poi(
                               "内蒙古某店", pname="内蒙古自治区", cityname="包头市",
                               adname="昆都仑区", adcode="150203", lng=109.8, lat=40.6)]}},
        })
        self.assertEqual(amap_client.search_venues("不存在的城市名xyz", "团建场地"), [])


# ============================ 3. 距离是约束, 不是提示词里的散文 ============================


class TestRankPoisDistance(unittest.TestCase):
    """_rank_pois 的通勤圈分级: 高分但远 -> 整体降到圈内候选之后

    旧实现只按评分排, "距城市中心超过100km应降级"只写在 prompt 里 —— 实测
    "杭州团建"的评分榜首是 138km 外的千岛湖植物园, 它照样排在候选清单第一位,
    全靠模型自觉避开。
    """

    def _poiinfo(self, name, km_away, rating):
        from app.models.schemas import POIInfo
        # 30.27 纬度上 1 经度 ≈ 96km, 用经度偏移造出指定距离
        return POIInfo(id=name, name=name, type="景点", address="杭州",
                       location=Location(longitude=120.15 + km_away / 96.0,
                                         latitude=30.27), rating=rating)

    def setUp(self):
        from app.graph.nodes import _rank_pois
        self._rank = _rank_pois
        self.anchor = Location(longitude=120.15, latitude=30.27)

    def test_far_high_rated_loses_to_near_low_rated(self):
        far = self._poiinfo("千岛湖植物园", 138.0, "4.8")
        near = self._poiinfo("西湖景区", 12.0, "3.5")

        ranked = self._rank([far, near], self.anchor)

        self.assertEqual([p.name for p in ranked], ["西湖景区", "千岛湖植物园"])

    def test_far_candidates_are_demoted_not_dropped(self):
        """去千岛湖团建本身没问题 —— 只是不该在"去杭州市区"的方案里排第一"""
        far = self._poiinfo("千岛湖植物园", 138.0, "4.8")
        ranked = self._rank([far], self.anchor)
        self.assertEqual(len(ranked), 1)

    def test_without_anchor_behaviour_is_unchanged(self):
        """没给锚点(地名没归一出来)时退回纯评分序, 不误伤"""
        far = self._poiinfo("千岛湖植物园", 138.0, "4.8")
        near = self._poiinfo("西湖景区", 12.0, "3.5")
        ranked = self._rank([far, near])
        self.assertEqual([p.name for p in ranked], ["千岛湖植物园", "西湖景区"])

    def test_missing_rating_still_sinks_within_its_band(self):
        """缺评分只垫底不淘汰(高德 biz_ext.rating 空置率不低, 淘汰会误伤小店)"""
        from app.models.schemas import POIInfo
        unrated = POIInfo(id="x", name="某小店", type="餐饮", address="杭州", rating=None,
                          location=Location(longitude=120.16, latitude=30.27))
        rated = self._poiinfo("西湖景区", 12.0, "3.0")
        ranked = self._rank([unrated, rated], self.anchor)
        self.assertEqual([p.name for p in ranked], ["西湖景区", "某小店"])

    def test_pick_route_anchor_uses_nearest_not_top_rated(self):
        """交通基准场地 = 距目的地最近的候选(照评分榜首算里程会是另一趟行程)"""
        from app.graph.nodes import _pick_route_anchor
        from app.models.schemas import VenueInfo

        def venue(name, km, rating):
            return VenueInfo(name=name, address="杭州", visit_duration=120,
                             description="景点",
                             location=Location(longitude=120.15, latitude=30.27),
                             distance_km=km, rating=rating)

        top = venue("千岛湖植物园", 138.0, "4.8")
        near = venue("西湖景区", 12.0, "3.5")
        self.assertIs(_pick_route_anchor([top, near]), near)

    def test_pick_route_anchor_falls_back_without_distance(self):
        from app.graph.nodes import _pick_route_anchor
        from app.models.schemas import VenueInfo
        v = VenueInfo(name="待定场地", address="", visit_duration=120, description="场地",
                      location=Location(longitude=120.15, latitude=30.27),
                      distance_km=None)
        self.assertIs(_pick_route_anchor([v]), v)
        self.assertIsNone(_pick_route_anchor([]))


# ============================ 4. 出事要让用户看得见 ============================


def _gather_state(**parsed):
    base = {"origin": "上海", "destination": "莫干山", "date": "2026-10-20"}
    base.update(parsed)
    return {"parsed_request": base, "activity_type": "团建"}


# 莫干山锚点(归一结果的 location): 候选都按"离它多少公里"造
MOGANSHAN_ANCHOR = (119.885724, 30.600077)


_UNSET = object()      # canon=None(解析失败) 与 "没有 override" 必须分得开


def _venue_poi(name, km_away, rating="4.5"):
    from app.models.schemas import POIInfo
    lng, lat = MOGANSHAN_ANCHOR
    return POIInfo(id=name, name=name, type="景点", address="德清县", rating=rating,
                   location=Location(longitude=lng + km_away / 96.0, latitude=lat))


class TestGatherInfoSurfacing(unittest.IsolatedAsyncioTestCase):
    """gather_info 必须把"目的地认成什么"和"哪一维真的没查到"交出来

    过去 EMPTY 既不进 trouble(不是降级)也不进 unavailable(不是没配 key), 于是
    整条链路上没有任何人提到它: 用户只看到"场地候选不足/地点待定", 既不知道原因,
    也不知道该改什么。
    """

    def setUp(self):
        from app.graph.errors import call_external

        def _no_wait(fn, **kwargs):
            return call_external(fn, sleep=lambda _s: None, **kwargs)

        p = mock.patch("app.graph.nodes.call_external", _no_wait)
        p.start()
        self.addCleanup(p.stop)

    def _patch_clients(self, canon=_UNSET, **overrides):
        from app.graph import amap_client, bocha_client, train_client
        from app.models.schemas import RouteInfo, WeatherInfo

        default_canon = {
            "name": "湖州市", "adcode": "330521", "province": "浙江省",
            "district": "德清县", "level": "", "kind": "place",
            "display": "莫干山（浙江省湖州市德清县）", "source": "poi", "query": "莫干山",
            "location": Location(longitude=119.885724, latitude=30.600077),
        }
        here = Location(longitude=121.47, latitude=31.23)
        defaults = {
            "resolve_city": lambda *a, **k: (default_canon if canon is _UNSET else canon),
            "search_venues": lambda *a, **k: [_venue_poi("莫干山露营地", 8.0)],
            "get_weather": lambda *a, **k: [WeatherInfo(
                date="2026-10-20", day_weather="晴", night_weather="多云",
                day_temp=22, night_temp=15)],
            "geocode": lambda *a, **k: here,
            "get_route_info": lambda *a, **k: RouteInfo(
                distance=190000.0, duration=150, route_type="driving",
                description="驾车 150 分钟 190 公里"),
            "query_trains": lambda *a, **k: [],
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

    
    async def test_destination_canon_reaches_basic_info(self):
        """四路提案只认这一份目的地 —— 它必须真的存进 basic_info"""
        from app.graph.nodes import gather_info

        self._patch_clients()
        out = await gather_info(_gather_state())

        canon = out["basic_info"]["destination_canon"]
        self.assertEqual(canon["display"], "莫干山（浙江省湖州市德清县）")
        self.assertEqual(canon["kind"], "place")
        # location 必须是可序列化的普通 dict, 不能是 Location 对象(checkpointer 会存)
        self.assertNotIsInstance(canon["location"], Location)
        self.assertAlmostEqual(canon["location"]["longitude"], 119.885724, places=4)
        # 距离是相对**归一后的目的地锚点**算的(景区自己), 不是相对湖州市中心
        self.assertAlmostEqual(out["basic_info"]["venues"][0]["distance_km"], 8.0, delta=0.5)

    async def test_unresolved_destination_is_named_in_steps_log(self):
        """地名认不出来要写明白, 且 basic_info 里不留半截 canon"""
        from app.graph.nodes import gather_info

        self._patch_clients(canon=None)
        out = await gather_info(_gather_state())

        self.assertIsNone(out["basic_info"]["destination_canon"])
        log = "".join(out["steps_log"])
        self.assertIn("未能识别为城市或具体地点", log)
        self.assertIn("莫干山", log)

    async def test_empty_critical_dimension_is_surfaced(self):
        """场地这一路"接口正常但 0 条"必须进 steps_log —— 它是最常见的静默失败"""
        from app.graph.nodes import gather_info

        self._patch_clients(search_venues=lambda *a, **k: [])
        out = await gather_info(_gather_state())

        self.assertEqual(out["data_status"]["venues"]["status"], "empty")
        log = "".join(out["steps_log"])
        self.assertIn("没有查到结果", log)
        self.assertIn("venue", log)

    async def test_empty_optional_dimension_stays_quiet(self):
        """火车/攻略为空是正常业务结果(同城/未配 key), 不能报成"没查到"叫用户改输入"""
        from app.graph.nodes import gather_info

        self._patch_clients()
        out = await gather_info(_gather_state())

        self.assertEqual(out["data_status"]["trains"]["status"], "empty")
        self.assertEqual(out["data_status"]["guides"]["status"], "ok")
        self.assertNotIn("没有查到结果", "".join(out["steps_log"]))

    async def test_approval_payload_carries_destination_and_data_status(self):
        """审批页要能看到"这次到底在给哪儿做方案"以及哪一维是估算的"""
        from app.graph.nodes import _build_approval_payload, gather_info

        self._patch_clients(search_venues=lambda *a, **k: [])
        state = _gather_state()
        out = await gather_info(state)
        state.update(out)

        payload = _build_approval_payload(state)

        dest = payload["destination"]
        self.assertEqual(dest["raw"], "莫干山")
        self.assertEqual(dest["resolved"], "莫干山（浙江省湖州市德清县）")
        self.assertEqual(dest["kind"], "place")
        self.assertEqual(payload["data_status"]["venues"]["status"], "empty")

    async def test_place_inputs_reach_both_resolve_and_search(self):
        """入口的省市县选择要同时进 resolve_city 与 search_venues —— 只给一个是分叉

        state 里存的是 dict(model_dump() 之后的样子), 而不是 PlaceInput 模型:
        模型进 Send payload 会随 checkpointer 序列化, 且缓存键会退化成 repr。
        """
        from app.graph.nodes import gather_info

        seen = []

        def _resolve(name, *a, **k):
            seen.append(("resolve", name, a[0] if a else None))
            return None

        def _venues(*a, **k):
            seen.append(("venues", *a))
            return []

        self._patch_clients(resolve_city=_resolve, search_venues=_venues)
        state = _gather_state()
        state["raw_request"] = {"structured": {"destination_place": _place(
            province="河南省", city="洛阳市", adcode="410300", detail="老君山")}}

        await gather_info(state)

        dest_calls = [c for c in seen if c[0] == "resolve" and c[1] == "莫干山"]
        self.assertEqual(len(dest_calls), 1)
        self.assertEqual(dest_calls[0][2]["detail"], "老君山")     # hint 进了 resolve_city
        self.assertEqual(dest_calls[0][2]["city"], "洛阳市")
        venue_calls = [c for c in seen if c[0] == "venues"]
        self.assertEqual(len(venue_calls), 1)
        self.assertEqual(venue_calls[0][3]["detail"], "老君山")    # **同一个** hint

    async def test_place_inputs_absent_keeps_old_call_shape(self):
        """没有地点入参时不许把 None 当 hint 传进去 —— 那会让缓存键从 ["x"] 变
        ["x", null], 老缓存全部冷启动(功能不变但白白多打一轮外网)"""
        from app.graph.nodes import gather_info

        seen = []

        def _resolve(name, *a, **k):
            seen.append(("resolve", name, len(a)))
            return None

        def _venues(*a, **k):
            seen.append(("venues", len(a)))
            return []

        self._patch_clients(resolve_city=_resolve, search_venues=_venues)
        await gather_info(_gather_state())

        self.assertEqual([c for c in seen if c[0] == "resolve"], [("resolve", "莫干山", 0),
                                                                ("resolve", "上海", 0)])
        self.assertEqual([c for c in seen if c[0] == "venues"], [("venues", 2)])

    async def test_destination_brief_carries_selection_and_conflict_note(self):
        """审批页要能看出"用户选的是上海, 但填的老君山在河南" —— 这是可修的一类错"""
        from app.graph.nodes import _destination_brief, gather_info

        canon = {
            "name": "上海市", "adcode": "310000", "province": "上海市", "district": "",
            "level": "市", "kind": "place", "display": "老君山（上海市）",
            "source": "hint", "query": "老君山",
            "note": "「老君山」实际位于河南省洛阳市栾川县, 与所选「上海市」不一致",
            "location": Location(longitude=111.636, latitude=33.786),
        }
        self._patch_clients(canon=canon)
        state = _gather_state()
        state["raw_request"] = {"structured": {"destination_place": _place(
            province="上海市", city="上海市", adcode="310000", detail="老君山")}}

        out = await gather_info(state)
        state.update(out)
        brief = _destination_brief(state)

        self.assertEqual(brief["raw"], "莫干山")          # raw 仍是用户原话, 不被选择覆盖
        self.assertEqual(brief["selection"]["city"], "上海市")
        self.assertEqual(brief["detail"], "老君山")
        self.assertIn("老君山", brief["note"])
        self.assertIn("老君山", "".join(out["steps_log"]))

    # ---- 出发地天气: 另一个城市, 只喂交通, 不喂风险 ----

    async def test_origin_weather_skipped_when_same_city(self):
        """同城时出发地=目的地, 不该再花一次高德调用去查同一座城的天气

        而且"同城不查"必须与"缺 API Key"分开报 —— 两者都是 DISABLED, 混在一起
        会让 steps_log 谎报一条不存在的配置问题。
        """
        from app.graph.nodes import gather_info
        from app.models.schemas import WeatherInfo

        calls = []

        def _weather(city, *a, **k):
            calls.append(city)
            return [WeatherInfo(date="2026-10-20", day_weather="晴", night_weather="多云")]

        # 默认 canon 对出发地/目的地返回同一个"湖州市" -> same_city 成立
        self._patch_clients(get_weather=_weather)
        out = await gather_info(_gather_state())

        self.assertEqual(len(calls), 1)                                  # 只查了目的地
        self.assertEqual(out["basic_info"]["origin_weather"], [])
        self.assertEqual(out["data_status"]["weather_origin"]["status"], "disabled")
        self.assertNotIn("缺 API Key", "".join(out["steps_log"]))

    async def test_origin_weather_cross_city_feeds_transport_only(self):
        """跨城: 出发地天气进 basic_info 与**交通** payload, 不进 weather, 不进风险检测

        最要防的一处: 出发地小雨 + 目的地晴 + 流程含室外项目。若出发地天气混进
        weather, 风险检测会报"雨天+室外项目, 需准备室内备选" —— 用户出发地下雨
        不代表活动地点下雨, 这是误报且静默。
        """
        from app.graph.nodes import _detect_weather_risk, _proposal_sends, gather_info
        from app.models.schemas import WeatherInfo

        destination_canon = {
            "name": "湖州市", "adcode": "330521", "province": "浙江省",
            "district": "德清县", "level": "", "kind": "place",
            "display": "莫干山（浙江省湖州市德清县）", "source": "poi", "query": "莫干山",
            "location": Location(longitude=119.885724, latitude=30.600077),
        }
        origin_canon = {
            "name": "上海市", "adcode": "310000", "province": "上海市",
            "district": "", "level": "市", "kind": "city", "display": "上海市",
            "source": "geocode", "query": "上海",
            "location": Location(longitude=121.47, latitude=31.23),
        }
        seen = []

        def _weather(city, *a, **k):
            seen.append(city)
            if city == "上海市":                       # 出发地: 雨
                return [WeatherInfo(date="2026-10-20", day_weather="小雨",
                                    night_weather="小雨")]
            return [WeatherInfo(date="2026-10-20", day_weather="晴", night_weather="多云")]

        self._patch_clients(
            resolve_city=lambda name, *a, **k: origin_canon if name == "上海" else destination_canon,
            get_weather=_weather)
        state = _gather_state()
        out = await gather_info(state)

        self.assertEqual(sorted(seen), ["上海市", "湖州市"])              # 两座城各查一次
        self.assertEqual([w["day_weather"] for w in out["basic_info"]["origin_weather"]],
                         ["小雨"])
        # 目的地天气里绝不能混进出发地的雨
        self.assertEqual([w["day_weather"] for w in out["basic_info"]["weather"]], ["晴"])

        state.update(out)
        sends = {s.node: s.arg for s in _proposal_sends(state)}
        self.assertEqual([w["day_weather"]
                          for w in sends["propose_transport"]["origin_weather"]], ["小雨"])
        for node in ("propose_venue", "propose_flow", "propose_food"):
            self.assertNotIn("origin_weather", sends[node])

        flow_prop = {"items": [{"name": "户外拓展", "note": "室外项目"}]}
        parsed = state["parsed_request"]
        flag, status = _detect_weather_risk(out["basic_info"], parsed, flow_prop)
        self.assertIsNone(flag)
        # 目的地晴 -> 这条规则是"清白"(查过且没事), 不是"未评估" —— 两者过去都表现为
        # flags 为空, 正是这一轮要分开的东西
        self.assertEqual(status.status, "clear")
        # 反证: 这份出发地天气**要是**漏进 weather, 上面那条就会误报 —— 断言才有意义
        leaked = dict(out["basic_info"], weather=out["basic_info"]["origin_weather"])
        leak_flag, _ = _detect_weather_risk(leaked, parsed, flow_prop)
        self.assertIsNotNone(leak_flag)

    async def test_origin_weather_out_of_forecast_range_is_stated(self):
        """返回日多半落在高德 3~4 天预报之外 —— 如实说覆盖范围, 并让 prompt 知道行程日期

        不说日期的话, 模型会拿手里这几天的天气去写半个月后那趟行程的 note。
        """
        from app.graph.nodes import _proposal_sends, gather_info
        from app.models.schemas import WeatherInfo
        from app.prompts.templates import build_transport_prompt

        # 目的地与出发地不同城, 但出发地预报只覆盖 10-08~10-10
        origin_canon = {
            "name": "上海市", "adcode": "310000", "province": "上海市",
            "district": "", "level": "市", "kind": "city", "display": "上海市",
            "source": "geocode", "query": "上海",
            "location": Location(longitude=121.47, latitude=31.23),
        }
        dest_canon = {
            "name": "洛阳市", "adcode": "410300", "province": "河南省",
            "district": "", "level": "市", "kind": "city", "display": "洛阳市",
            "source": "geocode", "query": "洛阳市",
            "location": Location(longitude=112.454, latitude=34.620),
        }

        def _weather(city, *a, **k):
            if city == "上海市":
                return [WeatherInfo(date=f"2026-10-{d:02d}", day_weather="晴",
                                    night_weather="多云") for d in (8, 9, 10)]
            return [WeatherInfo(date="2026-10-20", day_weather="晴")]

        self._patch_clients(
            resolve_city=lambda name, *a, **k: origin_canon if name == "上海" else dest_canon,
            get_weather=_weather)
        state = _gather_state(destination="洛阳市", date="2026-10-20",
                              date_end="2026-10-22")
        out = await gather_info(state)

        ow = out["basic_info"]["origin_weather"]
        self.assertEqual(len(ow), 3)                    # 数据照收(保留全部预报)
        state.update(out)
        payload = {s.node: s.arg for s in _proposal_sends(state)}["propose_transport"]

        prompt = build_transport_prompt(
            {}, 20, None, origin="上海", destination="洛阳市",
            origin_weather=payload["origin_weather"],
            date=payload["date"], date_end=payload["date_end"])
        self.assertIn("行程日期: 2026-10-20 ~ 2026-10-22", prompt)
        self.assertIn("预报没覆盖到出发/返回日时不要臆测", prompt)
        self.assertIn("不代表活动地点的天气", prompt)


# ============================ 5. 归一结果进 prompt 的分寸 ============================


class TestTransportPromptCanon(unittest.TestCase):
    """交通 prompt 里的归一目的地只是补充说明, 不许污染"同城/跨城"判断

    这是"已发现但差点写坏"的一处: 把归一后的 display 当 destination 传进去,
    "上海市" != "上海" 会让同城团建被判成跨城, 于是模型开始推高铁。
    """

    def _line(self, **kwargs):
        from app.prompts.templates import build_transport_prompt
        prompt = build_transport_prompt({}, 20, None, **kwargs)
        return [l for l in prompt.splitlines() if "同城/跨城" in l][0]

    def test_same_city_judgement_uses_raw_text(self):
        line = self._line(origin="上海", destination="上海",
                          destination_canon={"kind": "city", "display": "上海市"})
        self.assertIn("与出发地同城", line)

    def test_city_kind_gets_no_redundant_note(self):
        """城市口径下"杭州 -> 杭州市"没有信息量, 添这行只会干扰模型"""
        line = self._line(origin="上海", destination="杭州",
                          destination_canon={"kind": "city", "display": "杭州市"})
        self.assertNotIn("归一目的地", line)
        self.assertIn("跨城(上海 -> 杭州)", line)

    def test_place_kind_shows_admin_parent(self):
        """具体地点必须带行政归属 —— 模型得知道"莫干山"在湖州"""
        line = self._line(origin="上海", destination="莫干山",
                          destination_canon={"kind": "place",
                                             "display": "莫干山（浙江省湖州市德清县）"})
        self.assertIn("归一目的地: 莫干山（浙江省湖州市德清县）", line)

    def test_no_canon_is_safe(self):
        line = self._line(origin="上海", destination="莫干山", destination_canon=None)
        self.assertNotIn("归一目的地", line)


# ======================= 6. 省市县选择(hint): 归属不再靠赌 =======================


# 老君山与"莫干山"是同一条后路的同类: geocode 对景区名整体不可用(见 amap_client
# 模块 docstring), 热度首位 POI 才是河南洛阳栾川那个景区。坐标取该景区的量级 ——
# 用例只关心"锚点来自 detail 而不是所选城市", 不关心精度。
LAOJUNSHAN_HOT = _poi("老君山风景名胜区", pname="河南省", cityname="洛阳市",
                      adname="栾川县", adcode="410324", lng=111.636, lat=33.786)
LUOYANG_GEOCODE = [_geocode("洛阳市", province="河南省", adcode="410300",
                            level="市", lng=112.454040, lat=34.619682)]
LUANCHUAN_GEOCODE = [_geocode("洛阳市", province="河南省", district="栾川县",
                              adcode="410324", level="区县", lng=111.608, lat=33.785)]


def _place(**kw):
    """PlaceInput 的 dict 形态(与 event.py 里 model_dump() 之后的样子一致)"""
    base = {"province": None, "city": None, "district": None,
            "adcode": None, "detail": None}
    base.update(kw)
    return base


class TestResolveCityWithHint(_AmapOffline):
    """选了省市县 -> 归属以**选择**为准; detail 只当锚点坐标与检索词

    这一层替换掉的是"高德热度首位"这个赌注: 跨省重名时它本来会赌歪, 现在用户在
    界面上选的省市县就是权威。用**名字**而不是界面带的 adcode 解析, 是因为名字
    比区划码稳 —— 前端静态表旧了也只影响下拉选项, 不会让归属出错。
    """

    def test_city_hint_skips_all_guessing(self):
        """选到市: 只按名字查一次 geocode 拿权威归属, 一次 place/text 都不发"""
        stub = self._patch_rest({"geocode/geo": {"洛阳市": {"geocodes": LUOYANG_GEOCODE}}})

        canon = amap_client.resolve_city(
            "洛阳", _place(province="河南省", city="洛阳市", adcode="410300"))

        self.assertEqual(canon["kind"], "city")
        self.assertEqual(canon["name"], "洛阳市")
        self.assertEqual(canon["adcode"], "410300")
        self.assertEqual(canon["province"], "河南省")
        self.assertEqual(canon["source"], "hint")
        self.assertEqual(stub.params_of("place/text"), [], "选到市就不该再赌热度首位")
        self.assertEqual([p["address"] for p in stub.params_of("geocode/geo")], ["洛阳市"])

    def test_district_hint_keeps_place_scope(self):
        """选到区县(没填 detail): 与今天一致地落到 place 口径, 只是归属来自选择"""
        stub = self._patch_rest({"geocode/geo": {"栾川县": {"geocodes": LUANCHUAN_GEOCODE}}})

        canon = amap_client.resolve_city(
            "栾川", _place(province="河南省", city="洛阳市",
                           district="栾川县", adcode="410324"))

        self.assertEqual(canon["kind"], "place")
        self.assertEqual(canon["name"], "洛阳市")
        self.assertEqual(canon["district"], "栾川县")
        self.assertEqual(canon["query"], "栾川县")
        self.assertEqual(canon["adcode"], "410324")
        self.assertAlmostEqual(canon["location"].longitude, 111.608, places=3)
        self.assertEqual(stub.params_of("place/text"), [])

    def test_detail_becomes_query_and_anchor_but_name_stays_city(self):
        """选市 + detail=老君山: 检索词/锚点用 detail, name 仍是市名(天气/火车要用)"""
        stub = self._patch_rest({
            "geocode/geo": {"洛阳市": {"geocodes": LUOYANG_GEOCODE}},
            "place/text": {"老君山": {"pois": [LAOJUNSHAN_HOT]}},
        })

        canon = amap_client.resolve_city(
            "洛阳", _place(province="河南省", city="洛阳市",
                           adcode="410300", detail="老君山"))

        self.assertEqual(canon["kind"], "place")
        self.assertEqual(canon["query"], "老君山")        # _search_scope 的检索词前缀
        self.assertEqual(canon["name"], "洛阳市")          # 天气/火车/归属过滤
        self.assertEqual(canon["district"], "栾川县")      # detail 的归属细化
        self.assertEqual(canon["display"], "老君山（河南省洛阳市栾川县）")
        # 锚点必须是 detail 自己的坐标, 不是所选城市的市中心(112.45) —— 否则
        # "在老君山附近找场地"会变成"在洛阳市区找场地"
        self.assertAlmostEqual(canon["location"].longitude, 111.636, places=3)
        self.assertNotIn("note", canon)
        # 锚点走的是 _hot_poi(热度首位)那条路, 不是 geocode —— 景区名在 geocode
        # 里没有"正确的那一条"
        self.assertEqual([p["keywords"] for p in stub.params_of("place/text")], ["老君山"])

    def test_detail_conflict_with_selection_is_surfaced_not_silently_trusted(self):
        """选了上海却填"老君山": 归属照所选, 但必须留下告警 —— 不静默替换成洛阳"""
        shanghai = _geocode("上海市", province="上海市", adcode="310000",
                            level="市", lng=121.473, lat=31.230)
        self._patch_rest({
            "geocode/geo": {"上海市": {"geocodes": [shanghai]}},
            "place/text": {"老君山": {"pois": [LAOJUNSHAN_HOT]}},
        })

        canon = amap_client.resolve_city(
            "上海", _place(province="上海市", city="上海市",
                           adcode="310000", detail="老君山"))

        self.assertEqual(canon["name"], "上海市")
        self.assertEqual(canon["adcode"], "310000")
        self.assertEqual(canon["query"], "老君山")
        self.assertIn("老君山", canon["note"])
        self.assertIn("不一致", canon["note"])

    def test_detail_alone_falls_back_to_the_old_path(self):
        """只填 detail、没选省市县 -> 不造一个假归属, 老老实实走老路(热度首位)"""
        self._patch_rest({
            "geocode/geo": {"莫干山": {"geocodes": MOGANSHAN_GEOCODE_JUNK}},
            "place/text": {"莫干山": {"pois": [MOGANSHAN_HOT]}},
        })

        canon = amap_client.resolve_city("莫干山", _place(detail="莫干山"))

        self.assertEqual(canon["source"], "poi")
        self.assertEqual(canon["kind"], "place")
        self.assertEqual(canon["name"], "湖州市")

    def test_unresolvable_admin_still_uses_the_chosen_names(self):
        """名字解析不出区划记录(表旧/生僻): 名字照用、不崩, 归属过滤会把它显性化"""
        self._patch_rest({"geocode/geo": {}, "place/text": {}})

        canon = amap_client.resolve_city(
            "", _place(province="河南省", city="不存在的市zzz", adcode="419999"))

        self.assertEqual(canon["name"], "不存在的市zzz")
        self.assertEqual(canon["adcode"], "", "解析不出就宁可空着, 不采信界面带的码")
        self.assertEqual(canon["kind"], "city")

    def test_plain_call_is_byte_for_byte_the_old_path(self):
        """hint=None 时走的必须是老两条路, 一个字不改(回归红线)"""
        self._patch_rest({
            "geocode/geo": {"莫干山": {"geocodes": MOGANSHAN_GEOCODE_JUNK}},
            "place/text": {"莫干山": {"pois": [MOGANSHAN_HOT]}},
        })

        canon = amap_client.resolve_city("莫干山")

        self.assertEqual(canon["source"], "poi")
        self.assertEqual(canon["name"], "湖州市")
        self.assertEqual(canon["display"], "莫干山（浙江省湖州市德清县）")


class TestSearchVenuesWithHint(_AmapOffline):
    """hint 必须**同时**进 search_venues —— 它内部自己会再调一次 resolve_city,
    只加给 resolve_city 会在检索这一步重新开赌, 两条路得出两份 canon"""

    def test_hinted_city_search_uses_the_authoritative_name(self):
        stub = self._patch_rest({
            "geocode/geo": {"洛阳市": {"geocodes": LUOYANG_GEOCODE}},
            "place/text": {"团建场地": {"pois": [
                _poi("洛阳某营地", pname="河南省", cityname="洛阳市", adname="栾川县",
                     adcode="410324", lng=112.46, lat=34.63),
            ]}},
        })

        pois = amap_client.search_venues(
            "洛阳", "团建场地", _place(province="河南省", city="洛阳市"))

        self.assertEqual([p.name for p in pois], ["洛阳某营地"])
        q = [p for p in stub.params_of("place/text") if p.get("keywords") == "团建场地"]
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["city"], "洛阳市", "城市口径要用归一后的规范市名")
        self.assertEqual(q[0]["citylimit"], "true")

    def test_hinted_place_search_keys_off_the_detail(self):
        """有 detail 时检索词必须是 detail —— 否则"老君山附近"退化成"洛阳市区" """
        stub = self._patch_rest({
            "geocode/geo": {"洛阳市": {"geocodes": LUOYANG_GEOCODE}},
            "place/text": {
                "老君山": {"pois": [LAOJUNSHAN_HOT]},
                "老君山 露营地": {"pois": [
                    _poi("老君山某营地", pname="河南省", cityname="洛阳市",
                         adname="栾川县", adcode="410324", lng=111.64, lat=33.79),
                ]},
            },
        })

        pois = amap_client.search_venues(
            "洛阳", "露营地",
            _place(province="河南省", city="洛阳市", detail="老君山"))

        self.assertIn("老君山某营地", [p.name for p in pois])
        scoped = [p for p in stub.params_of("place/text")
                  if p.get("keywords") == "老君山 露营地"]
        self.assertEqual(len(scoped), 1)
        self.assertNotIn("city", scoped[0], "具体地点口径不许传 city")
        self.assertNotIn("citylimit", scoped[0], "具体地点口径不许传 citylimit")

    def test_wrong_city_noise_does_not_suppress_the_wide_fallback(self):
        """原始条数够但**同城**为 0 时必须补宽搜 —— 否则选到区县就是 0 条候选

        实测: 高德对 "栾川县 团建场地" 回 4 条**北京**的团建场馆。旧闸门按原始
        条数(4 >= 下限)判定"召回充足", 于是不补宽搜, 这 4 条再被同城过滤全丢掉
        -> 0 条, 而用户只看到"候选不足", 从日志里也查不出为什么。裸名 "栾川县"
        实测有 3 条同城候选, 补一次就能救回来。
        """
        beijing = [_poi("北京某团建场地", pname="北京市", cityname="北京市",
                        adname="朝阳区", adcode="110105", lng=116.47 + i * 0.01,
                        lat=39.92, poi_type="体育休闲服务;休闲场所;休闲场所")
                   for i in range(4)]
        local = _poi("栾川某营地", pname="河南省", cityname="洛阳市", adname="栾川县",
                     adcode="410324", lng=111.62, lat=33.79)
        stub = self._patch_rest({
            "geocode/geo": {"栾川县": {"geocodes": LUANCHUAN_GEOCODE}},
            "place/text": {"栾川县 团建场地": {"pois": beijing},
                           "栾川县": {"pois": [local]}},
        })

        pois = amap_client.search_venues(
            "栾川", "团建场地",
            _place(province="河南省", city="洛阳市", district="栾川县", adcode="410324"))

        self.assertEqual([p.name for p in pois], ["栾川某营地"])
        kws = [p["keywords"] for p in stub.params_of("place/text")]
        self.assertIn("栾川县", kws, "同城为 0 时必须补一次裸名宽搜")


class TestCanonPublicWhitelist(unittest.TestCase):
    """进 state 的 canon 必须是**白名单**字段 —— 逐键外扩会把对象带上 checkpointer"""

    def test_unknown_keys_are_dropped_and_location_is_plain(self):
        from app.graph.nodes import _canon_public

        out = _canon_public({"name": "洛阳市", "kind": "city", "note": "冲突: 见 steps_log",
                             "secret": Location(longitude=0.0, latitude=0.0),
                             "location": Location(longitude=112.454, latitude=34.620)})

        self.assertEqual(out["name"], "洛阳市")
        self.assertEqual(out["note"], "冲突: 见 steps_log")   # 审批页要看它
        self.assertNotIn("secret", out)
        self.assertEqual(out["location"], {"longitude": 112.454, "latitude": 34.620})