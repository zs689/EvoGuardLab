# -*- coding: utf-8 -*-
"""FRONTIER-02：代理边界签发的可离线验证 Action Receipt（零依赖）。

对标 Pipelock / mcp-firewall（2026）：回执在 Agent 沙箱**外部**签发，
Agent 失陷也无法伪造；签名覆盖请求、策略、决策与执行效果。

未配置密钥时**显式标记 unsigned**，绝不伪装成已验签——这和审计链
HMAC 的诚实原则一致。
"""
import base64
import hashlib
import hmac
import json
import os
import time

VERSION = 1
KEY_ENV = "EVOGUARD_AUDIT_HMAC_KEY"


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(obj):
    return hashlib.sha256(_canon(obj)).hexdigest()


def sign(payload, *, key=None, boundary=None):
    """签一条回执。key=None 时 sig=None 且 unsigned=True。"""
    if key is None:
        key = os.environ.get(KEY_ENV) or None
    body = {
        "receipt_version": VERSION,
        "ts": time.time(),
        "boundary": boundary or "unknown",
        "request_sha256": _sha(payload.get("request") or {}),
        "policy_sha256": _sha(payload.get("policy") or {}),
        "decision": str(payload.get("decision") or "allow"),
        "executed": bool(payload.get("executed")),
        "reason": str(payload.get("reason") or "")[:200],
        "sig_alg": "HMAC-SHA256" if key else "none",
        "unsigned": key is None,
        "sig": None,
    }
    if key:
        unsigned_body = dict(body)      # 签名覆盖除 sig 之外的全部字段
        unsigned_body.pop("sig", None)
        body["sig"] = base64.urlsafe_b64encode(
            hmac.new(key.encode("utf-8"), _canon(unsigned_body), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
    return body


def verify(receipt, *, key=None):
    """离线验签。返回 {valid, status, reason}。"""
    if not isinstance(receipt, dict):
        return {"valid": False, "status": "invalid", "reason": "not_a_dict"}
    if receipt.get("unsigned") or not receipt.get("sig"):
        return {"valid": False, "status": "unsigned",
                "reason": "receipt_was_never_signed"}
    if key is None:
        key = os.environ.get(KEY_ENV) or None
    if not key:
        return {"valid": False, "status": "no_key",
                "reason": "verification_key_missing"}
    sig = receipt.get("sig")
    body = dict(receipt)
    body.pop("sig", None)
    want = base64.urlsafe_b64encode(
        hmac.new(key.encode("utf-8"), _canon(body), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(str(sig), want):
        return {"valid": False, "status": "invalid", "reason": "signature_mismatch"}
    return {"valid": True, "status": "verified", "reason": "ok"}


def describe():
    return {"version": VERSION, "alg": "HMAC-SHA256",
            "key_env": KEY_ENV, "unsigned_is_explicit": True}
