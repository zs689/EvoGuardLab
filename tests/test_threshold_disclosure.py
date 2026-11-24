# -*- coding: utf-8 -*-
"""阈值校准的诚实披露。

历史问题：`pick_threshold` 在 calib 上选出 high=0.4 并写进产物，但实测
400 条样本上它与默认阈值判定完全相同（changed=0）——真正起作用的是
权重内置阈值。把 recall/fpr 归因给"calib 选出的 high"是虚假归因。

判据（可复核）：
  1. 产物必须含 changed_decisions_vs_default / threshold_effective；
  2. 两者必须自洽（changed==0 时 effective 必须为 False）；
  3. changed==0 时必须给出 warning，且默认阈值指标与 L1+light 一致。
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "experiments", "pub_jailbreak_layered.json")


@unittest.skipUnless(os.path.isfile(ART), "产物不存在")
class TestThresholdDisclosure(unittest.TestCase):
    def setUp(self):
        with open(ART, encoding="utf-8") as f:
            self.d = json.load(f)

    def test_fields_present(self):
        t = self.d.get("threshold_calibration") or {}
        self.assertIn("changed_decisions_vs_default", t)
        self.assertIn("threshold_effective", t)
        self.assertIn("default_threshold_metrics", t)

    def test_consistency(self):
        t = self.d["threshold_calibration"]
        self.assertEqual(bool(t["threshold_effective"]),
                         t["changed_decisions_vs_default"] > 0,
                         "effective 与 changed 不自洽")

    def test_no_effect_means_warning_and_equal_metrics(self):
        t = self.d["threshold_calibration"]
        if t["changed_decisions_vs_default"] == 0:
            self.assertIsNotNone(t.get("note"))
            self.assertTrue(self.d["self_checks"].get("warning"),
                            "空操作校准必须给出 warning")
            # 校准无效时，两组指标应当完全相同
            self.assertEqual(t["default_threshold_metrics"]["recall"],
                             self.d["L1_plus_light"]["recall"])
            self.assertEqual(t["default_threshold_metrics"]["fpr"],
                             self.d["L1_plus_light"]["fpr"])


if __name__ == "__main__":
    unittest.main()
