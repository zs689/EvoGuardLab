# -*- coding: utf-8 -*-
"""gate 的 skip 不能静默算通过——这是「CI 少查一项仍全绿」的根源。"""
import contextlib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import review_checks as RC                                  # noqa: E402


def _run_gate(checks, extra=()):
    old_checks, old_argv = RC.CHECKS, sys.argv
    try:
        RC.CHECKS = checks
        sys.argv = ["review_checks.py", "--gate"] + list(extra)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = RC.main()
        return rc, buf.getvalue()
    finally:
        RC.CHECKS, sys.argv = old_checks, old_argv


class TestSkipStrictness(unittest.TestCase):
    def test_skip_fails_gate_by_default(self):
        checks = [("TEST", lambda: RC._result(
            "TEST", True, "x", "y", skipped=True))]
        rc, out = _run_gate(checks)
        self.assertEqual(rc, 1, "skip 未显式允许时 gate 必须非零退出")
        self.assertIn("skip 未获显式允许", out)

    def test_allow_skip_passes(self):
        checks = [("TEST", lambda: RC._result(
            "TEST", True, "x", "y", skipped=True))]
        rc, _ = _run_gate(checks, ["--allow-skip"])
        self.assertEqual(rc, 0, "显式允许后应放行")

    def test_real_pass_still_zero(self):
        checks = [("TEST", lambda: RC._result("TEST", True, "x", "y"))]
        rc, out = _run_gate(checks)
        self.assertEqual(rc, 0)
        self.assertIn("0 skip", out)

    def test_fail_still_nonzero(self):
        checks = [("TEST", lambda: RC._result("TEST", False, "x", "y"))]
        rc, _ = _run_gate(checks)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
