"""偏好聚合测试: 计数口径 / 同义归一 / 规则提示 / 不写死默认值

覆盖这一轮改造的六件事:
- headcount **声明**计数(不再用 len(prefs)==1 猜"是不是全员汇总行")
- dietary_counts 让忌口带人数; 只填忌口的成员能被算进去
- 活动同义归一(爬山/登山/徒步 合并, 且同一人不重复计数)
- 偏好提示改纯规则生成: 零 LLM、确定、可重复
- 体能默认值不再静默进分布(旧默认 3 会稀释弱体能占比, 掩盖真冲突)
- budget_median / budget_willingness 已删除

运行:
    cd backend && py -3.13 -m unittest discover -s tests -v
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest import mock

from app.graph import nodes as nodes_module
from app.graph.lexicon import fold_activities, fold_dietary
from app.graph.nodes import _detect_intensity_conflict, aggregate_prefs
from app.models.schemas import PersonPref, PrefSummary


def _prefs(*rows) -> list:
    """把 (人数, 活动, 体能, 忌口) 四元组转成 PersonPref 列表"""
    out = []
    for i, (headcount, acts, fitness, dietary) in enumerate(rows):
        out.append({
            "person_name": f"成员{i + 1}",
            "headcount": headcount,
            "activity_prefs": acts,
            "fitness_level": fitness,
            "dietary": dietary,
        })
    return out


def _agg(preferences, people_count=30) -> PrefSummary:
    """跑一次聚合节点(preferences 传 dict, 走 _normalize_prefs 的容错分支)"""
    out = aggregate_prefs({
        "preferences": preferences,
        "parsed_request": {"people_count": people_count},
    })
    return out["pref_summary"]


class TestHeadcountDeclaration(unittest.TestCase):
    """计数口径由 headcount 声明, 不再靠行数猜"""

    def test_aggregate_row_scales_by_declared_headcount(self):
        """一条汇总行 headcount=30 -> 票数 30(这是旧逻辑也能做对的场景)"""
        s = _agg(_prefs((30, ["爬山"], None, [])))
        self.assertEqual(s.activity_votes, {"爬山": 30})
        self.assertEqual(s.stated_people, 30)

    def test_single_row_with_headcount_1_is_not_inflated(self):
        """30 人团建但只代填了 1 条成员偏好 -> 票数就是 1, 不该被放大成 30

        旧逻辑 len(prefs)==1 + people_count 放大, 会把 1 个人的意愿说成"30人想爬山"。
        """
        s = _agg(_prefs((1, ["爬山"], None, [])))
        self.assertEqual(s.activity_votes, {"爬山": 1})
        self.assertEqual(s.stated_people, 1)

    def test_mixed_rows_sum_declared_headcount(self):
        """1 条汇总行(代表5人) + 2 条逐人行 -> 票数 7, 而不是 3 也不是 15"""
        s = _agg(_prefs(
            (5, ["爬山"], None, []),
            (1, ["爬山"], None, []),
            (1, ["剧本杀"], None, []),
        ))
        self.assertEqual(s.activity_votes, {"爬山": 6, "剧本杀": 1})
        self.assertEqual(s.stated_people, 7)

    def test_unstated_is_the_gap_and_never_negative(self):
        """未表态 = people_count - stated; 且 people_count 缺省时回退到已表态人数"""
        s = _agg(_prefs((2, ["爬山"], None, [])), people_count=30)
        self.assertEqual((s.people_count, s.stated_people), (30, 2))

        out = aggregate_prefs({"preferences": _prefs((2, ["爬山"], None, []))})
        self.assertEqual(out["pref_summary"].people_count, 2)   # 无 parsed_request 时不崩

    def test_default_headcount_is_one(self):
        self.assertEqual(PersonPref(person_name="张三").headcount, 1)


class TestDietaryAndDecoupling(unittest.TestCase):
    """忌口带人数; 只填忌口的成员必须能被表达"""

    def test_dietary_counts_carry_headcount(self):
        s = _agg(_prefs(
            (3, [], None, ["素食"]),
            (1, [], None, ["清真"]),
        ))
        self.assertEqual(s.dietary_counts, {"素食": 3, "清真": 1})

    def test_dietary_all_is_derived_from_counts(self):
        """dietary_all 由 counts 派生, 两个字段不会漂移"""
        s = _agg(_prefs((1, [], None, ["清真"]), (1, [], None, ["素食"])))
        self.assertEqual(s.dietary_all, sorted(s.dietary_counts))

    def test_dietary_only_member_without_activity_is_counted(self):
        """只填忌口、没选活动的成员(素食且对玩什么没意见)也要进统计

        这是"必填从 姓名+活动 解耦成 姓名+活动/忌口至少一项"的服务端一侧:
        它的忌口必须进 dietary_all, 否则餐饮方案会漏掉这条约束。
        """
        s = _agg(_prefs(
            (1, ["爬山"], None, []),
            (1, [], None, ["素食"]),        # 无活动偏好
        ))
        self.assertIn("素食", s.dietary_all)
        self.assertEqual(s.dietary_counts["素食"], 1)
        self.assertEqual(s.stated_people, 2)


class TestActivityFolding(unittest.TestCase):
    """活动同义归一: 消除"爬山 vs 登山"这种本质是同一件事的假分歧"""

    def test_synonyms_fold_into_one_bucket(self):
        s = _agg(_prefs(
            (1, ["爬山"], None, []),
            (1, ["登山"], None, []),
            (1, ["徒步"], None, []),
        ))
        self.assertEqual(s.activity_votes, {"爬山": 3})

    def test_same_person_listing_synonyms_counts_once(self):
        """一个人同时勾"爬山"和"登山"只算一次, 票数不能凭空翻倍"""
        self.assertEqual(fold_activities(["爬山", "登山", "徒步"]), ["爬山"])
        s = _agg(_prefs((1, ["爬山", "登山", "徒步"], None, [])))
        self.assertEqual(s.activity_votes, {"爬山": 1})

    def test_unknown_activity_passes_through(self):
        """自定义活动(旱地冰壶)不能被吞掉——这是保留自由输入的前提"""
        s = _agg(_prefs((1, ["旱地冰壶"], None, [])))
        self.assertEqual(s.activity_votes, {"旱地冰壶": 1})

    def test_dietary_synonyms_fold(self):
        self.assertEqual(fold_dietary(["吃素", "素食"]), ["素食"])
        s = _agg(_prefs((2, [], None, ["吃素"]), (1, [], None, ["素食"])))
        self.assertEqual(s.dietary_counts, {"素食": 3})


class TestFrontendLexiconMirror(unittest.TestCase):
    """frontend/src/utils/lexicon.ts 是后端词表的镜像 —— 钉住不许漂移

    前端折一遍是为了让用户**当场看到**"登山→爬山", 不是为了正确性(后端一定会再折)。
    但两边规范词一旦不一致, 用户看到的标签和服务端统计的桶就会是两套, 而这件事
    在界面上完全看不出来。所以这里直接把两张表读出来逐条比对。
    """

    def _ts_tables(self) -> dict:
        from pathlib import Path
        import re

        ts = Path(__file__).resolve().parents[2] / "frontend" / "src" / "utils" / "lexicon.ts"
        if not ts.exists():
            self.skipTest(f"前端目录不存在(仅后端部署): {ts}")
        text = ts.read_text(encoding="utf-8")
        tables = {}
        for name in ("ACTIVITY_SYNONYMS", "DIETARY_SYNONYMS"):
            m = re.search(rf"const {name}\s*:[^{{]*{{(.*?)\n\}}", text, re.S)
            self.assertIsNotNone(m, f"没能在 {ts.name} 里解析出 {name}(格式变了就更新本测试)")
            pairs = re.findall(r"([A-Za-z0-9_一-鿿]+)\s*:\s*'([^']*)'", m.group(1))
            self.assertTrue(pairs, f"{name} 解析出 0 条, 解析器需要更新")
            tables[name] = dict(pairs)
        return tables

    def test_synonym_tables_match_backend(self):
        from app.graph.lexicon import ACTIVITY_SYNONYMS, DIETARY_SYNONYMS

        ts = self._ts_tables()
        for name, backend in (("ACTIVITY_SYNONYMS", ACTIVITY_SYNONYMS),
                              ("DIETARY_SYNONYMS", DIETARY_SYNONYMS)):
            with self.subTest(table=name):
                frontend = ts[name]
                only_be = {k: v for k, v in backend.items() if k not in frontend}
                only_fe = {k: v for k, v in frontend.items() if k not in backend}
                diff = {k: (backend[k], frontend[k])
                        for k in backend.keys() & frontend.keys() if backend[k] != frontend[k]}
                self.assertEqual(
                    (only_be, only_fe, diff), ({}, {}, {}),
                    f"{name} 前端镜像已漂移: 仅后端={only_be}, 仅前端={only_fe}, 规范词不一致={diff}")


class TestPrefNotesAreRulesNotLlm(unittest.TestCase):
    """偏好提示: 纯规则生成 —— 零 LLM、数字算出来、同一输入同一输出"""

    def _run(self, preferences, people_count=30) -> list:
        # 规则生成不该碰 LLM: 一碰就炸, 把"零 LLM"变成断言而不是承诺
        with mock.patch.object(nodes_module, "invoke_structured",
                               side_effect=AssertionError("聚合不该调 LLM")), \
             mock.patch.object(nodes_module, "get_chat_model",
                               side_effect=AssertionError("聚合不该建模型")):
            return _agg(preferences, people_count).conflict_points

    def test_no_llm_and_deterministic(self):
        """跑两遍逐字一致 —— 否决回炉重跑时审批页的提示不会变"""
        rows = _prefs((5, ["爬山"], 4, []), (5, ["剧本杀"], 1, ["素食"]))
        first = self._run(rows)
        self.assertEqual(first, self._run(rows))
        self.assertTrue(first, "5 vs 5 该报活动分歧")

    def test_activity_split_reports_counts(self):
        notes = self._run(_prefs((5, ["爬山"], None, []), (5, ["剧本杀"], None, [])))
        joined = " ".join(notes)
        self.assertIn("5人想爬山", joined)
        self.assertIn("5人想剧本杀", joined)

    def test_aggregate_row_wanting_both_is_not_a_conflict(self):
        """汇总行里 30 人都勾了爬山和剧本杀 -> 那是"都想安排", 不是人群对立"""
        notes = self._run(_prefs((30, ["爬山", "剧本杀"], None, [])))
        self.assertEqual([n for n in notes if " vs " in n], [])

    def test_dominant_preference_is_not_a_conflict(self):
        """25 vs 5 只是"主打", 够不上分歧(阈值 50%)"""
        notes = self._run(_prefs((25, ["爬山"], None, []), (5, ["剧本杀"], None, [])))
        self.assertEqual([n for n in notes if " vs " in n], [])

    def test_fitness_tier_note(self):
        """弱/强两端各占 25% 才提示分层"""
        notes = self._run(_prefs((5, [], 1, []), (5, [], 5, [])))
        self.assertTrue(any("体能分层" in n for n in notes), notes)

    def test_no_fitness_note_when_uniform(self):
        notes = self._run(_prefs((10, [], 3, [])))
        self.assertFalse(any("体能分层" in n for n in notes), notes)

    def test_dietary_is_framed_as_constraint_not_conflict(self):
        """忌口是约束不是分歧: 措辞要自明, 不能写成"3人素食 vs 27人不素食" """
        notes = self._run(_prefs((3, ["爬山"], None, ["素食"])))
        diet = [n for n in notes if "忌口" in n]
        self.assertEqual(len(diet), 1, notes)
        self.assertIn("3人素食", diet[0])
        self.assertNotIn(" vs ", diet[0])


class TestFitnessDefaultNotSilentlyCounted(unittest.TestCase):
    """体能默认值曾让未表态者按 3 级进分布, 稀释弱体能占比 -> 假阴性"""

    def test_unstated_fitness_excluded_from_distribution(self):
        s = _agg(_prefs(
            (5, ["爬山"], 2, []),
            (25, ["爬山"], None, []),      # 没填体能
        ))
        self.assertEqual(s.fitness_distribution, {"2": 5})

    def test_weak_ratio_not_diluted_so_conflict_is_reported(self):
        """5人明确体能2级 + 25人未填: 弱体能占比应是 100% 而不是 5/30=17%

        旧逻辑把 25 个未表态者按默认 3 级计入分母 -> 17% < 25% 阈值 -> 静默漏报。
        """
        s = _agg(_prefs(
            (5, ["爬山"], 2, []),
            (25, ["爬山"], None, []),
        ))
        flag, _status = _detect_intensity_conflict(
            {"items": [{"name": "爬山", "note": ""}]},
            {"fitness_distribution": s.fitness_distribution},
        )
        self.assertIsNotNone(flag, "弱体能占比被默认值稀释, 真冲突被掩盖了")
        self.assertIn("100%", flag.detail)


class TestHeadcountCeilingAtHttpLayer(unittest.TestCase):
    """声明人数不得超总人数: 前端拦不住 evals / 直连 API, 后端必须兜住"""

    def _body(self, **kw):
        from datetime import date, timedelta
        body = {
            "people_count": 3,
            "date": (date.today() + timedelta(days=30)).isoformat(),
            "origin": "上海",
        }
        body.update(kw)
        return body

    def test_declared_exceeding_people_count_rejected(self):
        from pydantic import ValidationError
        from app.api.routes.event import PlanStartBody
        with self.assertRaises(ValidationError) as ctx:
            PlanStartBody(**self._body(preferences=[
                {"person_name": "张三", "headcount": 5, "activity_prefs": ["爬山"]}]))
        self.assertIn("超过总人数", str(ctx.exception))

    def test_declared_equal_to_people_count_accepted(self):
        """正好等于总人数(全员都表态了)是合法的, 不该被误拦"""
        from app.api.routes.event import PlanStartBody
        body = PlanStartBody(**self._body(preferences=[
            {"person_name": "全员", "headcount": 3, "activity_prefs": ["爬山"]}]))
        self.assertEqual(body.preferences[0].headcount, 3)

    def test_declared_less_than_people_count_allowed_as_unstated(self):
        """少于总人数 = 有人没表态, 必须放行(不是错误)"""
        from app.api.routes.event import PlanStartBody
        body = PlanStartBody(**self._body(preferences=[
            {"person_name": "张三", "headcount": 1, "activity_prefs": ["爬山"]}]))
        self.assertEqual(len(body.preferences), 1)

    def test_zero_headcount_rejected(self):
        from pydantic import ValidationError
        from app.api.routes.event import PlanStartBody
        with self.assertRaises(ValidationError):
            PlanStartBody(**self._body(preferences=[
                {"person_name": "张三", "headcount": 0, "activity_prefs": ["爬山"]}]))


class TestRemovedFields(unittest.TestCase):
    """budget_median / budget_willingness 已删除: 口径随模式漂移且只写不读"""

    def test_pref_summary_has_no_budget_median(self):
        self.assertNotIn("budget_median", PrefSummary.model_fields)

    def test_person_pref_has_no_budget_willingness(self):
        self.assertNotIn("budget_willingness", PersonPref.model_fields)

    def test_unknown_field_is_ignored_by_pydantic(self):
        """前端/旧客户端仍传 budget_willingness 时降级为忽略, 不是 500"""
        self.assertEqual(PersonPref(person_name="张三", budget_willingness=100).person_name, "张三")


class TestGraphEntryHasNoPrefOnlyBypass(unittest.TestCase):
    """入口唯一: "仅偏好直达聚合"的死分支已删除"""

    def test_route_entry_removed(self):
        from app.graph import graph as graph_module
        self.assertFalse(hasattr(graph_module, "route_entry"))

    def test_missing_input_reports_error_instead_of_silent_bypass(self):
        """没有 raw_text/structured 时由 parse_request 明确报缺字段, 不静默跳过解析"""
        out = asyncio.run(nodes_module.parse_request(
            {"raw_request": {"preferences": []}},
            store=None,
        ))
        self.assertIn("missing_fields", out)
        self.assertEqual(out["missing_fields"], ["raw_text"])

    def test_graph_still_compiles(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from app.graph import build_event_graph
        build_event_graph(checkpointer=InMemorySaver())


if __name__ == "__main__":
    unittest.main(verbosity=2)