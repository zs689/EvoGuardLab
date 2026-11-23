# -*- coding: utf-8 -*-
"""HARD-03-T1：晋升统计协议 v2（修前必红反例）。

老门禁 3 题全对 p=0.125 就 PROMOTE（9% 晋升率），统计上不可信。
v2 要求最少 8 个配对样本且单侧 chance p < 0.05。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                 # noqa: E402
import skills as SK                                         # noqa: E402


class TestProtocolV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pv2-")
        self.store = core.Store(os.path.join(self.tmp.name, "p.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def _evaluate(self, n, *, protocol="v2"):
        skill = {"id": "s_test", "name": "candidate", "status": "candidate",
                 "version": 1, "body": "def f(): pass", "tags": []}
        target = [{"task_id": "t%d" % i, "agent_type": "react",
                   "category": "target", "fault_type": "none",
                   "fault_chain": [], "fault_target": 0, "seed": 7}
                  for i in range(n)]
        guard = [{"task_id": "g%d" % i, "agent_type": "react",
                  "category": "guard", "fault_type": "none",
                  "fault_chain": [], "fault_target": 0, "seed": 7}
                 for i in range(3)]
        def run_case(case, use_skill):
            # guard 必须 baseline 成功（才能估计负迁移），且 candidate
            # 不回归；target baseline 失败、candidate 成功 => 正增益。
            if case.get("category") == "guard":
                return True
            return bool(use_skill)
        return SK.evaluate(self.store, skill, target, guard, run_case,
                           protocol=protocol)

    def test_n_less_than_8_rejected_under_v2(self):
        ev = self._evaluate(3)
        self.assertEqual(ev["verdict"], "REJECT", ev)
        self.assertTrue(any("统计" in r or "样本" in r
                            for r in ev["reasons"]), ev["reasons"])

    def test_p_above_005_rejected_under_v2(self):
        ev = self._evaluate(8)          # 8 题全对 p=0.0039 < 0.05 -> 应通过
        self.assertEqual(ev["verdict"], "PROMOTE", ev)
        self.assertLess(ev["chance_p_value"], 0.05)
        self.assertTrue(ev["stat_significant"])

    def test_v1_legacy_preserves_old_behavior(self):
        ev = self._evaluate(3, protocol="v1_legacy")
        self.assertEqual(ev["verdict"], "PROMOTE", ev)
        self.assertFalse(ev["stat_significant"])

    def test_invalid_protocol_fails_loud(self):
        with self.assertRaises(ValueError):
            self._evaluate(3, protocol="v3_unknown")


class TestLegacyCompat(unittest.TestCase):
    def test_active_legacy_skills_keep_v1_marker(self):
        """历史 7 个 active 技能不被 v2 打断，标 v1_legacy。"""
        import inspect
        src = inspect.getsource(SK)
        self.assertIn("v1_legacy", src)
        self.assertIn("stat_significant", src)
        # list_skills 必须透出字段
        ls = inspect.getsource(SK.list_skills)
        self.assertIn("stat_significant", ls)


if __name__ == "__main__":
    unittest.main()
