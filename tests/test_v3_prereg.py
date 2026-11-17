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

    def test_result_if_present_is_honestly_marked(self):
        """结果可以由工具链产出，但**绝不能冒充独立复跑**。

        预注册的原始断言是「result.json 不存在」。WS-03-T1 要求本机执行
        实验，所以这里显式更新：允许文件存在，但必须
        1) independent_reproduction=false；
        2) gate_sha256 与冻结判定线一致；
        3) 判定失败时 passed=false（不许靠改判定线翻绿）。
        """
        import json
        p = os.path.join(ROOT, "experiments", "semantic_injection_v3",
                         "result.json")
        if not os.path.isfile(p):
            return                      # 尚未执行也合法
        with io.open(p, encoding="utf-8") as f:
            d = json.load(f)
        self.assertIs(d.get("independent_reproduction"), False,
                      "工具链自跑必须标记 independent_reproduction=false，"
                      "不能冒充未参与调参的独立复跑者")
        self.assertEqual(d.get("gate_sha256"), G.gate_sha256(),
                         "结果里的判定线指纹与冻结值不一致")
        self.assertIn("prereg_status", d)
        if not d.get("passed"):
            self.assertTrue(d.get("checks") or d.get("metrics"),
                            "判定失败也必须留下逐条证据，不许直接删结果")

    def test_plan_declares_no_gate_relaxation(self):
        with io.open(PLAN, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("不放宽 v2 的任何判定线", text)
        self.assertIn("八条全满足才算通过", text)


if __name__ == "__main__":
    unittest.main()
