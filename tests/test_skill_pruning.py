# -*- coding: utf-8 -*-
"""ZERO-05-T1：零增益候选技能预筛剪枝（修前必红反例）。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                 # noqa: E402
import skill_synthesis as SS                                # noqa: E402


def _trace(err):
    return [{"seq": 1, "logical": 1, "name": "code.run_in_sandbox",
             "status": "error", "error_type": "AssertionError",
             "output": {"stderr": err}}]


class TestSkillPruning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "p.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_vague_candidate_rejected_at_entry(self):
        """没有具体环境差异证据的套话，合成入口就该拒。"""
        out = SS.synthesize(self.store, _trace("assertion failed"),
                            task_type="newdomain", require_info_gain=True)
        errs = " ".join(out["accepted"].get("errors") or [])
        self.assertFalse(out["accepted"]["accepted"], errs)
        self.assertTrue(any("信息增量" in e or "泛泛" in e
                            for e in out["accepted"]["errors"]), errs)

    def test_concrete_env_invariant_kept(self):
        """带具体边界/字段约束的候选必须保留。"""
        out = SS.synthesize(
            self.store,
            _trace("assert x >= 1 failed: got 0, boundary must start at 1"),
            task_type="newdomain", require_info_gain=True,
            env_spec=[{"match": "boundary must start at 1",
                       "rule": "分页起点是 1 不是 0",
                       "domain": "pagination"}])
        self.assertTrue(out["accepted"]["accepted"], out["accepted"])

    def test_pruning_reports_reason(self):
        out = SS.synthesize(self.store, _trace("something went wrong"),
                            require_info_gain=True)
        self.assertIn("accepted", out)
        self.assertTrue(out["accepted"].get("errors"))


if __name__ == "__main__":
    unittest.main()
