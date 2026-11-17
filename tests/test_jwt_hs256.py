# -*- coding: utf-8 -*-
"""WS-06-T2：标准库 JWT HS256 验签的 fail-closed 回归。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import distributed as D                                     # noqa: E402
import jwt_hs256 as J                                       # noqa: E402


class TestJWT(unittest.TestCase):
    SECRET = "test-secret"

    def test_roundtrip(self):
        t = J.encode({"tenant_id": "t1", "roles": ["admin"]},
                     self.SECRET, now=1000, ttl=60)
        c = J.verify(t, self.SECRET, now=1010)
        self.assertEqual(c["tenant_id"], "t1")
        self.assertEqual(c["roles"], ["admin"])

    def test_bad_signature_rejected(self):
        t = J.encode({"tenant_id": "t1"}, self.SECRET, now=1000)
        with self.assertRaises(J.JWTError):
            J.verify(t, "wrong-secret", now=1000)

    def test_alg_none_rejected(self):
        # 手搓 alg=none 的 token（无签名），必须拒绝
        h = J._b64url(b'{"alg":"none","typ":"JWT"}')
        p = J._b64url(b'{"tenant_id":"t1"}')
        with self.assertRaises(J.JWTError):
            J.verify("%s.%s." % (h, p), self.SECRET)

    def test_expired_rejected(self):
        t = J.encode({"tenant_id": "t1"}, self.SECRET, now=1000, ttl=10)
        with self.assertRaises(J.JWTError):
            J.verify(t, self.SECRET, now=1011)

    def test_nbf_rejected(self):
        t = J.encode({"tenant_id": "t1", "nbf": 2000},
                     self.SECRET, now=1000, ttl=3600)
        with self.assertRaises(J.JWTError):
            J.verify(t, self.SECRET, now=1500)

    def test_issuer_audience(self):
        t = J.encode({"tenant_id": "t1", "iss": "idp", "aud": "api"},
                     self.SECRET, now=1000)
        self.assertEqual(J.verify(t, self.SECRET, now=1000,
                                  issuer="idp", audience="api")["tenant_id"], "t1")
        with self.assertRaises(J.JWTError):
            J.verify(t, self.SECRET, now=1000, issuer="evil")

    def test_malformed_token_rejected(self):
        for bad in ("", "a.b", "a.b.c.d", "!!!.xxx.yyy"):
            with self.assertRaises(J.JWTError):
                J.verify(bad, self.SECRET)


class TestTenantIntegration(unittest.TestCase):
    def test_resolver_with_real_verifier(self):
        r = D.TenantResolver(verifier=J.tenant_verifier("s"))
        # 用真实时间签发（resolve 内部也用真实时间校验）
        t = J.encode({"tenant_id": "t9", "roles": ["reader"]}, "s")
        tenant, err = r.resolve("Bearer " + t)
        # 真实时间下刚签发的 token 未过期（ttl=360 默认）
        self.assertEqual(err, "")
        self.assertEqual(tenant["tenant_id"], "t9")

    def test_forged_token_is_rejected(self):
        r = D.TenantResolver(verifier=J.tenant_verifier("secret"))
        t = J.encode({"tenant_id": "attacker"}, "other-secret")
        tenant, err = r.resolve("Bearer " + t)
        self.assertIsNone(tenant)
        self.assertTrue(err.startswith("verify_failed"))


if __name__ == "__main__":
    unittest.main()
