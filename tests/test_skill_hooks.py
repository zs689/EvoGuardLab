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


class TestEnvHintInjection(unittest.TestCase):
    """EVO-REBUILD-01-T1：env_invariant 必须在命中签名时按需注入单行事实。"""

    SKILL = {"error_signature": "KeyError|key_error",
             "fix_pattern": "unknown_no_repair_trace",
             "verification_check": "unknown_no_verification_trace",
             "env_invariants": [
                 {"domain": "naming", "rule": "字段名必须 snake_case（如 user_name）",
                  "evidence": "KeyError: 'user_name'"},
                 {"domain": "pagination", "rule": "分页从 1 开始",
                  "evidence": "422 offset must be >= 1"},
             ]}

    def test_hint_injected_on_signature_match(self):
        h = SH.env_hint_for(self.SKILL, "KeyError: 'user_name'")
        self.assertIsNotNone(h)
        self.assertIn("snake_case", h)

    def test_only_one_line_injected(self):
        h = SH.env_hint_for(self.SKILL, "KeyError: 'x'")
        self.assertEqual(h.count("\n"), 0, "注入的不是单行事实")
        self.assertNotIn("分页", h, "一次注入了多条规约")

    def test_no_hint_on_signature_mismatch(self):
        self.assertIsNone(SH.env_hint_for(self.SKILL, "ValueError: bad input"))

    def test_hooks_actionable_with_invariant_only(self):
        """即使 fix_pattern 是 unknown，有真实 invariant 也算可用。"""
        hooks = SH.hooks_from_skill(self.SKILL)
        self.assertTrue(hooks["actionable"])
        self.assertIsNotNone(hooks["env_hint"])
        self.assertIsNone(hooks["pre_write"])

    def test_flat_skill_has_no_hint(self):
        hooks = SH.hooks_from_skill({"error_signature": "timeout",
                                     "fix_pattern": "unknown",
                                     "verification_check": "unknown"})
        self.assertFalse(hooks["actionable"])
        self.assertIsNone(hooks["env_hint"])


if __name__ == "__main__":
    unittest.main()
