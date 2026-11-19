# -*- coding: utf-8 -*-
"""SCORE-05-T2：EVOGUARD_PROFILE=strict 一键安全档位（修前必红）。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import env                                                  # noqa: E402


class TestProfile(unittest.TestCase):
    def test_demo_default_changes_nothing(self):
        e = {"PATH": "/x"}
        changed = env.apply_profile(e)
        self.assertEqual(e, {"PATH": "/x"})
        self.assertEqual(changed, [])

    def test_strict_enables_all_hard_gates(self):
        e = {}
        changed = env.apply_profile(e)
        e = {}
        e["EVOGUARD_PROFILE"] = "strict"
        changed = env.apply_profile(e)
        self.assertIn("AGENTLAB_SECURITY_MODE", changed)
        self.assertEqual(e["AGENTLAB_SECURITY_MODE"], "enforce")
        self.assertEqual(e["AGENTLAB_EXEC_MODE"], "controlled")
        self.assertEqual(e["EVOGUARD_LIGHT_GUARD"], "1")
        self.assertEqual(e["EVOGUARD_UNKNOWN_TOOL"], "deny")
        self.assertEqual(e["EVOGUARD_OUTPUT_BLOCK"], "1")

    def test_strict_does_not_overwrite_explicit_values(self):
        e = {"AGENTLAB_SECURITY_MODE": "audit", "EVOGUARD_PROFILE": "strict"}
        env.apply_profile(e)
        # 显式设置优先：档位是默认姿态，不是覆盖运维决定
        self.assertEqual(e["AGENTLAB_SECURITY_MODE"], "audit")

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            env.apply_profile({"EVOGUARD_PROFILE": "yolo"})

    def test_meta_reports_effective_profile(self):
        meta = env.profile_meta({})
        self.assertEqual(meta["profile"], "demo")
        e = {"EVOGUARD_PROFILE": "strict"}
        env.apply_profile(e)
        meta = env.profile_meta(e)
        self.assertEqual(meta["profile"], "strict")
        self.assertTrue(meta["hard_gates"]["security_enforce"])


if __name__ == "__main__":
    unittest.main()
