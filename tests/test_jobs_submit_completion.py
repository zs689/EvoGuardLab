# -*- coding: utf-8 -*-
"""证明 submit-vs-completion 不是放水：真失败时必须红。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import jobs_smoke as J                                      # noqa: E402


class TestSubmitCompletion(unittest.TestCase):
    ok = ["succeeded"] * 8

    def test_normal_passes(self):
        self.assertTrue(J.submit_completion_ok(self.ok, 15, 90000))

    def test_worker_sync_blocking_fails(self):
        # worker 同步：submit 等到完成，submit_p95 不再远小于 completion
        self.assertFalse(J.submit_completion_ok(self.ok, 89000, 90000))

    def test_any_failed_job_fails(self):
        self.assertFalse(J.submit_completion_ok(
            ["succeeded"] * 7 + ["failed"], 15, 90000))

    def test_missing_jobs_fails(self):
        self.assertFalse(J.submit_completion_ok(["succeeded"] * 7, 15, 90000))

    def test_ignores_timeout_as_pass_criterion(self):
        # 超时只是测量保护；判据只看状态与先后，不含硬编码毫秒阈值
        self.assertTrue(J.submit_completion_ok(self.ok, 100, 300000))


if __name__ == "__main__":
    unittest.main()
