# -*- coding: utf-8 -*-
"""HARD-01：tool_synthesis 红队反例（修前必红）。

1. tests[].call 直接 eval 的逃逸；
2. 下划线属性绕过黑名单；
3. 幂运算/超大循环在宿主进程耗尽资源（线程无法强杀）。
"""
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import tool_synthesis as TS                                 # noqa: E402

PURE = ("def f(x):\n    return x + 1\n")
TESTS = [{"call": "f(1)", "expect": 2},
         {"call": "f(0)", "expect": 1},
         {"call": "f(-1)", "expect": 0}]


class TestHardening(unittest.TestCase):
    def test_dunder_payload_in_test_call_rejected(self):
        """红队：tests[].call 的 dunder 逃逸必须被 AST 拒，不能 eval。"""
        bad = dict(TESTS[0], call="().__class__.__base__.__subclasses__()")
        with self.assertRaises(TS.ToolSynthesisError):
            TS.synthesize_tool("evil.esc", PURE, [bad] + TESTS[1:])

    def test_underscore_attribute_rejected_everywhere(self):
        """任何下划线开头属性都拒（不止 BANNED_ATTRS 里列的那几个）。"""
        src = "def f(x):\n    return x._secret\n"
        with self.assertRaises(TS.ToolSynthesisError):
            TS.check_source(src, "f")

    def test_pow_bomb_rejected_statically(self):
        src = "def f(x):\n    return 10 ** (10 ** 8)\n"
        with self.assertRaises(TS.ToolSynthesisError):
            TS.check_source(src, "f")

    def test_huge_range_rejected_statically(self):
        src = ("def f(x):\n"
               "    total = 0\n"
               "    for i in range(10 ** 9):\n"
               "        total += i\n"
               "    return total\n")
        with self.assertRaises(TS.ToolSynthesisError):
            TS.check_source(src, "f")

    def test_timeout_kills_process_without_thread_leak(self):
        """超时必须杀子进程，主进程线程数不增长。"""
        src = ("def f(x):\n"
               "    total = 0\n"
               "    for i in range(10 ** 7):\n"
               "        total += i\n"
               "    return total\n")
        before = threading.active_count()
        with self.assertRaises(TS.ToolSynthesisError):
            TS.run_tests(src, "f", [dict(TESTS[0], call="f(1)")] * 3,
                         timeout_s=0.3)
        self.assertLessEqual(threading.active_count(), before,
                             "超时后主进程仍有泄漏线程")


if __name__ == "__main__":
    unittest.main()
