# -*- coding: utf-8 -*-
"""EG-017：v3 判定线必须在跑实验之前写死，且不许事后漂移。"""
import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import v3_gate as G                                         # noqa: E402

PLAN = os.path.join(ROOT, "docs", "semantic-injection-v3-plan.md")
# 预注册时算出的指纹；改任何一个判定线数字都会让这条测试红。
FROZEN_SHA256 = "8aba16521813e0c7552f6581014a7f6da1dc32893cbaa14b75f47149a6f9ba9d"


class TestV3Preregistration(unittest.TestCase):
    def test_gate_fingerprint_is_frozen(self):
        self.assertEqual(G.gate_sha256(), FROZEN_SHA256,
                         "判定线被改过；预注册不允许事后调线")

    def test_document_matches_code(self):
        with io.open(PLAN, encoding="utf-8") as f:
            text = f.read()
        pairs = dict(re.findall(r"`([a-z_]+)\s*=\s*([0-9.]+)`", text))
        for key, limit in G.GATE.items():
            self.assertIn(key, pairs, "文档 §六 缺判定线 %s" % key)
            self.assertAlmostEqual(float(pairs[key]), float(limit),
                                   msg="文档与代码不一致：%s" % key)

    def test_all_eight_checks_required(self):
        ok, rows = G.evaluate({
            "auc_drop_max": 0.005, "fp_max": 0.03, "tail_gain_min": 0.2,
            "overdefense_fp_max": 0.02, "slice_fp_max": 0.04,
            "side_effect_max": 0, "silent_pass_max": 0,
            "latency_ratio_max": 1.5})
        self.assertTrue(ok)
        self.assertEqual(len(rows), 8)
        ok2, rows2 = G.evaluate({"auc_drop_max": 0.005})
        self.assertFalse(ok2, "缺项不能算通过")
        self.assertEqual(sum(1 for r in rows2 if not r["ok"]), 7)

    def test_missing_metric_is_not_treated_as_zero(self):
        _, rows = G.evaluate({"side_effect_max": None})
        r = [x for x in rows if x["check"] == "side_effect_max"][0]
        self.assertFalse(r["ok"])
        self.assertIn("未测", r["note"])

    def test_v2_negative_result_is_archived_intact(self):
        ok, note = G.v2_archive_intact()
        self.assertTrue(ok, note)

    def test_experiment_not_yet_executed(self):
        """预注册状态：还没有 v3 结果文件。跑了之后要显式更新本测试。"""
        p = os.path.join(ROOT, "experiments", "semantic_injection_v3",
                         "result.json")
        self.assertFalse(os.path.isfile(p),
                         "v3 已有结果文件，请同步更新预注册状态与台账")

    def test_plan_declares_no_gate_relaxation(self):
        with io.open(PLAN, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("不放宽 v2 的任何判定线", text)
        self.assertIn("八条全满足才算通过", text)


if __name__ == "__main__":
    unittest.main()
