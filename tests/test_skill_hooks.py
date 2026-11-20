# -*- coding: utf-8 -*-
"""FINAL-02-T2：Skill 从提示词升级为可执行钩子的测试。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import skill_hooks as SH                                    # noqa: E402


class TestSkillHooks(unittest.TestCase):
    def test_empty_shell_skill_not_actionable(self):
        skill = {"error_signature": "timeout",
                 "fix_pattern": "unknown_no_repair_trace",
                 "verification_check": "unknown_no_verification_trace"}
        self.assertFalse(SH.is_actionable(skill))
        h = SH.hooks_from_skill(skill)
        self.assertFalse(h["actionable"])
        self.assertIsNone(h["pre_write"])

    def test_real_skill_actionable_and_hooks_work(self):
        skill = {"error_signature": "ZeroDivisionError|zero_division",
                 "fix_pattern": "fs.patch: replace denominator guard",
                 "verification_check": "code.run_in_sandbox: assert no ZeroDivision"}
        self.assertTrue(SH.is_actionable(skill))
        h = SH.hooks_from_skill(skill)
        self.assertTrue(h["actionable"])
        pre = h["pre_write"]("fs.write", {"path": "m.py"})
        self.assertEqual(pre["prefer_tool"], "fs.patch")
        post = h["post_write"]("fs.patch", {"path": "m.py"}, {"patched": True})
        self.assertTrue(post["must_run"])
        self.assertIn("code.run_in_sandbox", post["verify"])

    def test_read_tool_has_no_write_hooks(self):
        skill = {"error_signature": "IndexError",
                 "fix_pattern": "fs.patch: fix index bound",
                 "verification_check": "code.run_in_sandbox: assert bounds"}
        h = SH.hooks_from_skill(skill)
        self.assertIsNone(h["pre_write"]("fs.read", {"path": "m.py"}))
        self.assertIsNone(h["post_write"]("fs.read", {"path": "m.py"}, {}))


if __name__ == "__main__":
    unittest.main()
