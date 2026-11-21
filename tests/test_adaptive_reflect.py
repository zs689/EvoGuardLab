# -*- coding: utf-8 -*-
"""MOD-AGENT-01-T1：按需反思（修前必红反例）。

公理 B：无条件插反思提示会稀释上下文导致 -4.35pp；只在连续失败
或卡壳时触发极简定向诊断。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import autonomous_agent as AA                               # noqa: E402


class TestAdaptiveReflect(unittest.TestCase):
    def test_first_failure_does_not_inject_long_reflection(self):
        """第 1 次失败：只回干净 stderr，不塞反思模板。"""
        d = AA.should_reflect(fail_count=1, same_error_twice=False,
                              near_loop_guard=False)
        self.assertFalse(d, "第一次失败不该打断推理")

    def test_two_same_errors_trigger_focused_reflection(self):
        self.assertTrue(AA.should_reflect(fail_count=2,
                                          same_error_twice=True,
                                          near_loop_guard=False))

    def test_near_loop_guard_triggers_reflection(self):
        self.assertTrue(AA.should_reflect(fail_count=1,
                                          same_error_twice=False,
                                          near_loop_guard=True))

    def test_different_errors_twice_do_not_trigger(self):
        """连续 2 次但错误类型不同：不算卡壳，不打断。"""
        self.assertFalse(AA.should_reflect(fail_count=2,
                                           same_error_twice=False,
                                           near_loop_guard=False))

    def test_reflection_message_is_short(self):
        msg = AA.focused_reflection_message("AssertionError: x != y",
                                            failed_tool="code.run_in_sandbox")
        self.assertLessEqual(len(msg), 200, "反思必须极简")
        self.assertIn("AssertionError", msg)


if __name__ == "__main__":
    unittest.main()
