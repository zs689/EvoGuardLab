# -*- coding: utf-8 -*-
"""FINAL-04-T2：库外独立审计锚点的红/绿测试。

红队反例：持 DB 写权者删空审计库、伪造一致哈希链后，
带 external_anchor 的 verify() 必须报 external_anchor_mismatch。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from enterprise.audit import AuditStore                     # noqa: E402
import external_anchor as EA                                # noqa: E402


def _event(i):
    return {"ts": 1000.0 + i, "tenant_id": "t1", "request_id": "r%d" % i,
            "actor": "a", "method": "GET", "path": "/x", "status": 200,
            "decision": "allowed", "detail": {"n": i}}


class TestExternalAnchor(unittest.TestCase):
    def test_append_and_read(self):
        d = tempfile.mkdtemp(prefix="anchor-")
        f = os.path.join(d, "anchor.jsonl")
        sink = EA.ExternalAnchorSink(f, key="test-key")
        sink.append(seq=1, tail_hash="abc123")
        sink.append(seq=2, tail_hash="def456")
        rows = EA.read_anchor(f)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["tail_hash"], "def456")
        self.assertTrue(rows[-1]["hmac"])

    def test_verify_ok_with_matching_anchor(self):
        d = tempfile.mkdtemp(prefix="anchor-")
        af = os.path.join(d, "anchor.jsonl")
        store = AuditStore(os.path.join(d, "audit.db"), hmac_key="secret")
        sink = EA.ExternalAnchorSink(af)
        for i in range(4):
            store.emit(_event(i))
            ok, info = store.verify()
            sink.append(seq=info["checked"], tail_hash=info.get("tail_hash") or
                        _tail(store))
        last = EA.read_anchor(af)[-1]
        ok, info = store.verify(external_anchor=last)
        self.assertTrue(ok, info)

    def test_rewritten_db_detected_by_external_anchor(self):
        """红队：删库重写一致链后，库外锚点仍能抓到。"""
        d = tempfile.mkdtemp(prefix="anchor-")
        af = os.path.join(d, "anchor.jsonl")
        db = os.path.join(d, "audit.db")
        store = AuditStore(db, hmac_key="secret")
        sink = EA.ExternalAnchorSink(af)
        for i in range(4):
            store.emit(_event(i))
        tail = _tail(store)
        sink.append(seq=4, tail_hash=tail)

        # 攻击者持 DB 写权：删空表 + 重新插入 2 行（伪造自洽链）
        store.close()
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM audit_events")
        conn.commit()
        conn.close()
        store2 = AuditStore(db, hmac_key="secret")
        for i in range(2):
            store2.emit(_event(i))
        # 库内链自洽（无外部锚点时 verify 通过）
        ok_internal, _ = store2.verify()
        self.assertTrue(ok_internal)
        # 但库外锚点 seq=4 与当前链尾 seq=2 不符 -> 必须报错
        last = EA.read_anchor(af)[-1]
        ok, info = store2.verify(external_anchor=last)
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "external_anchor_mismatch")


def _tail(store):
    row = store._conn.execute(
        "SELECT row_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else ""


if __name__ == "__main__":
    unittest.main()
