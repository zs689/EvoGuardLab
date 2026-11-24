# -*- coding: utf-8 -*-
"""检查自检本身也要能失败——否则它自己就是另一个"永远绿"的检查。"""
import contextlib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import review_checks as RC                                  # noqa: E402


def _run_self_test(tests):
    old = RC.SELF_TESTS
    try:
        RC.SELF_TESTS = tests
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = RC.cmd_self_test()
        return rc, buf.getvalue()
    finally:
        RC.SELF_TESTS = old


class TestGateSelfTest(unittest.TestCase):
    def test_noop_corruption_is_detected_as_fake_green(self):
        """样本没破坏任何东西 -> 检查仍 pass -> 自检必须判 FAIL。"""
        rc, out = _run_self_test((("c_gate_in_ci", "什么都不改", lambda r: None),))
        self.assertEqual(rc, 1, "自检没有识破无效样本（自己变假绿了）")
        self.assertIn("假绿", out)

    def test_real_self_tests_pass(self):
        rc, out = _run_self_test(RC.SELF_TESTS)
        self.assertEqual(rc, 0, out)
        self.assertIn("%d/%d" % (len(RC.SELF_TESTS), len(RC.SELF_TESTS)), out)
        self.assertIn("覆盖率", out)
        self.assertIn("未覆盖", out)


if __name__ == "__main__":
    unittest.main()
