"""偏好词表与同义归一(纯数据 + 纯函数, 零依赖)

存在意义: 活动/忌口都是 tags 自由输入, 同一个东西的多种写法("爬山/登山/徒步")
会把票数拆成好几个桶, 于是提示生成器可能写出"10人想爬山 vs 8人想登山" ——
本质是同一件事的**假冲突**, 而它会作为统计结果展示在审批页上。

两条原则:
1. **只覆盖高频同义词**, 不追求穷尽。同义词表命不中的标签一律原样透传,
   自定义活动(旱地冰壶/桨板…)不能被吞掉 —— 这正是保留自由输入的前提。
2. 规范词必须是**活动名/忌口名**, 不是场地类别。不要复用 ACTIVITY_KEYWORDS
   (那张表是"活动 -> POI 搜索词", 值是场地类别: 爬山/登山/徒步 都映射到
   "风景区"), 拿它当规范词会把活动合并成场地, 与"露营地/餐厅"混在一个粒度上。

前端 frontend/src/utils/lexicon.ts 是这份表的镜像, 用于输入时的即时归一。
表以这里为准: 前端即使滞后, 后端仍会折叠(前端只影响用户当场看到的标签写法)。

本模块还放风险评估要用的**项目画像表**(项目 -> 强度/室内外/时段)。它属于
"纯数据表 + 纯函数"的同一类东西, 过去散在风险节点里当三张平行表维护, 已漂移过
一次(见 PROJECT_PROFILES 上方的说明)。
"""

from typing import Dict, List, NamedTuple, Optional, Sequence

# 活动同义表: 各种写法(lower) -> 规范活动名
# 爬山/登山/徒步 统一为"爬山": 对投票而言三者是同一件事(都落在风景区),
# 分开计数只会造出假分歧; 分强度可以在流程提案里按体能分布处理。
ACTIVITY_SYNONYMS: Dict[str, str] = {
    "爬山": "爬山", "登山": "爬山", "徒步": "爬山", "户外徒步": "爬山",
    "hiking": "爬山", "徒步旅行": "爬山",
    "剧本杀": "剧本杀", "剧本杀推理": "剧本杀", "推理本": "剧本杀",
    "密室": "密室逃脱", "密室逃脱": "密室逃脱",
    "聚餐": "聚餐", "吃饭": "聚餐", "团餐": "聚餐",
    "烧烤": "烧烤", "bbq": "烧烤", "自助烧烤": "烧烤",
    "露营": "露营", "露营烧烤": "露营",
    "ktv": "KTV", "k歌": "KTV", "唱歌": "KTV",
    "桌游": "桌游", "桌游馆": "桌游", "棋牌": "桌游",
    "温泉": "温泉", "泡温泉": "温泉",
    "骑行": "骑行", "自行车": "骑行", "骑车": "骑行",
    "飞盘": "飞盘", "极限飞盘": "飞盘",
    "漂流": "漂流", "越野": "漂流",
    "滑雪": "滑雪", "滑雪场": "滑雪",
    "古镇": "古镇", "古镇游": "古镇",
    "cs": "真人CS", "真人cs": "真人CS", "真人镭战": "真人CS",
    "拓展": "拓展训练", "团建拓展": "拓展训练", "拓展训练": "拓展训练",
}

# 忌口同义表: 各种写法(lower) -> 规范忌口名
# 重叠只会让餐饮方案更保守(避开所有标签), 不会漏掉约束, 所以这里可以放心折叠。
DIETARY_SYNONYMS: Dict[str, str] = {
    "素食": "素食", "吃素": "素食", "纯素": "素食", "全素": "素食",
    "vegan": "素食", "vegetarian": "素食",
    "清真": "清真", "halal": "清真", "穆斯林": "清真",
    "不吃辣": "不吃辣", "忌辣": "不吃辣", "不吃辣椒": "不吃辣",
    "海鲜过敏": "海鲜过敏", "不吃海鲜": "海鲜过敏", "海鲜忌口": "海鲜过敏",
    "不喝咖啡": "不喝咖啡", "忌咖啡": "不喝咖啡", "不喝奶茶": "不喝咖啡",
    "不吃牛羊肉": "不吃牛羊肉", "不吃牛羊": "不吃牛羊肉", "不吃牛肉": "不吃牛羊肉",
    "乳糖不耐": "乳糖不耐", "不吃乳制品": "乳糖不耐", "不喝牛奶": "乳糖不耐",
    "坚果过敏": "坚果过敏", "花生过敏": "坚果过敏",
    "不吃香菜": "不吃香菜", "忌香菜": "不吃香菜",
}


# ============ 项目画像表(风险评估用) ============

# 一个活动项目一条记录: 关键词 -> (强度1-5, 是否室外, 典型时段)
#
# 存在理由: 这三件事过去是三张**平行**的硬编码表(室外表 / 强度表 / 文案里的
# 时段假设), 于是漂移过一次 —— 高尔夫在室外表里、强度表里没有, 结果它算"雨天
# 要备室内", 却对"全队平均强度"贡献 0, 把平均拉低、把尖峰抹平。合成一张表后,
# "每个室外的项目都有强度"变成结构上不可能违反的事, 不再靠人记得同步两张表。
#
# 时段(slot)的用处只有一个: 夜间降雨只对傍晚/通宵的项目算命中。否则"白天晴、
# 夜间小雨"配上白天的户外项目就是误报 —— 和"出发地的雨不能说目的地的室外项目"
# 是同一类错误的两个面。
#
# 匹配口径: 统一小写后做**子串**匹配, 按本表顺序取首个命中(具体词排在泛词前面)。
# 未命中的项目**不计入**强度统计, 但会被列进"未识别"清单交审批人看 ——
# 解析不出不妄下结论, 但也不假装它不存在。
#
# 强度标尺与 PersonPref.fitness_level 一致(1=最轻, 5=最重)。
PROJECT_PROFILES: tuple = (
    # 高强度户外(5)
    ("爬山", 5, True, "day"), ("登山", 5, True, "day"),
    ("攀岩", 5, True, "day"), ("溯溪", 5, True, "day"),
    # 中高强度户外(4)
    ("徒步", 4, True, "day"), ("拓展", 4, True, "day"),
    ("定向", 4, True, "day"), ("越野", 4, True, "day"),
    ("骑行", 4, True, "day"), ("漂流", 4, True, "day"),
    ("滑雪", 4, True, "day"),
    # 中等户外(3)
    ("皮划艇", 3, True, "day"), ("划船", 3, True, "day"),
    ("水上", 3, True, "day"), ("飞盘", 3, True, "day"),
    ("足球", 3, True, "day"), ("篮球", 3, True, "day"),
    ("棒球", 3, True, "day"), ("高尔夫", 3, True, "day"),
    ("真人cs", 3, True, "day"), ("镭战", 3, True, "day"),
    # 低强度户外(2): 均为傍晚/通宵项目 -> 夜雨也算命中
    ("露营", 2, True, "evening"), ("烧烤", 2, True, "evening"),
    ("野餐", 2, True, "evening"), ("篝火", 2, True, "evening"),
    ("古镇", 2, True, "day"),
    # 室内(雨天不受影响, 强度照算)
    ("密室", 2, False, "any"), ("剧本杀", 1, False, "any"),
    ("桌游", 1, False, "any"), ("聚餐", 1, False, "any"),
    ("ktv", 1, False, "any"), ("温泉", 1, False, "any"),
)

# 降水关键词(高德 day_weather/night_weather: 小雨/中雨/雷阵雨/小雪…)
PRECIP_KEYWORDS: tuple = ("雨", "雪")

# 极端天气关键词: 与降水分开 —— 这类**不要求**流程里有室外项目。
# 台风天在室内办活动同样要重问整个行程(通勤/返程), 拿"室内活动不受影响"放行
# 是错的。降水那一档才需要"有室外项目"这个前提。
EXTREME_WEATHER_KEYWORDS: tuple = (
    "台风", "暴雨", "雷暴", "雷电", "冰雹", "大雾", "雾霾", "沙尘", "暴雪", "寒潮",
)


class ProjectProfile(NamedTuple):
    """一个活动项目的画像(强度 / 是否室外 / 时段)"""
    keyword: str
    intensity: int
    outdoor: bool
    slot: str          # "day" | "evening" | "any"


def resolve_project(text: str) -> Optional[ProjectProfile]:
    """从项目文案里解析画像(首个命中优先); 解析不出返回 None

    不调 LLM、不猜: 命中不了就让调用方把这一项列进"未识别", 而不是给个
    default 强度混进统计里 —— 默认值会把平均往中间拽, 正好抹掉尖峰。
    """
    low = (text or "").lower()
    if not low:
        return None
    for keyword, intensity, outdoor, slot in PROJECT_PROFILES:
        if keyword in low:
            return ProjectProfile(keyword, intensity, outdoor, slot)
    return None


def weather_hits(keywords: Sequence[str], *texts: str) -> List[str]:
    """在若干天气字段里找出命中的关键词(原样返回命中的词, 供证据展示)"""
    joined = " ".join(t or "" for t in texts)
    return [k for k in keywords if k in joined]


def _fold(tags: Sequence[str], table: Dict[str, str]) -> List[str]:
    """逐个归一为规范词(保持首次出现的顺序, 同一规范词只留一次)

    去重很关键: 一个人同时勾了"爬山"和"登山", 折叠后会变成同一个词,
    若不去重就会把他的 headcount 计两遍, 票数凭空翻倍。
    """
    out: List[str] = []
    seen = set()
    for raw in tags or []:
        key = str(raw).strip()
        if not key:
            continue
        canonical = table.get(key.lower(), key)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def fold_activities(tags: Sequence[str]) -> List[str]:
    return _fold(tags, ACTIVITY_SYNONYMS)


def fold_dietary(tags: Sequence[str]) -> List[str]:
    return _fold(tags, DIETARY_SYNONYMS)


def canonical_activity_labels() -> List[str]:
    """规范活动词全集(去重保序)

    前端 PREF_SUGGESTIONS 应保持为本集合的子集 —— 让"下拉里能选的"一定能被
    归一, 否则用户从模板里选的活动会被当成自定义词另立一个桶。
    """
    out: List[str] = []
    seen = set()
    for canonical in ACTIVITY_SYNONYMS.values():
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out