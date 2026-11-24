# -*- coding: utf-8 -*-
"""哈希链的**真实**防篡改边界：整链重写能否被检出。

只改一行不改后续 -> 没有 HMAC 也能被 hash 链抓到（prev_mismatch）。
但伪造者若把所有行的 prev_hash/row_hash 一起重算（保持链自洽）：
  - 没有 HMAC：verify 通过 —— **伪造成功**
  - 配置 HMAC：verify 失败（hmac_mismatch）—— 检出
这条测试把「有/无 HMAC」的差别钉死，防止以后有人以为默认配置就防得住。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from enterprise import audit as AU                          # noqa: E402


def _make(with_key, n=4):
    p = os.path.join(tempfile.mkdtemp(prefix="audit-"), "a.db")
    st = AU.AuditStore(path=p, hmac_key="secret-key" if with_key else None)
    for i in range(n):
        st.emit({"tenant_id": "acme", "actor": "u%d" % i, "method": "POST",
                 "path": "/api/run", "status": 200, "decision": "allow",
                 "detail": {"i": i}})
    return st


def _rewrite_chain(st, target_id=2):
    """模拟「有 DB 写权但没有 HMAC key」的伪造者：整链重算，签名不动。"""
    conn = st._conn
    cols = [c[1] for c in conn.execute("PRAGMA table_info(audit_events)")]
    rows = [dict(zip(cols, r)) for r in
            conn.execute("SELECT * FROM audit_events ORDER BY id")]
    prev = ""
    for r in rows:
        dec = "deny" if r["id"] == target_id else r["decision"]
        det = (json.dumps({"i": 999}, ensure_ascii=False, sort_keys=True)
               if r["id"] == target_id else r["detail"])
        h = AU._canonical(r["ts"], r["tenant_id"], r["request_id"], r["actor"],
                          r["method"], r["path"], r["status"], dec, det,
                          r["latency_ms"], prev)
        conn.execute("UPDATE audit_events SET decision=?,detail=?,prev_hash=?,"
                     "row_hash=? WHERE id=?", (dec, det, prev, h, r["id"]))
        prev = h
    conn.commit()
    return rows


class TestChainRewrite(unittest.TestCase):
    def test_without_hmac_full_rewrite_goes_undetected(self):
        """诚实记录已知弱点：默认配置下整链重写能骗过 verify。"""
        st = _make(with_key=False)
        ok, _ = st.verify()
        self.assertTrue(ok, "初始链应当自洽")
        _rewrite_chain(st)
        ok2, det = st.verify()
        self.assertTrue(ok2,
                        "无 HMAC 时整链重写本应无法检出（若这里失败，说明"
                        "防篡改能力有变化，请同步更新文档）：%s" % (det,))

    def test_with_hmac_full_rewrite_is_detected(self):
        st = _make(with_key=True)
        ok, _ = st.verify()
        self.assertTrue(ok)
        _rewrite_chain(st)
        ok2, det = st.verify()
        self.assertFalse(ok2, "配置 HMAC 后整链重写必须被检出")
        self.assertEqual(det.get("reason"), "hmac_mismatch", det)

    def test_single_row_tamper_detected_even_without_hmac(self):
        """只改一行不同步后续：hash 链本身就能抓（证明链不是摆设）。"""
        st = _make(with_key=False)
        conn = st._conn
        conn.execute("UPDATE audit_events SET decision='deny' WHERE id=2")
        conn.commit()
        ok, det = st.verify()
        self.assertFalse(ok)
        self.assertIn(det.get("reason"),
                      ("hash_mismatch", "row_mismatch", "prev_mismatch"), det)


if __name__ == "__main__":
    unittest.main()
