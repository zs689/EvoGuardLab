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
            docs = sim.HARD_KB[topic]
            top = max(docs, key=lambda d: d["version"])
            self.assertEqual(ans["doc"], top["doc_id"],
                             "%s 的真值必须取版本号最高的文档" % topic)
            self.assertIn(ans["must"], top["text"])

    def test_all_versions_have_conflicting_answers(self):
        """每个主题的三个版本必须给出不同结论，否则「比版本」这个考点不存在。"""
        for topic, docs in sim.HARD_KB.items():
            found = {sim.re.search(r"\d+\s*个?(?:工作日|天|个月)", d["text"]).group(0)
                     for d in docs}
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


class TestHardDataset(unittest.TestCase):
    def test_cases_include_no_fault_baseline(self):
        """每个任务必须有 fault_type=none 的纯能力基线。

        没有它就没法区分「题目太难模型不会」和「恢复机制坏了」。
        """
        cases = sim.hard_cases()
        none_cases = [c for c in cases if c["fault_type"] == "none"]
        self.assertEqual(len(none_cases), len(sim.HARD_ORDERS) + len(sim.HARD_KB))

    def test_hard_cases_do_not_touch_training_set(self):
        """难度组不进训练集：AGENT_TYPES 里不能出现 calc / xref。"""
        for a in sim.HARD_AGENTS:
            self.assertNotIn(a, sim.AGENT_TYPES,
                             "难度组混进 AGENT_TYPES 会让训练集/技能库跟着变，历史数字全部作废")


if __name__ == "__main__":
    unittest.main()
