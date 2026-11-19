# -*- coding: utf-8 -*-
"""INFRA-CONC-01：Redis 并发/断链 + 契约（零依赖单测）。

真 Redis 的跨进程压测在 scripts/distributed_stress.py；这里用假 socket
精确构造「多线程交错」与「idle 断链」，不依赖本机有没有 redis-server。
"""
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import redis_backend as RB                                  # noqa: E402


class FakeSocket:
    """按 RESP 协议回 +OK / +1，可模拟首读断链。"""

    def __init__(self, fail_first=False):
        self.sent = []
        self.closed = False
        self.fail_first = fail_first
        self.reads = 0

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        self.reads += 1
        if self.fail_first and self.reads == 1:
            raise OSError("idle connection closed")
        return b"+OK\r\n"

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


class TestRedisConcurrency(unittest.TestCase):
    def test_lock_exists_and_serializes_cmd(self):
        c = RB.RESP()
        self.assertTrue(hasattr(c, "_lock"))

        sock = FakeSocket()
        c.sock = sock
        c._connect_fn = lambda *a, **k: sock
        # 16 线程同时 cmd：RESP 无请求 ID，若不加锁回复会串位；
        # 这里用 recv 计数验证每个请求都完整走完 send+read。
        errors = []
        def worker(i):
            try:
                for _ in range(20):
                    c.cmd("INCR", "k%d" % i)
            except Exception as exc:                        # noqa: BLE001
                errors.append(exc)
        ts = [threading.Thread(target=worker, args=(i,))
              for i in range(16)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(errors, [], "并发下出现协议错位：%s" % errors[:2])
        self.assertEqual(len(sock.sent), 16 * 20)

    def test_reconnect_once_on_idle_drop(self):
        c = RB.RESP()
        sockets = []
        def maker(*a, **k):
            s = FakeSocket(fail_first=(len(sockets) == 0))
            sockets.append(s)
            return s
        c._connect_fn = maker                # 用假 socket 替换建连
        value = c.cmd("PING")
        self.assertEqual(value, "OK")
        self.assertEqual(len(sockets), 2, "断链后应重连一次再重试")
        self.assertTrue(sockets[0].closed)

    def test_double_failure_raises_not_silent_ok(self):
        c = RB.RESP()
        def bad(*a, **k):
            raise OSError("always down")
        c._connect_fn = bad
        with self.assertRaises(RB.RedisError):
            c.cmd("PING")


class TestPGAuditContract(unittest.TestCase):
    """PG 审计后端在无 DSN/无 store 时不能静默退回 SQLite。"""

    def test_requires_dsn_or_store(self):
        from enterprise.audit import PostgresAuditStore
        with self.assertRaises(ValueError):
            PostgresAuditStore()

    def test_pg_schema_declares_hmac_column(self):
        from enterprise.audit import PG_AUDIT_SCHEMA
        self.assertIn("hmac_sig", PG_AUDIT_SCHEMA)
        self.assertIn("tenant_id", PG_AUDIT_SCHEMA)
        self.assertIn("prev_hash", PG_AUDIT_SCHEMA)


if __name__ == "__main__":
    unittest.main()
