# -*- coding: utf-8 -*-
"""EVO-BENCH-01：基准自身的方法学测试。

统计代码算错会让「显著」变成假的，所以这些必须能独立验算。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import eval_env_invariant_bench as B                     # noqa: E402


class TestMcNemar(unittest.TestCase):
    def test_no_discordant_pairs_gives_p_one(self):
        self.assertEqual(B.mcnemar_exact(0, 0), 1.0)

    def test_symmetric_discordant_not_significant(self):
        # 5 vs 5 完全对称，不可能显著
        self.assertGreater(B.mcnemar_exact(5, 5), 0.9)

    def test_extreme_gain_is_significant(self):
        p = B.mcnemar_exact(10, 0)
        self.assertLess(p, 0.01)

    def test_known_exact_value(self):
        # b=10, c=0, n=10: 双侧 p = 2 * (1/2^10) = 0.001953125
        self.assertAlmostEqual(B.mcnemar_exact(10, 0), 0.001953125, places=9)
        # b=6, c=1, n=7: 2*(C(7,0)+C(7,1))/2^7 = 2*8/128 = 0.125
        self.assertAlmostEqual(B.mcnemar_exact(6, 1), 0.125, places=9)

    def test_symmetric_swap_same_p(self):
        self.assertAlmostEqual(B.mcnemar_exact(8, 2), B.mcnemar_exact(2, 8))


class TestWilsonCI(unittest.TestCase):
    def test_zero_sample(self):
        self.assertEqual(B.wilson_ci(0, 0), (0.0, 0.0))

    def test_all_pass_upper_bound_is_one(self):
        lo, hi = B.wilson_ci(10, 10)
        self.assertGreater(lo, 0.5)
        self.assertLessEqual(hi, 1.0)

    def test_none_pass_lower_bound_zero(self):
        lo, _hi = B.wilson_ci(0, 10)
        self.assertEqual(lo, 0.0)

    def test_contains_point_estimate(self):
        lo, hi = B.wilson_ci(5, 10)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)


class TestPairedStats(unittest.TestCase):
    def _rows(self, pairs):
        no = [{"id": i, "passed": a} for i, (a, _b) in enumerate(pairs)]
        w = [{"id": i, "passed": b} for i, (_a, b) in enumerate(pairs)]
        return no, w

    def test_counts_discordant_pairs(self):
        # 3 题只 With 过，1 题只 No 过，2 题都对，1 题都错
        pairs = [(False, True), (False, True), (False, True),
                 (True, False), (True, True), (True, True), (False, False)]
        no, w = self._rows(pairs)
        st = B.paired_stats(no, w)
        self.assertEqual(st["only_with"], 3)
        self.assertEqual(st["only_no"], 1)
        self.assertEqual(st["both_pass"], 2)
        self.assertEqual(st["both_fail"], 1)
        self.assertEqual(st["n"], 7)
        # no 通过=3（both2+only_no1），with 通过=5（only_with3+both2）
        self.assertAlmostEqual(st["delta_pp"],
                               round((5 / 7 - 3 / 7) * 100, 2), places=2)

    def test_identical_arms_gives_zero_delta(self):
        pairs = [(True, True), (False, False)]
        no, w = self._rows(pairs)
        st = B.paired_stats(no, w)
        self.assertEqual(st["delta_pp"], 0.0)
        self.assertEqual(st["mcnemar_p"], 1.0)


class TestBenchCases(unittest.TestCase):
    def test_case_count_and_splits(self):
        cases = B.build_cases()
        self.assertEqual(len(cases), 60, "基准必须是 60 题")
        train = [c for c in cases if c["split"] == "train"]
        hold = [c for c in cases if c["split"] == "holdout"]
        self.assertEqual(len(train), 20)
        self.assertEqual(len(hold), 40)
        # 4 个域各 5 训练 + 10 留出
        self.assertEqual(len({c["domain"] for c in cases}), 4)

    def test_holdout_never_used_for_training(self):
        cases = B.build_cases()
        train_ids = {c["id"] for c in cases if c["split"] == "train"}
        hold_ids = {c["id"] for c in cases if c["split"] == "holdout"}
        self.assertEqual(train_ids & hold_ids, set(), "训练/留出有重叠题")

    def test_hidden_tests_are_syntactically_valid(self):
        """每道隐藏测试都必须能编译（否则判分全是假失败）。"""
        for c in B.build_cases():
            compile(c["hidden"], "<hidden:%s>" % c["id"], "exec")

    def test_invariant_not_leaked_into_prompt(self):
        """题面不得直接给出私有规约的**结论**（否则 0-shot 就不叫 0-shot）。

        注意：标识符（如 coupling 域必须展示的 registry.py 里的 REQUIRED）
        是「被改造的现有代码」，不是规约结论，不算泄露。
        """
        leaks = ("单位是分", "不做除以 100", "从 1 开始", "每页上限",
                 "必须同时登记", "以 Z 结尾", "naive datetime 直接拒绝")
        for c in B.build_cases():
            low = c["prompt"].lower()
            hit = [w for w in leaks if w.lower() in low]
            self.assertEqual(hit, [], "%s 题面泄露了规约 %s" % (c["id"], hit))


if __name__ == "__main__":
    unittest.main()
