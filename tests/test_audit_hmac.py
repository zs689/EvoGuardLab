# -*- coding: utf-8 -*-
"""FIX-ENT-T2：审计链 HMAC 签名。

威胁模型：持有 DB 写权的攻击者可以读取所有行、按公开算法重算整条
prev_hash/row_hash 链，让朴素的哈希链 verify() 重新通过。HMAC 密钥不落库，
攻击者无法为伪造的行生成有效签名 —— verify() 必须拒绝整链重写。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from enterprise import audit as A                           # noqa: E402

KEY = "enterprise-secret-key-do-not-store-in-db"


class TestAuditHMAC(unittest.TestCase):
    def test_no_key_keeps_legacy_behavior(self):
        """未配置密钥：行为与旧版一致，hmac_sig 为空，verify 仍通过。"""
        s = A.AuditStore()
        try:
            self.assertIsNone(s.hmac_key)
            for i in range(5):
                s.emit({"tenant_id": "t", "request_id": "r%d" % i,
                        "status": 200})
            self.assertEqual(s.version, A.SCHEMA_VERSION)
            ok, info = s.verify()
            self.assertTrue(ok, info)
            self.assertEqual(info["checked"], 5)
        finally:
            s.close()

    def test_with_key_signs_every_row_and_verifies(self):
        s = A.AuditStore(hmac_key=KEY)
        try:
            for i in range(8):
                s.emit({"tenant_id": "t", "request_id": "r%d" % i,
                        "status": 200, "path": "/api/x"})
            ok, info = s.verify()
            self.assertTrue(ok, info)
            self.assertEqual(info["checked"], 8)
            sigs = s._conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE hmac_sig IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(sigs, 8)
        finally:
            s.close()

    def test_tamper_one_row_detected_as_hash_mismatch(self):
        """改业务字段：先撞 hash_mismatch（没有密钥无法重算签名）。"""
        d = tempfile.mkdtemp(prefix="evo-audit-hmac-")
        path = os.path.join(d, "audit.db")
        s = A.AuditStore(path, hmac_key=KEY)
        try:
            for i in range(4):
                s.emit({"tenant_id": "t", "request_id": "r%d" % i,
                        "status": 200})
        finally:
            s.close()
        c = sqlite3.connect(path)
        c.execute("UPDATE audit_events SET status=500 WHERE id=2")
        c.commit()
        c.close()
        s = A.AuditStore(path, hmac_key=KEY)
        try:
            ok, info = s.verify()
            self.assertFalse(ok)
            self.assertEqual(info["reason"], "hash_mismatch")
        finally:
            s.close()

    def test_full_chain_rewrite_still_fails_without_key(self):
        """核心验收：攻击者重算整条链让哈希自洽，verify 仍因签名失败。"""
        d = tempfile.mkdtemp(prefix="evo-audit-rewrite-")
        path = os.path.join(d, "audit.db")
        s = A.AuditStore(path, hmac_key=KEY)
        try:
            for i in range(6):
                s.emit({"tenant_id": "t", "request_id": "r%d" % i,
                        "ts": float(1000 + i), "status": 200})
        finally:
            s.close()

        # 攻击者模拟：拿到 DB 写权，篡改所有 status，并按公开算法重算
        # prev_hash/row_hash 整条链（他能读源码，知道 _canonical）。
        # 但他没有 hmac_key，签名列保持原值 -> verify 必须拒绝。
        c = sqlite3.connect(path)
        rows = c.execute(
            "SELECT id,ts,tenant_id,request_id,actor,method,path,status,"
            "decision,detail,latency_ms FROM audit_events ORDER BY id ASC"
        ).fetchall()
        prev = ""
        for (rid, ts, tenant, req, actor, method, path_, status, decision,
             detail, latency) in rows:
            new_hash = A._canonical(ts, tenant, req, actor, method, path_,
                                    999, decision, detail, latency, prev)
            c.execute("UPDATE audit_events SET status=999, prev_hash=?, "
                      "row_hash=? WHERE id=?", (prev, new_hash, rid))
            prev = new_hash
        c.commit()
        c.close()

        # 先用公开哈希链自检：攻击者确实把链做成了「哈希自洽」
        s_naive = A.AuditStore(path)          # 不带密钥 -> 只看哈希链
        try:
            ok_naive, _ = s_naive.verify()
            self.assertTrue(ok_naive, "模拟的整链重写没有成功，测试前提不成立")
        finally:
            s_naive.close()

        # 带密钥的 verify 必须识破：签名对不上被重算过的 row_hash
        s = A.AuditStore(path, hmac_key=KEY)
        try:
            ok, info = s.verify()
            self.assertFalse(ok, "整链重写后带密钥 verify 竟然通过")
            self.assertEqual(info["reason"], "hmac_mismatch")
        finally:
            s.close()

    def test_key_from_environment(self):
        """环境变量注入路径（生产配置方式）生效。"""
        old = os.environ.get(A.HMAC_KEY_ENV)
        os.environ[A.HMAC_KEY_ENV] = "env-secret"
        try:
            s = A.AuditStore()
            self.assertEqual(s.hmac_key, "env-secret")
            s.emit({"tenant_id": "t", "status": 200})
            self.assertTrue(s.verify()[0])
            s.close()
        finally:
            if old is None:
                os.environ.pop(A.HMAC_KEY_ENV, None)
            else:
                os.environ[A.HMAC_KEY_ENV] = old


if __name__ == "__main__":
    unittest.main()
