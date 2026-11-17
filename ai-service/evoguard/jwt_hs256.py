# -*- coding: utf-8 -*-
"""WS-06-T2：标准库 JWT HS256 验签（核心零依赖）。

只做对称密钥 HS256（OIDC client secret / 内部签发的 service token）。
RS256 需要非对称加密库（cryptography），核心库不引入 —— 通过
`TenantResolver(verifier=...)` 注入外部 verifier 支持，不假装支持。

**严格校验**（少一项都不算验签）：
- 三段式且 base64url 合法；
- alg 必须是 HS256（防 alg=none / 算法混淆）；
- HMAC 恒定时间比较（hmac.compare_digest）；
- exp / nbf 按当前时间校验（可注入 now 以便确定性测试）；
- iss / aud 可选白名单校验；
- 签名不符、过期、缺声明一律拒绝，绝不 returns claims 半成品。
"""
import base64
import hashlib
import hmac
import json
import time


class JWTError(ValueError):
    pass


def b64url_decode(s):
    try:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad)
    except Exception as exc:                                # noqa: BLE001
        raise JWTError("bad_base64url:" + type(exc).__name__)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def encode(claims, secret, *, now=None, ttl=360):
    """签发 HS256 token（供测试与内部 service token；生产用 OIDC 发行方）。"""
    now = time.time() if now is None else now
    body = dict(claims)
    body.setdefault("iat", int(now))
    body.setdefault("exp", int(now + ttl))
    h = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"},
                           sort_keys=True, separators=(",", ":")).encode())
    p = _b64url(json.dumps(body, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode("utf-8"), ("%s.%s" % (h, p)).encode("ascii"),
                   hashlib.sha256).digest()
    return "%s.%s.%s" % (h, p, _b64url(sig))


def verify(token, secret, *, now=None, issuer=None, audience=None,
           leeway=0):
    """验签并返回 claims；任何不合法都抛 JWTError（fail-closed）。"""
    if not isinstance(token, str) or token.count(".") != 2:
        raise JWTError("not_three_parts")
    h, p, s = token.split(".")
    try:
        header = json.loads(b64url_decode(h).decode("utf-8"))
        claims = json.loads(b64url_decode(p).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise JWTError("bad_json:" + type(exc).__name__)
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise JWTError("algorithm_not_allowed:%s" % header.get("alg"))
    expect = hmac.new(secret.encode("utf-8"), ("%s.%s" % (h, p)).encode("ascii"),
                      hashlib.sha256).digest()
    if not hmac.compare_digest(expect, b64url_decode(s)):
        raise JWTError("bad_signature")
    now = time.time() if now is None else now
    if not isinstance(claims, dict):
        raise JWTError("claims_not_object")
    exp = claims.get("exp")
    if exp is not None and float(exp) + leeway <= now:
        raise JWTError("token_expired")
    nbf = claims.get("nbf")
    if nbf is not None and float(nbf) - leeway > now:
        raise JWTError("token_not_yet_valid")
    if issuer is not None and claims.get("iss") != issuer:
        raise JWTError("issuer_mismatch")
    if audience is not None:
        aud = claims.get("aud")
        ok = (aud == audience or (isinstance(aud, list) and audience in aud))
        if not ok:
            raise JWTError("audience_mismatch")
    return claims


def tenant_verifier(secret, **kwargs):
    """返回可直接注入 TenantResolver.verifier 的回调。"""
    def _v(token):
        return verify(token, secret, **kwargs)
    return _v
