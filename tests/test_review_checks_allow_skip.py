# -*- coding: utf-8 -*-
"""PREPUSH-01：--allow-skip-id 必须**按 id 精确**放行，不放宽其它 SKIP。

背景：review_checks 的 gate 把任意未允许 SKIP 视为失败（正确）。CI 里
windows-latest 必然跳 DEEP-01/no-orphans、clean-clone 必然跳
EG-024/static-names；若只有粗粒度 --allow-skip，一旦多跳了一项也会全绿，
又回到"CI 少查一项仍通过"的根源。
"""
import contextlib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import review_checks as RC                                  # noqa: E402


def _run(checks, extra=()):
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


def _skip(rid):
    return ("X-" + rid, lambda: RC._result(
        rid, True, "x", "y", skipped=True))


class TestAllowSkipById(unittest.TestCase):
    def test_only_id_a_allowed(self):
        checks = [_skip("A")]
        rc, out = _run(checks, ["--allow-skip-id", "A"])
        self.assertEqual(rc, 0, "A 在白名单内应放行")
        self.assertIn("A", out)

    def test_b_not_allowed_when_only_a_whitelisted(self):
        checks = [_skip("A"), _skip("B")]
        rc, out = _run(checks, ["--allow-skip-id", "A"])
        self.assertEqual(rc, 1, "白名单外的 B 仍 SKIP 时 gate 必须失败")
        self.assertIn("B", out)

    def test_no_whitelist_means_strict(self):
        checks = [_skip("DEEP-01/no-orphans")]
        rc, _ = _run(checks)
        self.assertEqual(1, rc)

    def test_repeatable_whitelist(self):
        checks = [_skip("A"), _skip("B")]
        rc, _ = _run(checks, ["--allow-skip-id", "A",
                              "--allow-skip-id", "B"])
        self.assertEqual(0, rc)

    def test_legacy_allow_skip_still_works(self):
        checks = [_skip("A"), _skip("B")]
        rc, _ = _run(checks, ["--allow-skip"])
        self.assertEqual(0, rc, "向后兼容不能破坏")

    def test_pass_checks_are_not_affected(self):
        checks = [_skip("A"), ("X-pass", lambda: RC._result(
            "PASS", True, "x", "y"))]
        rc, _ = _run(checks, ["--allow-skip-id", "A"])
        self.assertEqual(0, rc)


if __name__ == "__main__":
    unittest.main()
