# -*- coding: utf-8 -*-
"""WS-06：分布式底座接口 + 零依赖默认实现。

注意边界：这里测的是「接口与默认实现」，不冒充生产级多副本压测
（Redis/PG 未实现，describe() 里如实列出）。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import distributed as D                                     # noqa: E402


class TestLimiters(unittest.TestCase):
    def test_memory_limiter_blocks_after_limit(self):
        lim = D.MemoryLimiter()
        ok1, _ = lim.check("a", limit=2, window=10, now=100.0)
        ok2, _ = lim.check("a", limit=2, window=10, now=101.0)
        ok3, wait = lim.check("a", limit=2, window=10, now=102.0)
        self.assertTrue(ok1 and ok2)
        self.assertFalse(ok3)
        self.assertGreater(wait, 0)

    def test_sqlite_limiter_same_semantics(self):
        lim = D.SQLiteLimiter()
        try:
            self.assertTrue(lim.check("a", 2, 10, now=100.0)[0])
            self.assertTrue(lim.check("a", 2, 10, now=101.0)[0])
            self.assertFalse(lim.check("a", 2, 10, now=102.0)[0])
        finally:
            lim.close()

    def test_sqlite_shares_across_instances(self):
        """两个实例指向同一文件 = 两个 API 副本共享配额。"""
        d = tempfile.mkdtemp(prefix="evo-rl-")
        p = os.path.join(d, "rl.db")
        a, b = D.SQLiteLimiter(p), D.SQLiteLimiter(p)
        now = 1000.0
        self.assertTrue(a.check("shared", 2, 10, now=now)[0])
        self.assertTrue(b.check("shared", 2, 10, now=now)[0])
        self.assertFalse(a.check("shared", 2, 10, now=now)[0],
                         "第二个副本看不到第一个的计数 → 配额翻倍")
        a.close()
        b.close()
        try:
            os.unlink(p)
            os.unlink(p + "-wal") if os.path.exists(p + "-wal") else None
            os.unlink(p + "-shm") if os.path.exists(p + "-shm") else None
            os.rmdir(d)
        except OSError:
            pass

    def test_window_expiry_frees_slot(self):
        lim = D.MemoryLimiter()
        lim.check("a", 1, 5, now=100.0)
        self.assertFalse(lim.check("a", 1, 5, now=101.0)[0])
        self.assertTrue(lim.check("a", 1, 5, now=106.0)[0])


class TestBreakerAndTenant(unittest.TestCase):
    def test_circuit_opens_after_threshold(self):
        b = D.CircuitBreakerState()
        for _ in range(5):
            s = b.record("x", True, now=10.0)
        self.assertTrue(b.is_open("x", now=11.0))
        b.record("x", False, now=12.0)
        self.assertFalse(b.is_open("x", now=13.0))

    def test_tenant_no_verifier_is_fail_closed(self):
        r = D.TenantResolver()
        tenant, err = r.resolve("Bearer anything")
        self.assertIsNone(tenant)
        self.assertEqual(err, "no_oidc_verifier_configured")

    def test_tenant_extracts_claims(self):
        r = D.TenantResolver(verifier=lambda t: {
            "tenant_id": "t1", "roles": ["admin", "user"]})
        tenant, err = r.resolve("Bearer abc")
        self.assertEqual(err, "")
        self.assertEqual(tenant["tenant_id"], "t1")
        self.assertEqual(tenant["roles"], ["admin", "user"])

    def test_tenant_rejects_missing_claim(self):
        r = D.TenantResolver(verifier=lambda t: {"sub": "u1"})
        tenant, err = r.resolve("Bearer abc")
        self.assertIsNone(tenant)
        self.assertEqual(err, "missing_tenant_claim")


class TestWebhook(unittest.TestCase):
    def test_no_url_is_explicit_not_sent(self):
        w = D.WebhookApprovalNotifier()
        self.assertFalse(w.notify("a1", {})["sent"])

    def test_receipt_hmac(self):
        w = D.WebhookApprovalNotifier(url="http://x", secret="s")
        sig = __import__("hmac").new(
            b"s", b"a1", __import__("hashlib").sha256).hexdigest()
        self.assertTrue(w.verify_receipt("a1", sig))
        self.assertFalse(w.verify_receipt("a1", "bad"))

    def test_no_secret_never_accepts_receipt(self):
        w = D.WebhookApprovalNotifier()
        self.assertFalse(w.verify_receipt("a1", "anything"))


class TestHonesty(unittest.TestCase):
    def test_describe_lists_not_implemented(self):
        """诚实性：已实现的不再列未实现；缺的仍如实列出。"""
        d = D.describe()
        self.assertTrue(d["not_implemented"])
        # Redis 后端已真实现并压测过（redis_backend.py），不能再列为未实现
        self.assertFalse(any("RedisLimiter" in x
                             for x in d["not_implemented"]))
        self.assertTrue(any("RedisLimiter" in x for x in d["implemented"]))
        # PG/RLS 与 OIDC discovery 确实没做，必须还在未实现列表
        self.assertTrue(any("PostgresStore" in x
                            for x in d["not_implemented"]))
        self.assertTrue(any("OIDC discovery" in x
                            for x in d["not_implemented"]))


if __name__ == "__main__":
    unittest.main()
