# -*- coding: utf-8 -*-
"""ZERO-02：门禁拆分 + git_sha 新鲜度（修前必红反例）。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import review_checks as RC                                  # noqa: E402


class TestGateSplit(unittest.TestCase):
    def test_every_check_has_kind(self):
        for cid, fn in RC.CHECKS:
            self.assertIn(cid, [x[0] for x in RC.CHECKS])
        kinds = RC.check_kinds()
        self.assertEqual(set(kinds.values()),
                         {"hygiene", "live_capability"}, kinds)
        self.assertEqual(len(kinds), len(RC.CHECKS))

    def test_live_checks_are_real_execution(self):
        """live 组必须含真起服务/真拦截，不能全是字符串检查。"""
        live = [fn.__name__ for cid, fn in RC.CHECKS
                if RC.check_kinds()[fn.__name__] == "live_capability"]
        self.assertIn("c_enterprise_e2e", live)
        self.assertIn("c_jobs_e2e", live)
        self.assertIn("c_evidence_freshness", live)

    def test_git_sha_mismatch_is_stale(self):
        d = os.path.join(ROOT, "docs", "review-remediation", "probes")
        ev = os.path.join(d, "ci_references.json")
        r = RC.evidence_git_sha([(ev, ["scripts/check_ci_references.py"])],
                                current_sha="0" * 40)
        self.assertFalse(r["fresh"], r)
        self.assertTrue(any("git_sha" in x for x in r["stale"]), r)

    def test_scorecard_marked_deprecated(self):
        import importlib.util
        p = os.path.join(ROOT, "ai-service", "evoguard", "enterprise",
                         "scorecard.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("DEPRECATED_FOR_CERTIFICATION", src)


if __name__ == "__main__":
    unittest.main()
