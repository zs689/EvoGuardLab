# -*- coding: utf-8 -*-
"""FINAL-04-T2：库外独立只追加审计锚点（ExternalAnchorSink）。

最小熵与异地不可抵赖公理：审计链与锚点不能同库。持 DB 写权者可重写
整库并重算哈希链（若同时拿到 HMAC key 更彻底），但只要拿不到**库外**
只追加文件，就无法让 `verify(external_anchor=...)` 通过。

文件格式：JSONL，每行 {"seq":N,"tail_hash":"...","hmac":"...","ts":...}
- `tail_hash`：当时库内链的尾哈希
- `hmac`：用独立密钥（默认取环境变量 EVOGUARD_AUDIT_ANCHOR_KEY）签名，
  防止攻击者伪造锚点文件本身
"""
import hashlib
import hmac as _hmac
import json
import os
import time

KEY_ENV = "EVOGUARD_ANCHOR_HMAC_KEY"


def _key():
    return os.environ.get(KEY_ENV) or os.environ.get("EVOGUARD_AUDIT_HMAC_KEY")


def _sign(payload, key=None):
    k = key or _key()
    if not k:
        return ""                      # 无密钥：锚点仅做一致性核对，不做不可伪造
    return _hmac.new(k.encode("utf-8"), payload.encode("utf-8"),
                     hashlib.sha256).hexdigest()


class ExternalAnchorSink:
    """只追加的库外锚点写入器。文件由 EVOGUARD_AUDIT_ANCHOR_FILE 指定。"""

    def __init__(self, path=None, *, key=None):
        self.path = path or os.environ.get("EVOGUARD_AUDIT_ANCHOR_FILE")
        if not self.path:
            raise ValueError("未指定库外锚点文件（EVOGUARD_AUDIT_ANCHOR_FILE）")
        self.key = key

    def append(self, *, seq, tail_hash):
        rec = {"seq": int(seq), "tail_hash": str(tail_hash or ""),
               "ts": round(time.time(), 3)}
        signed = "%s|%s|%s" % (rec["seq"], rec["tail_hash"], rec["ts"])
        rec["hmac"] = _sign(signed, self.key)
        self._write(rec)
        return rec

    def append_event(self, kind, payload=None):
        """EVO-REBUILD-01-T2：记录一条**库外**只追加事件锚点（如自动回滚）。

        与链尾锚点共用同一文件，但带 `kind`，便于异地取证时区分
        「审计链尾快照」与「自愈事件」。
        """
        rows = _read_all(self.path)
        seq = int(rows[-1].get("seq") or 0) + 1 if rows else 1
        rec = {"seq": seq, "kind": str(kind or "event"),
               "payload": payload if isinstance(payload, dict) else {},
               "ts": round(time.time(), 3)}
        signed = "%s|%s|%s|%s" % (
            rec["seq"], rec["kind"],
            json.dumps(rec["payload"], ensure_ascii=False, sort_keys=True),
            rec["ts"])
        rec["hmac"] = _sign(signed, self.key)
        self._write(rec)
        return rec

    def _write(self, rec):
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    def latest(self):
        rows = read_anchor(self.path)
        return rows[-1] if rows else None


def _read_all(path):
    """读取文件里全部合法 JSON 行（锚点与事件混存）。坏行跳过，不伪造。"""
    rows = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and "seq" in r:
                rows.append(r)
    return rows


def read_anchor(path):
    """读取**链尾锚点**行（含 tail_hash），供 AuditStore.verify 交叉核对。"""
    return [r for r in _read_all(path) if "tail_hash" in r]


def read_events(path, kind=None):
    """读取库外事件锚点行（如 auto_rollback），可选按 kind 过滤。"""
    rows = [r for r in _read_all(path) if r.get("kind")]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows


def _signed_payload(rec):
    """按记录类型重建签名原文（链尾 3 段 / 事件 4 段）。"""
    if rec.get("kind"):
        return "%s|%s|%s|%s" % (
            rec.get("seq"), rec.get("kind"),
            json.dumps(rec.get("payload") or {}, ensure_ascii=False,
                       sort_keys=True), rec.get("ts"))
    return "%s|%s|%s" % (rec.get("seq"), rec.get("tail_hash"),
                         rec.get("ts"))


def verify_anchor(rec, *, path=None, key=None):
    """校验单条锚点：结构完整性 + （有密钥时）HMAC 防伪。

    无密钥时**不能**无脑通过：至少要核对 seq/ts 存在且类型对，
    并且链尾行必须有 tail_hash、事件行必须有 kind —— 否则
    "存在性/一致性核对"只是注释里的谎话。
    """
    if not isinstance(rec, dict):
        return False
    seq, ts = rec.get("seq"), rec.get("ts")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return False
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    is_event = bool(rec.get("kind"))
    if is_event:
        if not isinstance(rec.get("kind"), str) or not rec["kind"]:
            return False
        if not isinstance(rec.get("payload"), dict):
            return False
    else:
        if not isinstance(rec.get("tail_hash"), str) or not rec["tail_hash"]:
            return False
    need = key or _key()
    if not need:
        return True                     # 无密钥：只做上面的真实结构核对
    signed = _signed_payload(rec)
    want = _sign(signed, key)
    return bool(rec.get("hmac")) and _hmac.compare_digest(
        str(rec.get("hmac")), want)
