# -*- coding: utf-8 -*-
"""难度组（hard suite）测试：真值必须能独立算、判定不能误杀。

这一组存在的理由：原来的 204 例是**抽取式**任务 —— 工具把答案算好了，
模型只是搬运，所以 936 次调用 0 次答错。要测到模型，必须让工具只给原料。

跑法（在 EvoGuardLab 目录下）：
    python -m unittest tests.test_hard_tasks -v
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import agents  # noqa: E402
import sim     # noqa: E402


class TestCalcTruth(unittest.TestCase):
    """多步计算的真值必须能脱离工具手算出来（否则判分没有依据）。"""

    def test_order_h3001_hand_computed(self):
        # 1580*2 + 860 = 4020 >= 1000 → 减 100 → 3920 × 0.95 = 3724
        t = sim.expected_hard_total("H3001")
        self.assertEqual(t["subtotal"], 4020.0)
        self.assertEqual(t["after_rebate"], 3920.0)
        self.assertEqual(t["total"], 3724.0)

    def test_threshold_not_met_skips_rebate(self):
        # 620+130 = 750 < 1000 → 不减；tier=none → 不打折 → 750
        t = sim.expected_hard_total("H3003")
        self.assertEqual(t["after_rebate"], 750.0)
        self.assertEqual(t["total"], 750.0)

    def test_rebate_then_tier_order_matters(self):
        """顺序敏感：先折扣后满减会得到不同的数，这正是考点。"""
        t = sim.expected_hard_total("H3002")
        wrong_order = round((2400 + 45.5 * 10) * 0.9 - 200, 2)   # 先打折再满减
        self.assertEqual(t["total"], 2389.5)
        self.assertNotEqual(t["total"], wrong_order,
                            "如果两种顺序结果相同，这道题就没有考点")


class TestXrefTruthAndJudge(unittest.TestCase):
    def test_truth_is_highest_version(self):
        for topic, ans in sim.HARD_KB_ANSWER.items():
            docs = sim.ALL_HARD_KB[topic]
            top = max(docs, key=lambda d: d["version"])
            self.assertEqual(ans["doc"], top["doc_id"],
                             "%s 的真值必须取版本号最高的文档" % topic)
            self.assertIn(ans["must"], top["text"])

    def test_all_versions_have_conflicting_answers(self):
        """每个主题的三个版本必须给出不同结论，否则「比版本」这个考点不存在。"""
        pat = r"\d+\s*(?:个工作日|个?天|个月|元)"
        for topic, docs in sim.ALL_HARD_KB.items():
            found = {m.group(0) for m in (sim.re.search(pat, d["text"]) for d in docs) if m}
            self.assertGreater(len(found), 1,
                               "%s 的三个版本答案相同，比版本号没有意义" % topic)

    def test_cited_with_version_suffix_counts(self):
        """模型把版本号一起写进 cited（"KB-25 v5"）不该被判错。

        这个 bug 真踩过：第一条 xref 基线里，两条本来做对的被精确匹配判成错。
        """
        ans = {"text": "整机保修 24 个月，电池保修 12 个月。", "cited": ["KB-25 v5"]}
        self.assertTrue(agents.is_correct("xref", ans, sim.expected_answer("xref", "保修期限")))

    def test_wrong_version_still_fails(self):
        ans = {"text": "自签收之日起 15 个工作日内可无理由退货。", "cited": ["KB-23 v1"]}
        self.assertFalse(agents.is_correct("xref", ans, sim.expected_answer("xref", "退货期限")))

    def test_right_text_wrong_citation_fails(self):
        """条款对但引用错文档 = 没真正比版本，不算会做。"""
        ans = {"text": "自签收之日起 10 个工作日内可无理由退货。", "cited": ["KB-21"]}
        self.assertFalse(agents.is_correct("xref", ans, sim.expected_answer("xref", "退货期限")))


class TestApplyRules(unittest.TestCase):
    """算术交给工具、顺序交给模型 —— 这个分工是测出来的，不是拍出来的。

    第一版让模型自己算，基线 33.3%；加 prompt 约束无效（H3001 从"算错"退化成"抄小计"）。
    改成这个分工后基线 83.3%。
    """

    ARGS = {"subtotal": 4020.0, "rebate": {"threshold": 1000.0, "amount": 100.0},
            "tier_rate": 0.95}

    def _run(self, order):
        return sim.call_tool("calc.apply_rules", dict(self.ARGS, order=order))

    def test_rebate_then_discount_is_correct(self):
        self.assertEqual(self._run(["rebate", "discount"])["total"], 3724.0)

    def test_order_changes_result(self):
        """顺序不同结果必须不同，否则「模型决定顺序」这个考点不存在。"""
        self.assertNotEqual(self._run(["rebate", "discount"])["total"],
                            self._run(["discount", "rebate"])["total"])

    def test_threshold_checked_on_current_amount(self):
        """先打折会让金额变小，可能就不满足门槛了 —— 这是顺序带来的真实差异。"""
        out = self._run(["discount", "rebate"])
        self.assertEqual(out["applied"], ["discount", "rebate"])
        # 4020*0.95=3819 仍 >= 1000，所以还是减了 100
        self.assertEqual(out["total"], 3719.0)

    def test_empty_order_rejected(self):
        with self.assertRaises(ValueError):
            self._run([])

    def test_unknown_rule_rejected(self):
        with self.assertRaises(ValueError):
            self._run(["rebate", "refund"])

    def test_below_threshold_skips_rebate(self):
        args = {"subtotal": 750.0, "rebate": {"threshold": 1000.0, "amount": 100.0},
                "tier_rate": 1.0, "order": ["rebate", "discount"]}
        out = sim.call_tool("calc.apply_rules", args)
        self.assertEqual(out["applied"], ["discount"], "没到门槛不该减")
        self.assertEqual(out["total"], 750.0)


class TestHardDataset(unittest.TestCase):
    def test_cases_include_no_fault_baseline(self):
        """每个任务必须有 fault_type=none 的纯能力基线。

        没有它就没法区分「题目太难模型不会」和「恢复机制坏了」。
        """
        cases = sim.hard_cases()
        none_cases = [c for c in cases if c["fault_type"] == "none"]
        self.assertEqual(len(none_cases), len(sim.HARD_ORDERS) + len(sim.HARD_KB))

    def test_hard_cases_do_not_touch_training_set(self):
        """calc / xref 不能出现在 AGENT_TYPES 里。

        它们进 AGENT_TYPES 会让 204 例的数据集统计和训练集划分一起变，
        历史数字全部作废。要学技能，用 hard_train_tasks 单独喂。
        """
        for a in sim.HARD_AGENTS:
            self.assertNotIn(a, sim.AGENT_TYPES,
                             "难度组混进 AGENT_TYPES 会让训练集/技能库跟着变，历史数字全部作废")

    def test_train_tasks_disjoint_from_eval_tasks(self):
        """训练任务与评测任务绝不能重叠 —— 否则技能会「记住评测题的答案」。"""
        for at in sim.HARD_AGENTS:
            eval_ids = {t["task_id"] for t in sim.hard_tasks(at)}
            train_ids = {t["task_id"] for t in sim.hard_train_tasks(at)}
            self.assertFalse(eval_ids & train_ids,
                             "%s 的训练任务和评测任务重叠：%s" % (at, eval_ids & train_ids))

    def test_train_tasks_are_reachable_by_tools(self):
        """训练任务必须能被工具查到。

        踩过：工具只查了评测表（HARD_ORDERS），训练用的 T4001 报「订单不存在」，
        探索阶段全部失败，训练静默产出 0 条 calc 技能 —— 表面看是"学不会"，
        实际是数据层没打通。所以这条必须钉住。
        """
        for tid in sim.HARD_TRAIN_ORDERS:
            out = sim.call_tool("order.get_quote_order", {"order_id": tid})
            self.assertEqual(out["order_id"], tid)
        for topic in sim.HARD_TRAIN_KB:
            out = sim.call_tool("kb.search_all", {"topic": topic})
            self.assertTrue(out["docs"], "%s 查不到文档" % topic)

    def test_every_topic_has_nonempty_truth(self):
        """真值抽取不能为空。

        踩过：正则只认「工作日/天/个月」，遇到「满 129 元包邮」抽出来是空字符串，
        于是无论模型答什么都判错 —— 一个静默的判分器 bug。
        """
        for topic, ans in sim.HARD_KB_ANSWER.items():
            self.assertTrue(ans["must"], "%s 的真值为空，正则没覆盖这种文案" % topic)


if __name__ == "__main__":
    unittest.main()
