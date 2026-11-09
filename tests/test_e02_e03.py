# -*- coding: utf-8 -*-
"""E02 / E03 回归：独立家族增量验证 + 经验适用范围与 BM25。"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import conditional_skill as CS  # noqa: E402
import experience_scope as ES  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


E02 = _load("e02_eval", "scripts/conditional_skill_eval.py")
E03 = _load("e03_demo", "scripts/experience_scope_demo.py")


class TestE03ExperienceScope(unittest.TestCase):
    def setUp(self):
        self.records = E03._records()
        self.ctx = E03.CTX
        self.now = E03.NOW

    def test_scope_fields_are_recorded(self):
        for r in self.records:
            for f in ES.REQUIRED_FIELDS:
                self.assertIn(f, r, "%s 缺适用范围字段 %s" % (r["id"], f))
            self.assertEqual(r["scope_version"], ES.SCOPE_VERSION)

    def test_structural_filter_rejects_untrustworthy_records(self):
        cases = {
            "exp_revoked_best_match": "撤销",
            "exp_expired": "过期",
            "exp_wrong_data_version": "数据版本",
            "exp_wrong_contract": "工具契约",
            "exp_privilege_overflow": "权限范围",
        }
        for r in self.records:
            ok, why = ES.structural_filter(r, self.ctx, self.now)
            if r["id"] in cases:
                self.assertFalse(ok, r["id"])
                self.assertTrue(any(cases[r["id"]] in w for w in why),
                                "%s: %s" % (r["id"], why))
            else:
                self.assertTrue(ok, "%s: %s" % (r["id"], why))

    def test_revoke_returns_new_record(self):
        r = self.records[0]
        out = ES.revoke(r)
        self.assertTrue(out["revoked"])
        self.assertFalse(r["revoked"])

    def test_bm25_ranks_exact_match_first(self):
        docs = ["新版退货政策 按文档版本号选证据", "无关内容 天气",
                "退货政策 退款时限"]
        scores = ES.bm25("新版退货政策 按文档版本号选证据", docs)
        self.assertEqual(len(scores), 3)
        self.assertEqual(scores.index(max(scores)), 0)

    def test_bm25_zero_denominator_is_empty(self):
        self.assertEqual(ES.bm25("query", []), [])
        self.assertEqual(ES.bm25("", ["a", "b"]), [])

    def test_chinese_bigram_tokenization(self):
        t = ES.tokens("退货政策 ABC123")
        self.assertIn("退货", t)
        self.assertIn("abc123", t)

    def test_similarity_cannot_override_revocation(self):
        """反例：文本最像但已撤销 → 不进候选；去掉撤销标记它会排第一。"""
        with contextlib.redirect_stdout(io.StringIO()):
            d = E03.main(["--out", tempfile.mkdtemp(prefix="e03_")])
        self.assertTrue(d["pass"])
        self.assertTrue(d["revoked_record_excluded"])
        self.assertEqual(d["counterfactual_top_if_revocation_ignored"],
                         "exp_revoked_best_match")
        self.assertNotEqual(d["actual_top"], "exp_revoked_best_match")
        self.assertFalse(d["retrieval"]["embedding_used"])

    def test_retrieve_filters_before_ranking(self):
        r = ES.retrieve(self.records, "新版退货政策 按文档版本号选证据",
                        context=self.ctx, now=self.now, top_k=3)
        ids = {x["id"] for x in r["results"]}
        self.assertNotIn("exp_revoked_best_match", ids)
        self.assertEqual(r["eligible"], 2)
        self.assertEqual(r["excluded"], 5)
        self.assertTrue(all(x["reasons"] for x in r["excluded_records"]))


class TestE02IndependentFamilies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="e02_")
        with contextlib.redirect_stdout(io.StringIO()):
            cls.r = E02.main(["--out", cls.tmp])

    def test_four_families_have_disjoint_tasks_and_seeds(self):
        fam = self.r["manifest"]["families"]
        self.assertEqual(set(fam), {"train", "dev", "validation", "test"})
        ids = [x for v in fam.values() for x in v]
        self.assertEqual(len(ids), len(set(ids)), "task_id 不能跨家族重叠")
        seeds = [x for v in self.r["manifest"]["seeds"].values() for x in v]
        self.assertEqual(len(seeds), len(set(seeds)), "seed 不能跨家族重叠")

    def test_validation_and_test_show_reproducible_gain(self):
        for key in ("validation_verdict", "test_verdict"):
            v = self.r[key]
            self.assertEqual(v["verdict"], "PROMOTE", key)
            self.assertGreater(v["gain"], 0, key)
            self.assertEqual(v["negative_transfer_rate"], 0.0, key)

    def test_gain_only_appears_where_baseline_was_wrong(self):
        """条件技能不是全量提升：只在规则表原本选错的任务上有增量。"""
        cases = self.r["cases"]
        before = {c["key"]: c for c in cases if c["problem"] in
                  ("xref_version", "calc_order") and c["family"] == "validation"
                  and c["reason"].startswith("规则表基线")}
        improved = 0
        for k, b in before.items():
            after = [c for c in cases if c["key"] == k
                     and c["reason"].startswith("条件技能")]
            if after and after[0]["success"] and not b["success"]:
                improved += 1
        self.assertGreater(improved, 0)
        self.assertLess(improved, len(before), "不应是全量提升")

    def test_negative_result_is_published(self):
        neg = self.r["negative_result"]
        self.assertEqual(neg["verdict"], "REJECT")
        self.assertEqual(neg["gain"], 0.0)
        self.assertIn("负结果", neg["reason"])
        self.assertTrue(neg["failure_samples"])

    def test_unauthorized_escalation_is_rejected_despite_business_gain(self):
        esc = self.r["security_guard"]["escalation_attempt"]
        self.assertGreater(esc["unauthorized_cases"], 0)
        self.assertEqual(esc["verdict"], "REJECT")
        self.assertIn("业务收益不能抵消", esc["reason"])

    def test_normal_guard_not_false_blocked(self):
        g = self.r["security_guard"]["normal_skill"]
        self.assertGreater(g["normal_guard_cases"], 0)
        self.assertEqual(g["normal_guard_false_blocked"], 0)
        self.assertEqual(g["security_guard_unauthorized"], 0)

    def test_manifest_fingerprints_and_test_frozen(self):
        m = self.r["manifest"]
        self.assertTrue(m["test_frozen"])
        self.assertEqual(len(m["family_content_fingerprint"]), 64)
        self.assertEqual(len(m["policy_fingerprint"]), 64)
        self.assertIn("不把同一道题多次调用当作扩大业务样本", m["note"])

    def test_paired_cases_match_family_counts(self):
        c = self.r["counts"]
        self.assertEqual(c["paired_validation"], c["validation"] - 1)
        self.assertEqual(c["paired_test"], c["test"] - 1)

    def test_report_and_manifest_files_written(self):
        for name in ("report.md", "results.json", "manifest.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, name)), name)
        with open(os.path.join(self.tmp, "results.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["validation_verdict"]["verdict"], "PROMOTE")


if __name__ == "__main__":
    unittest.main()
