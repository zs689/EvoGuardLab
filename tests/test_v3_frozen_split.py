# -*- coding: utf-8 -*-
"""v3 工具链：冻结划分的确定性与判定线的可证伪性（不跑实验）。"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import build_v3_frozen_split as B              # noqa: E402
import v3_gate as G                            # noqa: E402


GOOD = {
    "auc_drop_max": 0.005, "fp_max": 0.03, "tail_gain_min": 0.2,
    "overdefense_fp_max": 0.02, "slice_fp_max": 0.04,
    "side_effect_max": 0, "silent_pass_max": 0,
    "latency_ratio_max": 1.5,
}


class TestFrozenSplit(unittest.TestCase):
    def test_deterministic(self):
        a, b = B.build(), B.build()
        self.assertEqual(a["integrity_sha256"], b["integrity_sha256"])
        self.assertEqual(a["gate_sha256"], G.gate_sha256())

    def test_groups_are_not_split_across_sets(self):
        rep = B.build()
        owner = {}
        for split, info in rep["splits"].items():
            for g in info["groups"]:
                self.assertNotIn(g, owner,
                                 "模板组 %s 跨了 %s 和 %s" % (g, owner.get(g), split))
                owner[g] = split

    def test_unseen_schema_has_no_overlap_with_calibration(self):
        rep = B.build()
        seen = set(rep["splits"]["calibration"]["schemas"])
        unseen = set(rep["splits"]["test_unseen_schema"]["schemas"])
        self.assertFalse(seen & unseen, "未见 schema 与校准集有交集")
        self.assertFalse(set(rep["seen_schemas"]) & set(rep["unseen_schemas"]))

    def test_all_five_slices_present(self):
        rep = B.build()
        for split, info in rep["splits"].items():
            self.assertEqual(set(info["slices"]), set(B.SLICES), split)

    def test_freeze_refuses_second_generation(self):
        d = tempfile.mkdtemp(prefix="evo-v3freeze-")
        p = os.path.join(d, "frozen_split.json")
        rep = B.build()
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, sort_keys=True)
        ok, state = B.verify_frozen(p)
        self.assertTrue(ok)
        self.assertEqual(state, "frozen")

    def test_tampered_freeze_is_detected(self):
        d = tempfile.mkdtemp(prefix="evo-v3tamper-")
        p = os.path.join(d, "frozen_split.json")
        rep = B.build()
        rep["total"] = rep["total"] + 1
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, sort_keys=True)
        ok, state = B.verify_frozen(p)
        self.assertFalse(ok)
        self.assertEqual(state, "tampered")

    def test_experiment_still_not_executed(self):
        p = os.path.join(ROOT, "experiments", "semantic_injection_v3",
                         "result.json")
        self.assertFalse(os.path.isfile(p),
                         "工具链不能替独立复跑者产出实验结果")


class TestGateFalsifiability(unittest.TestCase):
    def test_all_limits_pass_with_good_metrics(self):
        ok, rows = G.evaluate(GOOD)
        self.assertTrue(ok)
        self.assertEqual(len(rows), 8)

    def test_each_single_violation_fails(self):
        """八条线逐条越线，每条都必须能单独把总判定拉红。"""
        step = {
            "auc_drop_max": 0.02, "fp_max": 0.06, "tail_gain_min": 0.05,
            "overdefense_fp_max": 0.06, "slice_fp_max": 0.06,
            "side_effect_max": 1, "silent_pass_max": 1,
            "latency_ratio_max": 2.5,
        }
        for key, bad in step.items():
            m = dict(GOOD)
            m[key] = bad
            ok, rows = G.evaluate(m)
            self.assertFalse(ok, "判定线 %s 被绕过" % key)
            failed = [r["check"] for r in rows if not r["ok"]]
            self.assertEqual(failed, [key], key)

    def test_gate_fingerprint_unchanged(self):
        self.assertEqual(
            G.gate_sha256(),
            "8aba16521813e0c7552f6581014a7f6da1dc32893cbaa14b75f47149a6f9ba9d")


if __name__ == "__main__":
    unittest.main()
