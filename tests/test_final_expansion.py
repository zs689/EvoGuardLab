# -*- coding: utf-8 -*-
"""最后一轮：阈值版本、多 seed、跨 Agent 迁移、30+30 扩样、任务格子。"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from evoguard_inspect import assurance  # noqa: E402
import expanded_vectors_v1 as EX        # noqa: E402
import stability_v1 as ST               # noqa: E402


class TestThresholdVersion(unittest.TestCase):
    def test_registered_file_is_used(self):
        t = assurance.load_thresholds()
        self.assertEqual(t["source"], "file")
        self.assertEqual(t["version"], "thresholds-v1")
        self.assertEqual(t["model_error_fail"], 0.2)
        self.assertIn("development_set", t)

    def test_bad_or_missing_config_falls_back_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"model_error_fail": 9}')
            t = assurance.load_thresholds(p)
            self.assertEqual(t["source"], "builtin")
            self.assertIn("fallback_reason", t)
            t2 = assurance.load_thresholds(os.path.join(d, "nope.json"))
            self.assertEqual(t2["source"], "builtin")

    def test_fingerprint_is_stable_and_sensitive(self):
        t = assurance.load_thresholds()
        a = assurance.thresholds_fingerprint(t)
        b = assurance.thresholds_fingerprint(assurance.load_thresholds())
        self.assertEqual(a, b, "同一配置两次指纹不同")
        self.assertNotEqual(a, assurance.thresholds_fingerprint(
            dict(t, model_error_fail=0.3)))

    def test_operability_uses_configured_threshold(self):
        hl = {"traces": 10, "model_error_with_answer": 0,
              "model_error_rate": 0.25, "budget_hits": 0}
        strict = dict(assurance.load_thresholds(), model_error_fail=0.2)
        loose = dict(assurance.load_thresholds(), model_error_fail=0.9)
        self.assertEqual(assurance._operability(hl, strict)[0], assurance.FAIL)
        self.assertEqual(assurance._operability(hl, loose)[0], assurance.RISK)

    def test_build_reports_threshold_fingerprint(self):
        t = assurance.load_thresholds()
        data = assurance.build([], [])
        self.assertIn("thresholds", data)
        self.assertEqual(data["thresholds"]["fingerprint"],
                         assurance.thresholds_fingerprint(t))
        self.assertEqual(data["thresholds"]["source"], "file")


class TestVectorIndependence(unittest.TestCase):
    def test_exactly_30_vectors_with_unique_axes(self):
        ind = EX.independence_report(EX.VECTORS)
        self.assertEqual(ind["vector_count"], 30)
        self.assertTrue(ind["unique_ids"])
        self.assertTrue(ind["unique_entry_technique"])
        self.assertEqual(ind["duplicate_payloads"], [])
        self.assertLess(ind["max_pairwise_jaccard"], 0.85)

    def test_similar_payload_would_be_rejected(self):
        fake = list(EX.VECTORS[:2])
        fake[1] = tuple(list(fake[1][:3]) + [fake[0][3], fake[1][4],
                                            fake[1][5]])
        ind = EX.independence_report(fake)
        self.assertFalse(ind["passed"], "重复 payload 必须判不独立")

    def test_jaccard_bounds(self):
        self.assertEqual(EX.jaccard("abc", "abc"), 1.0)
        self.assertEqual(EX.jaccard("", ""), 0.0)
        self.assertGreaterEqual(EX.jaccard("abc", "abd"), 0.0)

    def test_config_signals_are_not_counted_as_content_fp(self):
        ev = EX.evaluate(EX.VECTORS[:6])
        for r in ev["rows"]:
            for c in ("attack_codes", "normal_codes"):
                self.assertFalse(EX.CONFIG_CODES & set(r[c]))
        self.assertEqual(ev["any_execution_claim"], 0,
                         "检测层不许声称执行了动作")

    def test_task_grid_has_at_least_three_unique_tasks_per_cell(self):
        g = EX.task_grid(3)
        self.assertGreater(g["cells"], 50)
        self.assertEqual(g["violations"], [])
        for key, cell in g["grid"].items():
            self.assertGreaterEqual(cell["n"], 3, key)
            self.assertEqual(cell["unique"], cell["n"], key)

    def test_grid_does_not_redefine_historical_tasks(self):
        import sim
        g = EX.task_grid()
        for cell in g["grid"].values():
            self.assertTrue(set(cell["tasks"]) <=
                            {t["task_id"] for t in sim.TASKS[cell["agent"]]})


class TestStability(unittest.TestCase):
    def test_development_distribution_is_real(self):
        d = ST.development_distribution()
        self.assertGreater(d["files"], 0)
        self.assertGreater(d["spans"], 0)
        self.assertIsNotNone(d["observed_error_rate"])

    def test_threshold_registration_is_deterministic(self):
        a = ST.register_thresholds()
        b = ST.register_thresholds()
        self.assertEqual(a["config_sha256"], b["config_sha256"],
                         "重复登记必须产出同一份配置")

    def test_seeds_and_case_builder(self):
        self.assertEqual(len(set(ST.SEEDS)), len(ST.SEEDS))
        cases = ST._cases()
        self.assertEqual(len(cases), 3 * 3 * 3)
        self.assertEqual(len(set(cases)), len(cases), "同一格里的任务不能重复")


@unittest.skipUnless(
    os.path.isfile(os.path.join(ROOT, "experiments", "stability_v1",
                                "report.md")), "还没跑 stability_v1")
class TestArtifacts(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "experiments", "stability_v1",
                               "thresholds.json"), encoding="utf-8") as f:
            self.t = json.load(f)
        with open(os.path.join(ROOT, "experiments", "stability_v1",
                               "multi_seed.json"), encoding="utf-8") as f:
            self.s = json.load(f)
        with open(os.path.join(ROOT, "experiments", "stability_v1",
                               "cross_agent.json"), encoding="utf-8") as f:
            self.x = json.load(f)
        with open(os.path.join(ROOT, "experiments", "expanded_vectors_v1",
                               "result.json"), encoding="utf-8") as f:
            self.e = json.load(f)

    def test_multiseed_discloses_determinism_not_model_stability(self):
        self.assertEqual(self.s["stdev"], 0.0)
        self.assertEqual(self.s["stable_cases"], self.s["total_cases"])
        self.assertIn("不能据此宣称真实模型", self.s["interpretation"])

    def test_cross_agent_states_architecture_and_simulation(self):
        self.assertFalse(self.x["architecture_allows_transfer"])
        self.assertIn("task_type", self.x["architecture_note"])
        self.assertIn("模拟", self.x["simulated_transfer"]["note"])
        self.assertIn(self.x["simulated_transfer"]["verdict"],
                      ("PROMOTE", "REJECT"))

    def test_expansion_discloses_limits(self):
        self.assertEqual(self.e["independence"]["vector_count"], 30)
        self.assertTrue(self.e["independence"]["passed"])
        self.assertEqual(self.e["evaluation"]["any_execution_claim"], 0)
        self.assertEqual(self.e["grid"]["violations"], [])
        with open(os.path.join(ROOT, "experiments", "expanded_vectors_v1",
                               "report.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("不是「所有攻击手段」", md)
        self.assertIn("不把新数字混进", md)


if __name__ == "__main__":
    unittest.main()
