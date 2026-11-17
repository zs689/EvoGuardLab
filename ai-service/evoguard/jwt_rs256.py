# -*- coding: utf-8 -*-
"""WS-06-T2：标准库 RS256 验签 + OIDC discovery（**不依赖 cryptography**）。

核心库 `dependencies=[]`。RS256 验证 = 模幂 + PKCS#1 v1.5 填充校验，
用 Python 内置大整数就能做，不需要非对称加密库。

安全要点（少一条都不算验签成功）：
- alg 必须严格等于 RS256（拒绝 none / RS384 / HS256 混淆攻击）；
- 用 JWKS 里的 kid 精确选公钥，kid 缺失且多 key 时拒绝（不猜）；
- 填充必须是 `00 01 FF..FF 00 || DigestInfo || hash`，且 DigestInfo 前缀匹配；
- 恒定时间比较（hmac.compare_digest），防时序侧信道；
- exp / nbf / iss / aud 全查。

OIDC discovery 用 urllib 拉 `/.well-known/openid-configuration` 与 jwks_uri；
**不做**自动信任：issuer 必须与配置一致（防 discovery 劫持）。
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.request

try:
    from . import jwt_hs256 as _hs
except ImportError:
    import jwt_hs256 as _hs                                 # type: ignore


class JWTError(ValueError):
    pass


#: DER 编码的 SHA-256 DigestInfo 前缀（RFC 8017 §9.2 note 1）
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_SHA1_PREFIX = bytes.fromhex("3021300906052b0e03021a05000414")


def _b64d(s):
    try:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad)
    except Exception as exc:                                # noqa: BLE001
        raise JWTError("bad_base64url:" + type(exc).__name__)


def jwk_to_rsa(jwk):
    """把 JWK（kty=RSA）转成 (n, e)。拒绝对称密钥（kty=oct）。"""
    if not isinstance(jwk, dict):
        raise JWTError("jwk_not_object")
    if jwk.get("kty") != "RSA":
        raise JWTError("kty_not_rsa:%s" % jwk.get("kty"))
    try:
        n = int.from_bytes(_b64d(jwk["n"]), "big")
        e = int.from_bytes(_b64d(jwk["e"]), "big")
    except (KeyError, TypeError) as exc:
        raise JWTError("bad_rsa_jwk:" + repr(exc)[:60])
    if n <= 0 or e <= 1:
        raise JWTError("invalid_rsa_params")
    return n, e


def _pkcs1_verify(signature, message, n, e, prefix):
    """RSASSA-PKCS1-v1_5 验签（SHA-256 / SHA-1 由 prefix 决定）。"""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    m = pow(s, e, n)                                        # 模幂：核心一步
    em = m.to_bytes(k, "big")
    # EM = 0x00 || 0x01 || PS(0xFF...) || 0x00 || T
    if not em.startswith(b"\x00\x01"):
        return False
    idx = em.find(b"\x00", 2)
    if idx < 10:                                            # PS 至少 8 字节
        return False
    if any(b != 0xFF for b in em[2:idx]):
        return False
    t = em[idx + 1:]
    digest = hashlib.sha256(message).digest()
    expect = prefix + digest
    return hmac.compare_digest(t, expect)


def verify(token, jwks, *, now=None, issuer=None, audience=None,
           leeway=0, allowed_algs=("RS256",)):
    """验签并返回 claims。jwks 可以是 {"keys":[...]} 或 key 列表。"""
    if not isinstance(token, str) or token.count(".") != 2:
        raise JWTError("not_three_parts")
    h, p, s = token.split(".")
    try:
        header = json.loads(_b64d(h).decode("utf-8"))
        claims = json.loads(_b64d(p).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise JWTError("bad_json:" + type(exc).__name__)
    if not isinstance(header, dict):
        raise JWTError("header_not_object")
    alg = header.get("alg")
    if alg not in allowed_algs:
        raise JWTError("algorithm_not_allowed:%s" % alg)     # 挡 none / HS256 混淆
    keys = jwks.get("keys") if isinstance(jwks, dict) else jwks
    if not isinstance(keys, list) or not keys:
        raise JWTError("no_jwks_keys")
    kid = header.get("kid")
    cands = [k for k in keys if isinstance(k, dict)
             and (kid is None or k.get("kid") == kid)]
    if kid is not None and not cands:
        raise JWTError("kid_not_found")
    if kid is None and len(cands) > 1:
        raise JWTError("kid_missing_ambiguous")              # 不猜哪把
    signing_input = ("%s.%s" % (h, p)).encode("ascii")
    sig = _b64d(s)
    ok = False
    for jwk in cands:
        try:
            n, e = jwk_to_rsa(jwk)
        except JWTError:
            continue
        prefix = _SHA1_PREFIX if alg == "RS1" else _SHA256_PREFIX
        if _pkcs1_verify(sig, signing_input, n, e, prefix):
            ok = True
            break
    if not ok:
        raise JWTError("bad_signature")
    now = time.time() if now is None else now
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
        if not (aud == audience or (isinstance(aud, list) and audience in aud)):
            raise JWTError("audience_mismatch")
    return claims


def discover(issuer, *, timeout=8, opener=None):
    """OIDC discovery：拉 /.well-known/openid-configuration 并校验 issuer。

    **必须**校验返回的 issuer 与请求一致 —— 否则被劫持的 discovery
    能把 jwks_uri 指向攻击者的密钥。
    """
    base = str(issuer).rstrip("/")
    url = base + "/.well-known/openid-configuration"
    fn = opener or urllib.request.urlopen
    try:
        with fn(url, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except Exception as exc:                                # noqa: BLE001
        raise JWTError("discovery_failed:" + type(exc).__name__)
    if doc.get("issuer") != issuer:
        raise JWTError("issuer_mismatch_in_discovery")
    if not doc.get("jwks_uri"):
        raise JWTError("discovery_missing_jwks_uri")
    return doc


def fetch_jwks(jwks_uri, *, timeout=8, opener=None):
    fn = opener or urllib.request.urlopen
    try:
        with fn(jwks_uri, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:                                # noqa: BLE001
        raise JWTError("jwks_fetch_failed:" + type(exc).__name__)


def oidc_verifier(issuer=None, *, jwks=None, jwks_uri=None,
                  audience=None, timeout=8):
    """返回可注入 TenantResolver.verifier 的回调（RS256 + 可选 discovery）。"""
    state = {"jwks": jwks, "iss": issuer}

    def _v(token):
        if state["jwks"] is None:
            if not jwks_uri:
                if not issuer:
                    raise JWTError("no_jwks_and_no_issuer")
                doc = discover(issuer, timeout=timeout)
                state["jwks"] = fetch_jwks(doc["jwks_uri"], timeout=timeout)
                state["iss"] = doc["issuer"]
            else:
                state["jwks"] = fetch_jwks(jwks_uri, timeout=timeout)
        return verify(token, state["jwks"], issuer=state["iss"],
                      audience=audience)
    return _v
