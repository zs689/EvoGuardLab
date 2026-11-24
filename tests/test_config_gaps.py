# -*- coding: utf-8 -*-
"""`evoguard config` 要主动说出「当前配置下的风险」，而不是只列值让人猜。

依据：审计链实测在无 HMAC 时整链可被重写伪造
（tests/test_audit_chain_rewrite.py）。用户跑 config 应该看到这条风险，
而不是要读文档才知道 EVOGUARD_AUDIT_HMAC_KEY 是干什么的。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import baseline                                             # noqa: E402


class TestConfigGaps(unittest.TestCase):
    def test_missing_hmac_key_is_reported(self):
        gaps = baseline.describe_gaps({})
        keys = " ".join(g.get("key", "") for g in gaps)
        self.assertIn("EVOGUARD_AUDIT_HMAC_KEY", keys, gaps)
        hmac_gap = [g for g in gaps
                    if g.get("key") == "EVOGUARD_AUDIT_HMAC_KEY"][0]
        self.assertIn("整链", hmac_gap["impact"])

    def test_configured_hmac_key_removes_gap(self):
        gaps = baseline.describe_gaps({"EVOGUARD_AUDIT_HMAC_KEY": "k"})
        self.assertNotIn("EVOGUARD_AUDIT_HMAC_KEY",
                         [g["key"] for g in gaps])

    def test_no_secret_leaks_in_gaps(self):
        gaps = baseline.describe_gaps({"EVOGUARD_AUDIT_HMAC_KEY": "supersecret"})
        self.assertNotIn("supersecret", str(gaps))


if __name__ == "__main__":
    unittest.main()
