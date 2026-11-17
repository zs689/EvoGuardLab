# -*- coding: utf-8 -*-
"""WS-06-T2：纯标准库 RS256 验签 + OIDC discovery 的 fail-closed 回归。

fixture 由 openssl 3.x **独立签名**（不是用本模块自己签），
所以「验签通过」证明了 PKCS#1 v1.5 实现与标准一致。
"""
import base64
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import jwt_rs256 as R                                       # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures", "rs256_token.json")


def _fixture():
    with open(FX, encoding="utf-8") as f:
        return json.load(f)


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class TestRS256(unittest.TestCase):
    def setUp(self):
        self.fx = _fixture()
        self.jwks = self.fx["jwks"]
        self.token = self.fx["token"]

    def test_openssl_signed_token_verifies(self):
        c = R.verify(self.token, self.jwks,
                     issuer="https://idp.example", audience="evoguard-api")
        self.assertEqual(c["tenant_id"], "tenant-fixture")

    def test_tampered_signature_rejected(self):
        bad = self.token[:-6] + "AAAAAA"
        with self.assertRaises(R.JWTError):
            R.verify(bad, self.jwks)

    def test_tampered_payload_rejected(self):
        h, p, s = self.token.split(".")
        forged = json.loads(base64.urlsafe_b64decode(
            p + "=" * (-len(p) % 4)))
        forged["tenant_id"] = "attacker"
        p2 = _b64u(json.dumps(forged, separators=(",", ":")).encode())
        with self.assertRaises(R.JWTError):
            R.verify("%s.%s.%s" % (h, p2, s), self.jwks)

    def test_alg_none_rejected(self):
        h = _b64u(b'{"alg":"none","kid":"fixture-2026"}')
        p = self.token.split(".")[1]
        with self.assertRaises(R.JWTError):
            R.verify("%s.%s." % (h, p), self.jwks)

    def test_hs256_confusion_rejected(self):
        """把 alg 改成 HS256、用公钥当 HMAC 密钥——经典混淆攻击。"""
        h = _b64u(json.dumps({"alg": "HS256", "kid": "fixture-2026"}).encode())
        _, p, s = self.token.split(".")
        with self.assertRaises(R.JWTError):
            R.verify("%s.%s.%s" % (h, p, s), self.jwks)

    def test_unknown_kid_rejected(self):
        keys = {"keys": [dict(k, kid="other") for k in self.jwks["keys"]]}
        with self.assertRaises(R.JWTError):
            R.verify(self.token, keys)

    def test_symmetric_jwk_rejected(self):
        """JWKS 里混入 kty=oct（对称密钥）不得被当作 RSA 公钥使用。"""
        with self.assertRaises(R.JWTError):
            R.jwk_to_rsa({"kty": "oct", "k": "abc"})

    def test_issuer_audience_mismatch(self):
        with self.assertRaises(R.JWTError):
            R.verify(self.token, self.jwks, issuer="https://evil.example")
        with self.assertRaises(R.JWTError):
            R.verify(self.token, self.jwks, audience="other-api")

    def test_expired_rejected(self):
        with self.assertRaises(R.JWTError):
            R.verify(self.token, self.jwks, now=4102444801)


class TestDiscovery(unittest.TestCase):
    def test_issuer_mismatch_in_discovery_rejected(self):
        """discovery 被劫持（返回别的 issuer）必须拒绝。"""
        class FakeResp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False
        payload = json.dumps({"issuer": "https://evil.example",
                              "jwks_uri": "https://evil.example/jwks"}).encode()
        with self.assertRaises(R.JWTError):
            R.discover("https://idp.example",
                       opener=lambda url, timeout=0: FakeResp(payload))

    def test_missing_jwks_uri_rejected(self):
        class FakeResp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False
        payload = json.dumps({"issuer": "https://idp.example"}).encode()
        with self.assertRaises(R.JWTError):
            R.discover("https://idp.example",
                       opener=lambda url, timeout=0: FakeResp(payload))


if __name__ == "__main__":
    unittest.main()
