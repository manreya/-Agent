"""数据模型定义(多人团建活动策划)"""

from datetime import date as _date, datetime as _datetime
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


def to_iso_date(value) -> Optional[str]:
    """把常见日期写法归一成 YYYY-MM-DD; 无法解析则抛错

    为什么放在模型层强制: 下游 reconcile/天气/火车票/时间线全部把 date 当
    **字符串**比较与查询(如 nodes.py 里 `date_end < date` 也是字符串比较),
    一个 "2026/10/01" 会让所有日期判断静默失效。所以进模型就只能是 ISO 形态。
    这里只归一"同一天的不同写法"(斜杠/点/中文/带时间), 不做"10月1日缺年份"
    这类补全 —— 缺少年份属于抽取失败, 应该退回重填而不是猜一个年份。

    抛 ValueError 而非静默置 None: 在 LLM 抽取路径上, invoke_structured 会把
    字段级错误喂回模型让它重填(见 model.py::schema_fix_instruction); 静默置
    None 只会让"日期没抽到"伪装成"用户没说日期"。
    """
    if value is None:
        return None
    if isinstance(value, _datetime):
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    head = (text.replace("年", "-").replace("月", "-").replace("日", "")
                .split("T")[0].split(" ")[0])
    parts = [p for p in head.replace("/", "-").replace(".", "-").split("-") if p]
    if len(parts) != 3:
        raise ValueError(f"日期必须是 YYYY-MM-DD 格式, 收到 {text!r}")
    try:
        return _date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
    except ValueError as exc:
        raise ValueError(f"日期 {text!r} 不是有效日期({exc})") from exc


# ============ 请求模型 ============

class PlaceInput(BaseModel):
    """结构化地点入参(前端省市县选择 + 自由输入的"更具体地点")

    为什么单独建模, 而不是把省市县拼进 origin/destination 字符串: 拼成一个串
    再让后端去切分"哪截是省、哪截是市", 就是昨天修掉的那类问题。这里把**用户
    的选择**当权威归属, 字段名即语义。

    两条关键口径:
    - adcode 只作交叉校验: 前端静态表可能滞后(撤县设区), 归属一律由**名字**
      重新解析, 不采信传进来的码;
    - detail 只当锚点坐标与检索词(如"老君山"), 不参与归属判定。

    注意本模型**不挂 EventRequest**: nodes.parse_request 的自由文本路径拿
    EventRequest 当 LLM 的工具 schema(nodes.py:244 "invoke_structured(EventRequest, ...)"),
    把地点入参放上去等于让 LLM 去填一份"权威归属" —— 而幻觉出来的 hint 会直接
    跳过 _name_related 等所有猜测闸门, 比没有 hint 更糟。故它只走入口契约
    (PlanStartBody)与 raw_request["structured"], 由 gather_info 读取。
    """
    province: Optional[str] = Field(default=None, max_length=30, description="省级名称, 如 河南省")
    city: Optional[str] = Field(default=None, max_length=30, description="地级名称, 如 洛阳市")
    district: Optional[str] = Field(default=None, max_length=30, description="区县名称, 如 栾川县")
    adcode: Optional[str] = Field(default=None, max_length=12,
                                  description="前端静态表带的区划码; 仅作交叉校验, 归属不采信它")
    detail: Optional[str] = Field(default=None, max_length=50,
                                  description="更具体的地点, 如 老君山; 只当锚点坐标与检索词")


class EventRequest(BaseModel):
    """团建活动策划请求(规范化后的结构化需求)

    两个来源共用本模型:
    - 表单/API 直传的结构化输入 -> 程序化组装(零 LLM);
    - 自由文本 -> LLM 抽取(缺失项记录在 missing_fields, 由调用方驱动补全)。
    因此 raw_text 默认空串: 结构化输入时没有原文可填, 也不该强迫 LLM 回抄。
    """
    raw_text: str = Field(default="", description="用户原始输入文本(结构化输入时可为空)", example="20人团建,预算1万,10月1日北京出发")
    people_count: Optional[int] = Field(default=None, ge=1, description="参与人数(LLM抽取)")
    date: Optional[str] = Field(default=None, description="活动开始日期 YYYY-MM-DD(LLM抽取)")
    date_end: Optional[str] = Field(default=None, description="活动结束日期 YYYY-MM-DD(多日活动填写, 单日/未提及为 null)(LLM抽取)")
    budget: Optional[float] = Field(default=None, ge=0, description="总预算(元; 非人均)(LLM抽取)")

    @field_validator("date", "date_end", mode="before")
    @classmethod
    def _normalize_date(cls, v):
        """归一为 ISO 日期(见 to_iso_date), 非法格式直接拒绝"""
        return to_iso_date(v)
    origin: Optional[str] = Field(default=None, description="出发地/集合地(LLM抽取)")
    destination: Optional[str] = Field(default=None, description="目的地/活动城市(与出发地同城可缺省, LLM抽取)")
    transport_pref: Optional[str] = Field(default=None, description="优先通行方式,如 高铁/自驾/大巴/不限(LLM抽取)")
    activity_type: Optional[str] = Field(default=None, description="活动类型,如 团建/聚餐/户外(LLM抽取)")
    missing_fields: List[str] = Field(default_factory=list, description="缺失的必填项 key 列表")

    class Config:
        json_schema_extra = {
            "example": {
                "raw_text": "20人团建,预算1万,10月1日北京出发,想搞户外拓展",
                "people_count": 20,
                "date": "2026-10-01",
                "budget": 10000.0,
                "origin": "北京",
                "activity_type": "户外拓展",
                "missing_fields": []
            }
        }


class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", example="团建场地")
    city: str = Field(..., description="城市", example="北京")
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(..., description="起点地址", example="北京市朝阳区阜通东大街6号")
    destination_address: str = Field(..., description="终点地址", example="北京市海淀区上地十街10号")
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: str = Field(default="walking", description="路线类型: walking/driving/transit")


# ============ 成员偏好 ============

class PersonPref(BaseModel):
    """单成员偏好

    所有字段都带长度上限: 偏好是**用户输入**的一部分, 不设界的话一次请求可以
    提交任意长的姓名/任意多标签, 直接进 prompt 撑爆 context。

    headcount 声明"这一行代表几个人": 表单既可以只提交 1 条"全员汇总", 也可以
    逐人提交 N 条。旧的 len(prefs)==1 猜法无法区分"1 人团队"和"代表 30 人的汇总
    行", 代填场景会把 1 个人的票放大成 30 个人; 现在由调用方声明, 统计一律
    += headcount。

    fitness_level 允许为 None: 默认值 3 会让"没填体能"的人静默进体能分布, 稀释
    _detect_intensity_conflict 的弱体能占比(阈值 25%), 把真冲突掩盖成假阴性。
    未表态就该是未表态。
    """
    person_name: str = Field(..., max_length=50, description="成员姓名/标识")
    headcount: int = Field(default=1, ge=1, description="本行代表人数(逐人=1, 全员汇总行=总人数)")
    activity_prefs: List[str] = Field(default_factory=list, max_length=20, description="活动偏好标签", example=["爬山", "剧本杀"])
    fitness_level: Optional[int] = Field(default=None, ge=1, le=5, description="体能水平 1-5; 未填写为 None(不参与体能分布)")
    dietary: List[str] = Field(default_factory=list, max_length=20, description="饮食禁忌/偏好", example=["素食"])


class PrefSummary(BaseModel):
    """全员偏好汇总(aggregate_prefs 的纯 Python 产出, 零 LLM)"""
    people_count: Optional[int] = Field(default=None, ge=1, description="总人数")
    stated_people: Optional[int] = Field(default=None, ge=0, description="已表态人数 sum(headcount); 与 people_count 的差额为未表态")
    dietary_all: List[str] = Field(default_factory=list, description="全员饮食合并标签(由 dietary_counts 派生)")
    dietary_counts: Dict[str, int] = Field(default_factory=dict, description="忌口人数,如 {'素食': 3}")
    fitness_distribution: Dict[str, int] = Field(default_factory=dict, description="体能分布,如 {'3': 10, '4': 7}; 仅统计已填写体能的人")
    activity_votes: Dict[str, int] = Field(default_factory=dict, description="活动投票,如 {'爬山': 12, '聚餐': 8}")
    conflict_points: List[str] = Field(default_factory=list, description="偏好分歧/约束提示(规则生成, 非 LLM)")


# ============ 基础信息 ============

class Location(BaseModel):
    """地理位置"""
    longitude: float = Field(..., description="经度")
    latitude: float = Field(..., description="纬度")


class VenueInfo(BaseModel):
    """场地信息(由高德 POI 候选转换/富化)"""
    name: str = Field(..., description="场地名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., description="建议活动时长(分钟)")
    description: str = Field(..., description="场地描述")
    category: Optional[str] = Field(default="场地", description="场地类别")
    rating: Optional[float] = Field(default=None, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="场地图片URL列表")
    poi_id: Optional[str] = Field(default="", description="POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: int = Field(default=0, description="门票/场地价格(元)")
    web_note: Optional[str] = Field(
        default=None,
        description="博查Web口碑摘要(程序化排序top3候选附带, 'Web口碑 | ...'格式; 未配置key或无结果为空)",
    )
    distance_km: Optional[float] = Field(
        default=None,
        description="距目的地锚点的直线距离(km, 程序化计算): 目的地是城市时锚点是市中心, "
                    "是具体地点(景区/古镇)时锚点是该地点本身; 用于通勤圈排序与交通锚点选择",
    )


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Union[int, str] = Field(default=0, description="白天温度")
    night_temp: Union[int, str] = Field(default=0, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError:
                return 0
        return v


class POIInfo(BaseModel):
    """POI信息(高德原始搜索结果)"""
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    tel: Optional[str] = Field(default=None, description="电话")
    rating: Optional[str] = Field(default=None, description="评分(高德原始字符串,如 '4.5')")
    cost: Optional[str] = Field(default=None, description="人均消费(元,高德原始字符串)")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., description="距离(米)")
    duration: int = Field(..., description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")


class BasicInfo(BaseModel):
    """基础信息(并行采集节点产出)"""
    venues: List[VenueInfo] = Field(default_factory=list, description="场地候选 top10(高德POI转换)")
    weather: List[WeatherInfo] = Field(default_factory=list, description="目的地天气预报")
    origin_weather: List[WeatherInfo] = Field(
        default_factory=list,
        description="出发地天气预报(仅跨城时采集, 同城为空): 回答'出发日/返回日赶路会不会"
                    "受天气影响', **只喂交通提案**。刻意与 weather 分开而不是塞进同一个"
                    "列表 —— 天气条目没有城市字段, 混在一起风险检测会拿出发地的雨去说"
                    "目的地的室外项目要改室内(误报且静默)",
    )
    route_info: Optional[RouteInfo] = Field(default=None, description="路线信息")
    origin_location: Optional[Location] = Field(default=None, description="出发地坐标(geocode 结果, 流程提案逐段转场的起点)")
    train_info: List[dict] = Field(default_factory=list, description="火车班次/票价(聚合数据12306; 未配置key或同城为空)")
    guides: List[dict] = Field(default_factory=list, description="游玩攻略/口碑搜索结果(博查Web Search; 未配置key为空)")
    route_anchor: Optional[dict] = Field(
        default=None,
        description="交通路线基准场地 {name, distance_km}: 取距目的地锚点最近的候选, "
                    "避免用 300km 外的景区当基准而让交通报价失真",
    )
    destination_canon: Optional[dict] = Field(
        default=None,
        description="归一后的目的地 {name, adcode, province, district, level, kind, "
                    "display, source, query, location}: **四路提案只认这一份目的地**。"
                    "存在的理由: 采集层按归一结果检索(乌镇 -> 嘉兴市桐乡市), 提案层过去"
                    "拿的是用户原话(乌镇), 两者可以自相矛盾 —— 方案里写着'上海->乌镇' "
                    "而场地全在嘉兴市区, 没有任何断言能发现。kind=city(整市范围)/"
                    "place(具体地点, 按半径检索)决定候选是怎么来的, 提案层据此解释候选范围。",
    )


# ============ 方案与校验 ============

class ProposalItem(BaseModel):
    """报价明细单项"""
    name: str = Field(..., description="明细名称")
    cost: float = Field(..., ge=0, description="单项费用(元)")
    note: Optional[str] = Field(default=None, description="备注")
    reason: Optional[str] = Field(
        default=None,
        description="结构化推荐理由(主推项必填, 1句话: 评分/人均/距离/与团队契合点, 及与备选的对比差异)",
    )
    location: Optional[dict] = Field(
        default=None,
        description="活动地点(仅 flow 提案的活动项): {name, location(经度,纬度), address}, 必须取自真实 POI 候选, 不得编造",
    )


class ProposalOption(BaseModel):
    """多候选备选项(供审批页挑选, 如 3 家候选餐厅/多种交通方式)"""
    name: str = Field(..., description="候选名称,如 '金 Grand 大巴 45座'/'G1024 高铁 二等座'")
    est_cost: float = Field(default=0.0, ge=0, description="该候选预估费用(元, 总计)")
    note: Optional[str] = Field(default=None, description="候选亮点/依据,如 '人均85, 距场地1.2km' 或 '票价来自12306查询'")
    reason: Optional[str] = Field(
        default=None,
        description="结构化推荐理由(每个候选必填, 说明为何排在这个位置: 评分/人均/距离/口碑, 与其他候选的差异)",
    )


class MealOptions(BaseModel):
    """单顿餐饮的候选餐厅组(逐顿挑选, 审批页按 顿 勾选)"""
    day: str = Field(..., description="所属天次, 如 'D1'(多日)或 '当天'(单日)")
    meal_type: str = Field(..., description="餐次: 早餐/午餐/晚餐")
    options: List[ProposalOption] = Field(default_factory=list, description="该顿 2-3 家候选餐厅")


class Proposal(BaseModel):
    """单路提案(交通/场地/流程/餐饮)"""
    category: Literal["transport", "venue", "flow", "food"] = Field(..., description="提案类别")
    items: List[ProposalItem] = Field(default_factory=list, description="报价明细")
    total_cost: float = Field(default=0.0, ge=0, description="类别合计(元)")
    description: str = Field(default="", description="方案说明")
    options: List[ProposalOption] = Field(default_factory=list, description="多候选备选(交通/餐饮类提供, 供审批页挑选)")
    meals: List[MealOptions] = Field(default_factory=list, description="逐顿餐饮候选(餐饮类提案, 按 天次×餐次 分组)")
    route_segments: List[str] = Field(default_factory=list, description="逐段转场路线(流程提案, 程序化调高德direction拼接, 如 '出发地→XX风景区: 驾车约25分钟(12.3km)')")


class ConflictReport(BaseModel):
    """校验报告"""
    over_budget: float = Field(default=0.0, description="超预算金额(元), 负数=结余")
    conflicts: List[str] = Field(default_factory=list, description="冲突列表")
    suggestions: List[str] = Field(default_factory=list, description="修改建议")
    # 冲突归属: 类别 -> 该类别相关的冲突原文。**没出现在任何类别下的即为全局冲突**
    # (典型是预算: 超支要四路一起让利, 归属不到某一路头上)。
    # 用途有两个, 都只是"谁能修这条", 不改变冲突本身:
    #   1. 回炉只重派**相关的那几路**(见 route_after_reconcile), 而不是四路全跑一遍;
    #   2. 每路的 prompt 只看到与自己相关的冲突(见 build_conflict_feedback),
    #      不再把"场地容量不足"塞进餐饮路的 prompt。
    # 名字带 conflicts_ 前缀是**故意的**: 审批 payload 里已有一个 by_category,
    # 指的是"每类的花费"; 两个 by_category 摆在一起, 迟早被读成同一个东西。
    # 默认空 dict: 老报告/手工构造的报告没有归属信息 -> 保守当作全局, 四路全派。
    conflicts_by_category: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="冲突归属(类别->冲突原文); 未列入任何类别的视为全局冲突")


class RiskFlag(BaseModel):
    """单条风险标记(结构化)

    过去标记是自由文本列表, 于是"哪条标记被哪条备选覆盖了"只能靠模型回抄原文、
    靠人肉眼比对; 等级也只能数条数("一条 medium、多条 high") —— 一条台风和
    两条鸡毛蒜皮的标记同分, 而单独一条致命标记反而只是 medium。

    现在每条标记自带: 稳定标识(备选按它关联)、规则来源、严重度、机器可读证据。
    严重度由**规则自己**定(降水+室外=high, 极端天气=high, 强度尖峰按弱体能占比
    分档), 不再由条数推。
    """
    id: str = Field(..., description="稳定标识, 形如 'weather_outdoor:2026-10-31'(备选按此关联)")
    rule: Literal["weather_outdoor", "extreme_weather", "intensity_fitness"] = Field(
        ..., description="产出该标记的规则")
    severity: Literal["medium", "high"] = Field(..., description="该条自身的严重度(等级取所有标记的最大值)")
    detail: str = Field(..., description="展示文案(带具体数字/项目名)")
    day: Optional[str] = Field(default=None, description="命中的活动日 YYYY-MM-DD(多日活动; 非天气类为 None)")
    evidence: Dict[str, Any] = Field(
        default_factory=dict,
        description="机器可读证据(降水词/室外项目名/强度/弱体能占比与样本量), 供审批页与测试断言")


class RuleStatus(BaseModel):
    """单条规则的执行情况

    存在的理由与采集侧那张 data_status 完全一样: 采集失败过去只有"静默空列表"
    和"整节点崩"两种表达, 于是"今天真没火车"和"聚合数据挂了"长得一模一样。
    风险规则有同一处病: "查过且没事"和"根本没查到"都表现为 flags 为空, 都拿到
    level=low 和一个绿色标签 —— 审批人读到的"低风险"其实可能是"没评估"。

    所以每条规则都必须表态: 命中 / 清白 / 未评估(带原因), 等级里的 unknown
    由此派生(全部规则都未评估时才为 unknown)。
    """
    rule: str = Field(..., description="规则标识")
    name: str = Field(default="", description="规则名(展示用)")
    status: Literal["hit", "clear", "not_evaluated"] = Field(..., description="命中/清白/未评估")
    reason: str = Field(default="", description="未评估的原因(如'出发地未取到天气预报')")
    detail: str = Field(default="", description="结论说明(命中或清白时的一句话)")


class RiskAlternativeDraft(BaseModel):
    """LLM 产出的备选草案(仅 covers/plan/est_cost 三个字段)

    刻意与存进报告的 RiskAlternative 分开: "这笔钱是不是模型估的""有没有计入
    合计"是**程序知道的事实**, 不能让模型顺手填 —— 与 risk 只让程序定等级同理。
    """
    covers: List[str] = Field(..., description="覆盖哪些风险标记, 填标记的 id 字段(不是原文回抄)")
    plan: str = Field(..., description="备选方案描述, 如 '雨天改室内剧本杀场地: XX店'")
    est_cost: float = Field(default=0.0, ge=0, description="备选方案预估总费用(元), 无额外费用为0")


class RiskAlternatives(BaseModel):
    """LLM 备选方案生成的结构化输出 schema(仅 flags 非空时调用一次)"""
    alternatives: List[RiskAlternativeDraft] = Field(
        default_factory=list, description="逐条风险对应的备选方案")


class RiskAlternative(BaseModel):
    """存进报告的备选(草案 + 程序校验/标注后的事实)"""
    covers: List[str] = Field(default_factory=list, description="经校验确实存在的标记 id(覆盖不到的已剔除)")
    plan: str = Field(..., description="备选方案描述")
    est_cost: float = Field(default=0.0, ge=0, description="备选方案预估总费用(元)")
    est_cost_is_estimate: bool = Field(
        default=True, description="费用是否为模型估算(程序填写): True=估算, 审批页必须标注")
    counted_in_total: bool = Field(
        default=False, description="是否已计入预算合计(程序填写): 备选一律 False, 免得读成已扣款")


class RiskReport(BaseModel):
    """风险评估报告(规则层零LLM; 有标记时追加一次 LLM 备选生成)"""
    level: Literal["low", "medium", "high", "unknown"] = Field(
        default="unknown",
        description="风险等级: 全部规则未评估=unknown; 否则取所有标记严重度的最大值(无标记=low)")
    flags: List[RiskFlag] = Field(default_factory=list, description="规则层标记(结构化)")
    rules: List[RuleStatus] = Field(default_factory=list, description="每条规则的执行情况(命中/清白/未评估)")
    alternatives: List[RiskAlternative] = Field(default_factory=list, description="LLM 备选方案(仅 flags 非空时生成)")
    uncovered_flags: List[str] = Field(
        default_factory=list, description="没有被任何备选覆盖的标记 id(必须显式告知审批人, 不能只报'已给出N条')")
    unevaluated_inputs: List[str] = Field(
        default_factory=list, description="强度解析不出的项目名(规则的盲区, 列出来而不是静默不计入)")
    summary: str = Field(default="", description="一句话结论, 供审批页展示")


class EventPlan(BaseModel):
    """最终方案: 四路 Proposal 汇总"""
    proposals: List[Proposal] = Field(default_factory=list, description="四路提案(transport/venue/flow/food 各一项,按category区分)")
    conflict_points: List[str] = Field(default_factory=list, description="冲突点")
    risk_report: str = Field(default="", description="风险报告")
    pref_coverage: Dict[str, str] = Field(default_factory=dict, description="偏好覆盖情况,如 {'爬山': '已覆盖(张三、李四)'}")
    checklist: Optional[dict] = Field(default=None, description="任务清单(TaskChecklist.model_dump(): 分工/采购/通知文案, 审批通过后产出)")
    memory_status: Optional[dict] = Field(default=None, description="团队记忆写入结果(written/reason/retro_key)。写入可能因存储故障降级放行, 前端要如实告知, 不能假定已存档")


# ============ 任务清单 ============

class Assignment(BaseModel):
    """分工单项"""
    role: str = Field(..., description="分工角色/任务")
    owner: str = Field(..., description="负责人")
    detail: Optional[str] = Field(default=None, description="任务说明")


class PurchaseItem(BaseModel):
    """采购清单单项(带预算项)"""
    item: str = Field(..., description="采购项名称")
    quantity: int = Field(default=1, ge=1, description="数量")
    est_cost: float = Field(default=0.0, ge=0, description="预估费用(元)")
    note: Optional[str] = Field(default=None, description="备注")
    # 属于哪一路提案(transport/venue/flow/food)。**没有这个字段就没法校验**
    # "采购合计有没有超过对应提案的报价" —— 提示词里写着这条要求, 但程序无从执行,
    # 而模型编出来的花费正是最该被程序兜住的东西。程序侧规范化: 认不出的一律置空
    # 并记进 warnings, 不硬塞一个猜测的类别。
    category: Optional[Literal["transport", "venue", "flow", "food"]] = Field(
        default=None, description="归属提案类别(程序规范化, 认不出为 None)")
    # 费用是否为模型估算: 由**程序**填, 不由模型声明。清单里的钱从来没有
    # 被询过价, 一律是估算 —— 这个事实不能让写它的人自己说。
    est_cost_is_estimate: bool = Field(
        default=True, description="预估费用为模型估算(程序填写)")


class TaskChecklist(BaseModel):
    """任务清单"""
    assignments: List[Assignment] = Field(default_factory=list, description="分工表")
    purchase_list: List[PurchaseItem] = Field(default_factory=list, description="采购清单(带预算项)")
    notice_text: str = Field(default="", description="通知文案")
    # 清单是最后一步, 且用户已经点了"通过" —— 失败不该让它变空手。
    # degraded=True 表示这份清单是程序按提案直接拼的(模型那次调用失败/超时),
    # 内容仍然只含方案里已有的事实, 精美度差一些而已。
    degraded: bool = Field(default=False, description="是否为程序兜底生成(非模型产出)")
    # 程序发现的、关于这份清单本身的问题(超提案报价/认不出的归属/兜底原因)。
    # 与内容分开存: 一条警告不是清单里的一个采购项。
    warnings: List[str] = Field(default_factory=list, description="程序校验警告")


# ============ 响应模型 ============

class EventPlanResponse(BaseModel):
    """团建活动计划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[EventPlan] = Field(default=None, description="团建活动计划数据")


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default_factory=list, description="POI列表")


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default_factory=list, description="天气信息")


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")
