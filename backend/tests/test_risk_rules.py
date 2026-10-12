"""风险规则测试: 跨日活动 / 预报覆盖不全 / 强度尖峰 / 严重度与等级 / 词表完整性

覆盖这一轮改造里几条**会改变结论**的口径:
- 多日活动逐日匹配(旧口径只比活动起始日, 第二天的雨静默漏报)
- 预报覆盖不全 -> 规则记为"未评估"而不是"无事", 覆盖率如实写进结论
- 夜间降水只对傍晚/通宵项目算命中(白天晴+白天户外项目不该报)
- 强度按"单点达标 + 有人吃不消", 不再被全队平均抹平; 样本量如实标注
- 严重度由规则自己定, 等级取最大值而不是数条数; 全部规则未评估=unknown
- 备选按标记 id 对齐: 覆盖不到的丢弃、没被覆盖的标记显式列出
- 词表完整性: 项目画像表一份定义三角(强度/室内外/时段), 不再有平行表漂移

运行:
    cd backend && py -3.13 -m unittest discover -s tests -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from app.graph import lexicon
from app.graph.nodes import (
    RULE_EXTREME, RULE_INTENSITY, RULE_TEMP_WIND, RULE_WEATHER,
    _align_alternatives, _detect_extreme_weather, _detect_intensity_conflict,
    _detect_weather_risk, _level_of, _weather_coverage_note, assess_rules,
    reassess_risk, unresolved_flow_items,
)
from app.models.schemas import (
    RiskAlternativeDraft, RiskFlag, RuleStatus,
)


def _wx(date, day="晴", night="晴"):
    """一条预报(只带判定用到的字段)"""
    return {"date": date, "day_weather": day, "night_weather": night, "day_temp": 20}


def _basic(*entries):
    return {"weather": list(entries)}


def _flow(*names):
    return {"items": [{"name": n, "note": ""} for n in names]}


def _status(rule, status):
    return RuleStatus(rule=rule, name=rule, status=status)


class TestWeatherDayCoverage(unittest.TestCase):
    """规则1: 降水 + 当天室外项目(逐日、分时段、按覆盖率)"""

    def test_rain_on_second_day_hits_second_day_item(self):
        basic = _basic(_wx("2026-10-18", day="晴"), _wx("2026-10-19", day="小雨"))
        parsed = {"date": "2026-10-18", "date_end": "2026-10-19"}
        flag, status = _detect_weather_risk(basic, parsed, _flow("D2 爬山"))
        self.assertIsNotNone(flag, "第二天的雨落在第二天的室外项目上, 必须标记")
        self.assertEqual(flag.day, "2026-10-19")
        self.assertEqual(status.status, "hit")
        self.assertEqual(flag.severity, "high")

    def test_rain_on_second_day_does_not_hit_first_day_item(self):
        """项目标了天次时按天匹配: 第二天下雨不该让第一天的项目背锅"""
        basic = _basic(_wx("2026-10-18", day="晴"), _wx("2026-10-19", day="小雨"))
        parsed = {"date": "2026-10-18", "date_end": "2026-10-19"}
        flag, status = _detect_weather_risk(basic, parsed, _flow("D1 爬山"))
        self.assertIsNone(flag)
        self.assertEqual(status.status, "clear")

    def test_item_without_day_is_conservative_and_says_so(self):
        basic = _basic(_wx("2026-10-19", day="小雨"))
        parsed = {"date": "2026-10-18", "date_end": "2026-10-19"}
        flag, _ = _detect_weather_risk(basic, parsed, _flow("爬山"))
        self.assertIsNotNone(flag)
        self.assertTrue(flag.evidence["conservative"])
        self.assertIn("保守判断", flag.detail)

    def test_night_rain_only_hits_evening_items(self):
        """白天晴、夜间小雨: 傍晚项目要报, 白天户外项目不该报"""
        basic = _basic(_wx("2026-10-18", day="晴", night="小雨"))
        parsed = {"date": "2026-10-18"}
        day_flag, _ = _detect_weather_risk(basic, parsed, _flow("爬山"))
        eve_flag, _ = _detect_weather_risk(basic, parsed, _flow("烧烤"))
        self.assertIsNone(day_flag, "夜雨不影响白天的爬山")
        self.assertIsNotNone(eve_flag, "夜雨影响傍晚的烧烤")
        self.assertIn("夜间", eve_flag.detail)

    def test_no_outdoor_item_is_clear_not_hit(self):
        basic = _basic(_wx("2026-10-18", day="小雨"))
        flag, status = _detect_weather_risk(basic, {"date": "2026-10-18"}, _flow("剧本杀", "聚餐"))
        self.assertIsNone(flag, "室内活动不受降水影响")
        self.assertEqual(status.status, "clear")

    def test_partial_coverage_reports_missed_day(self):
        """第一天在预报范围内且晴, 第二天超出范围 -> 清白但要说清哪几天没评估"""
        basic = _basic(_wx("2026-10-18", day="晴"))
        parsed = {"date": "2026-10-18", "date_end": "2026-10-19"}
        flag, status = _detect_weather_risk(basic, parsed, _flow("爬山"))
        self.assertIsNone(flag)
        self.assertEqual(status.status, "clear")
        self.assertIn("2026-10-19", status.reason)
        self.assertIn("2026-10-19", _weather_coverage_note(basic, parsed))

    def test_activity_days_all_out_of_range_is_not_evaluated(self):
        basic = _basic(_wx("2026-10-08", day="小雨"))
        parsed = {"date": "2026-11-20"}
        flag, status = _detect_weather_risk(basic, parsed, _flow("爬山"))
        self.assertIsNone(flag)
        self.assertEqual(status.status, "not_evaluated",
                         "活动日超出预报范围是'没评估', 不是'无风险'")

    def test_missing_weather_is_not_evaluated(self):
        flag, status = _detect_weather_risk({}, {"date": "2026-10-18"}, _flow("爬山"))
        self.assertIsNone(flag)
        self.assertEqual(status.status, "not_evaluated")

    def test_coverage_note_mentions_full_coverage_gap(self):
        basic = _basic(_wx("2026-10-08", day="晴"))
        note = _weather_coverage_note(basic, {"date": "2026-11-20"})
        self.assertIn("无法评估", note)


class TestExtremeWeather(unittest.TestCase):
    """规则2: 极端天气不要求室外项目(台风天在室内办也得重问行程)"""

    def test_extreme_weather_flags_indoor_only_flow(self):
        basic = _basic(_wx("2026-10-18", day="台风"))
        flag, status = _detect_extreme_weather(basic, {"date": "2026-10-18"})
        self.assertIsNotNone(flag, "室内流程不构成放行理由")
        self.assertEqual(status.status, "hit")
        self.assertEqual(flag.severity, "high")

    def test_plain_rain_is_not_extreme(self):
        basic = _basic(_wx("2026-10-18", day="小雨"))
        flag, status = _detect_extreme_weather(basic, {"date": "2026-10-18"})
        self.assertIsNone(flag, "普通降水归降水那条规则, 不算极端天气")
        self.assertEqual(status.status, "clear")


class TestIntensityRule(unittest.TestCase):
    """规则3: 单点达标 + 有人吃不消(不看全队平均), 分母如实标注"""

    def test_spike_not_diluted_by_average(self):
        """五项里只有一项爬山、其余聚餐: 旧口径平均 1.8 不标记, 现在必须标记"""
        flow = _flow("爬山", "聚餐", "聚餐", "聚餐", "聚餐")
        flag, status = _detect_intensity_conflict(
            flow, {"fitness_distribution": {"2": 3, "4": 10}})
        self.assertIsNotNone(flag, "平均值不该把单点高危抹平")
        self.assertEqual(status.status, "hit")
        self.assertIn("爬山(5级)", flag.detail)
        self.assertIn("23%", flag.detail)
        # 结论文案里不放"平均"这个数: 无论算全队还是只算高强度项, 摆在最前面都会被
        # 当成"团队整体强度"读 —— 而平均正是把这条尖峰抹掉的那个量
        self.assertNotIn("平均", flag.detail)
        self.assertEqual(flag.evidence["high_item_avg"], 5.0)
        self.assertEqual(flag.severity, "medium")     # 弱体能 3/13 = 23% < 25%

    def test_severity_high_when_weak_ratio_high(self):
        flag, _ = _detect_intensity_conflict(
            _flow("爬山"), {"fitness_distribution": {"1": 3, "5": 5}})
        self.assertEqual(flag.severity, "high")       # 3/8 = 37.5%

    def test_clear_when_nobody_is_weak(self):
        flag, status = _detect_intensity_conflict(
            _flow("爬山"), {"fitness_distribution": {"4": 5}})
        self.assertIsNone(flag)
        self.assertEqual(status.status, "clear")

    def test_not_evaluated_when_fitness_missing(self):
        flag, status = _detect_intensity_conflict(_flow("爬山"), {})
        self.assertIsNone(flag)
        self.assertEqual(status.status, "not_evaluated", "没人填体能是'没评估', 不是'没事'")

    def test_sample_size_is_stated(self):
        """30 人只有 5 人填体能: 占比是 5/5=100%, 但必须把样本量说出来"""
        flag, _ = _detect_intensity_conflict(
            _flow("爬山"), {"fitness_distribution": {"2": 5}, "people_count": 30})
        self.assertIn("100%", flag.detail)
        self.assertIn("仅 5/30 人填写", flag.detail)


class TestUnresolvedItems(unittest.TestCase):
    """解析不出强弱的项目要列出来, 而不是静默不计入"""

    def test_unresolved_items_listed(self):
        names = unresolved_flow_items(_flow("旱地冰壶", "自由活动", "爬山"))
        self.assertEqual(sorted(names), ["旱地冰壶", "自由活动"])

    def test_assess_rules_carries_unresolved(self):
        _flags, _rules, unresolved = assess_rules({}, {}, {"flow": _flow("旱地冰壶")}, {})
        self.assertEqual(unresolved, ["旱地冰壶"])


class TestLevelAndRuleStatus(unittest.TestCase):
    """等级只表示方案风险: 按严重度取最大值, 全部未评估=unknown"""

    def test_level_by_severity_not_by_count(self):
        m1 = RiskFlag(id="a", rule=RULE_INTENSITY, severity="medium", detail="")
        m2 = RiskFlag(id="b", rule=RULE_INTENSITY, severity="medium", detail="")
        h = RiskFlag(id="c", rule=RULE_WEATHER, severity="high", detail="")
        self.assertEqual(_level_of([m1, m2], []), "medium",
                         "两条中风险仍是 medium, 不该按条数升成 high")
        self.assertEqual(_level_of([m1, h], []), "high")

    def test_level_unknown_only_when_everything_not_evaluated(self):
        self.assertEqual(_level_of([], [_status("a", "not_evaluated")]), "unknown")
        self.assertEqual(_level_of([], [_status("a", "clear"), _status("b", "not_evaluated")]),
                         "low")

    def test_all_not_evaluated_end_to_end(self):
        """没有天气采集、没人填体能 -> 等级是 unknown, 不是绿色的 low"""
        flags, rules, _ = assess_rules({}, {"date": "2026-10-18"},
                                       {"flow": _flow("爬山")}, {})
        self.assertEqual(flags, [])
        self.assertEqual(_level_of(flags, rules), "unknown")

    def test_temperature_rule_always_reported(self):
        """温度/风力本期不做: 显式记一条未评估, 而不是干脆不写"""
        _flags, rules, _ = assess_rules({}, {}, {"flow": _flow("聚餐")}, {})
        temp = [r for r in rules if r.rule == RULE_TEMP_WIND]
        self.assertEqual(len(temp), 1)
        self.assertEqual(temp[0].status, "not_evaluated")
        self.assertTrue(temp[0].reason)

    def test_every_rule_reports_status(self):
        """每条规则都必须表态 —— 这正是"没查"和"没事"分开的地方"""
        _flags, rules, _ = assess_rules(_basic(_wx("2026-10-18", day="晴")),
                                        {"date": "2026-10-18"},
                                        {"flow": _flow("聚餐")},
                                        {"fitness_distribution": {"3": 5}})
        self.assertEqual({r.rule for r in rules},
                         {RULE_WEATHER, RULE_EXTREME, RULE_INTENSITY, RULE_TEMP_WIND})
        self.assertTrue(all(r.status in ("hit", "clear", "not_evaluated") for r in rules))


class TestAlternativeAlignment(unittest.TestCase):
    """备选按标记 id 对齐: 覆盖不到任何标记的丢弃, 没被覆盖的标记显式列出"""

    def _flag(self, fid, rule=RULE_WEATHER):
        return RiskFlag(id=fid, rule=rule, severity="high", detail="")

    def test_orphan_alternative_dropped(self):
        flags = [self._flag("weather_outdoor:2026-10-18")]
        drafts = [RiskAlternativeDraft(covers=["weather_outdoor:2026-10-18"], plan="A"),
                  RiskAlternativeDraft(covers=["intensity_fitness:-"], plan="B")]
        kept, uncovered = _align_alternatives(drafts, flags)
        self.assertEqual([a.plan for a in kept], ["A"], "自造/写错的 id 不该变成一条备选")
        self.assertEqual(uncovered, [])
        self.assertTrue(kept[0].est_cost_is_estimate, "费用是模型估算: 这个事实由程序填")
        self.assertFalse(kept[0].counted_in_total, "备选一律未计入合计")

    def test_uncovered_flag_is_listed(self):
        flags = [self._flag("weather_outdoor:2026-10-18"),
                 self._flag("extreme_weather:2026-10-19", RULE_EXTREME)]
        drafts = [RiskAlternativeDraft(covers=["weather_outdoor:2026-10-18"], plan="A")]
        _kept, uncovered = _align_alternatives(drafts, flags)
        self.assertEqual(uncovered, ["extreme_weather:2026-10-19"],
                         "没被覆盖的标记必须说出来, 不能只报'已给出1条备选'")

    def test_one_alternative_can_cover_several_flags(self):
        flags = [self._flag("weather_outdoor:2026-10-18"),
                 self._flag("extreme_weather:2026-10-18", RULE_EXTREME)]
        drafts = [RiskAlternativeDraft(covers=[f.id for f in flags], plan="改期")]
        kept, uncovered = _align_alternatives(drafts, flags)
        self.assertEqual(len(kept[0].covers), 2)
        self.assertEqual(uncovered, [])


class TestReassess(unittest.TestCase):
    """交付前重算: 结论按最终方案现算, 旧备选能沿用就不重复花钱"""

    PREV = {"alternatives": [{"covers": ["weather_outdoor:2026-10-18"], "plan": "旧备选",
                              "est_cost": 100, "est_cost_is_estimate": True,
                              "counted_in_total": False}]}

    def test_matching_alternative_reused(self):
        out = reassess_risk(self.PREV, _basic(_wx("2026-10-18", day="小雨")),
                            {"date": "2026-10-18"}, {"flow": _flow("爬山")}, {})
        self.assertEqual([a["plan"] for a in out["alternatives"]], ["旧备选"])
        self.assertEqual(out["level"], "high")

    def test_stale_alternative_dropped_when_flags_gone(self):
        out = reassess_risk(self.PREV, _basic(_wx("2026-10-18", day="晴")),
                            {"date": "2026-10-18"}, {"flow": _flow("爬山")}, {})
        self.assertEqual(out["alternatives"], [])
        self.assertEqual(out["flags"], [])
        self.assertEqual(out["level"], "low")


class TestProjectProfileTable(unittest.TestCase):
    """词表完整性: 一份定义三角, 不再有"室外表有、强度表没有"的漂移"""

    def test_no_duplicate_keywords(self):
        kws = [k for k, _i, _o, _s in lexicon.PROJECT_PROFILES]
        self.assertEqual(len(kws), len(set(kws)), "重复关键词会让后一条永远匹配不到")

    def test_keywords_are_lowercase(self):
        """匹配口径是"统一小写后子串匹配", 关键词必须是小写"""
        for kw, _i, _o, _s in lexicon.PROJECT_PROFILES:
            self.assertEqual(kw, kw.lower(), f"{kw} 不是小写, 永远匹配不到")

    def test_every_profile_field_is_valid(self):
        for kw, intensity, outdoor, slot in lexicon.PROJECT_PROFILES:
            self.assertIn(intensity, (1, 2, 3, 4, 5), kw)
            self.assertIsInstance(outdoor, bool, kw)
            self.assertIn(slot, ("day", "evening", "any"), kw)

    def test_golf_has_intensity_regression(self):
        """曾经的漂移: 高尔夫在室外表里、强度表里没有 -> 对平均强度贡献 0"""
        p = lexicon.resolve_project("高尔夫球场")
        self.assertIsNotNone(p)
        self.assertTrue(p.outdoor)
        self.assertGreaterEqual(p.intensity, 1)

    def test_outdoor_projects_are_resolvable(self):
        for kw, _i, outdoor, _s in lexicon.PROJECT_PROFILES:
            profile = lexicon.resolve_project(f"{kw} 场地")
            self.assertIsNotNone(profile, kw)
            self.assertEqual(profile.outdoor, outdoor, kw)

    def test_resolve_prefers_first_match(self):
        """具体词排在泛词前面: '皮划艇' 不该被 '水上' 提前截胡(两者同级, 但要稳定)"""
        p = lexicon.resolve_project("皮划艇体验")
        self.assertEqual(p.keyword, "皮划艇")

    def test_unknown_project_returns_none(self):
        self.assertIsNone(lexicon.resolve_project("旱地冰壶"))


class TestDeliveredRiskText(unittest.TestCase):
    """交付文本里的风险结论: 用户唯一会读到的就是这一行, 它不能说反

    之前这里的兜底文案写死成"低风险, 无需备选" —— 于是"没有天气数据"和
    "天气晴、体能达标"在交付文档里长得完全一样。这里钉住 unknown 与 low 的分别。
    """

    def _plan_text(self, final_state) -> str:
        from app.api.routes.event import _event_plan_from_state

        return _event_plan_from_state(final_state).risk_report

    def _state(self, flow=_flow("爬山"), basic=None, pref=None, prev_risk=None):
        return {
            "proposals": {"flow": {"category": "flow",
                                   "items": [{"name": i["name"], "cost": 0} for i in flow["items"]]}},
            "basic_info": basic or {},
            "parsed_request": {"date": "2026-10-18"},
            "pref_summary": pref,
            "risk_report": prev_risk or {},
            "conflict_report": {},
        }

    def test_unknown_is_not_written_as_low(self):
        """缺天气 + 没人填体能 -> 交付文本必须是"未评估", 不能是"低风险" """
        text = self._plan_text(self._state())
        self.assertIn("未评估", text)
        self.assertNotIn("低风险", text)
        self.assertIn("未能判定", text)

    def test_low_is_still_low(self):
        """规则都跑过且清白 -> 仍然是低风险(别把未知的修法变成谁都不说低风险)"""
        text = self._plan_text(self._state(
            basic=_basic(_wx("2026-10-18", day="晴")),
            pref={"fitness_distribution": {"4": 5}},
        ))
        self.assertIn("低风险", text)
        self.assertNotIn("未评估): 未能判定", text)

    def test_flags_and_estimate_caveat_are_delivered(self):
        text = self._plan_text(self._state(
            basic=_basic(_wx("2026-10-18", day="小雨")),
            prev_risk={"alternatives": [{"covers": ["weather_outdoor:2026-10-18"],
                                         "plan": "改期到周日", "est_cost": 200,
                                         "est_cost_is_estimate": True,
                                         "counted_in_total": False}]},
        ))
        self.assertIn("高风险", text)
        self.assertIn("小雨", text)
        self.assertIn("改期到周日", text)
        self.assertIn("模型估算", text)     # 别把模型估的数读成已定的价
        self.assertIn("未计入合计", text)

    def test_unresolved_items_are_delivered(self):
        """识别不出强弱的项目要在交付物里说出来, 而不是静默当它不存在"""
        text = self._plan_text(self._state(flow=_flow("旱地冰壶")))
        self.assertIn("旱地冰壶", text)
        self.assertIn("未纳入判定", text)


if __name__ == "__main__":
    unittest.main()