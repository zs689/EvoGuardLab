# -*- coding: utf-8 -*-
"""创新点：SQL 感知的写串行化（读写分离，默认开、可关）。"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core          # noqa: E402


class FakeGate:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1


def _mem():
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE t(a INTEGER)")
    raw.commit()
    return raw


class TestWriteGuardConn(unittest.TestCase):
    def test_read_does_not_take_lock(self):
        raw, gate = _mem(), FakeGate()
        c = core._WriteGuardConn(raw, gate)
        c.execute("SELECT * FROM t").fetchall()
        self.assertEqual(gate.acquired, 0, "读路径必须完全不加锁")
        c.commit()
        self.assertEqual(gate.released, 0)

    def test_write_takes_lock_once_per_transaction(self):
        raw, gate = _mem(), FakeGate()
        c = core._WriteGuardConn(raw, gate)
        c.execute("INSERT INTO t VALUES(1)")
        c.execute("INSERT INTO t VALUES(2)")
        self.assertEqual(gate.acquired, 1, "同一事务只拿一次锁")
        c.commit()
        self.assertEqual(gate.released, 1)

    def test_rollback_releases_lock(self):
        raw, gate = _mem(), FakeGate()
        c = core._WriteGuardConn(raw, gate)
        c.execute("INSERT INTO t VALUES(1)")
        c.rollback()
        self.assertEqual(gate.released, 1, "异常路径也必须放锁")

    def test_executescript_is_treated_as_write(self):
        raw, gate = _mem(), FakeGate()
        c = core._WriteGuardConn(raw, gate)
        c.executescript("CREATE TABLE IF NOT EXISTS t2(a)")
        self.assertEqual(gate.acquired, 1)
        c.commit()
        self.assertEqual(gate.released, 1)

    def test_attribute_forwarding(self):
        raw, gate = _mem(), FakeGate()
        c = core._WriteGuardConn(raw, gate)
        c.row_factory = sqlite3.Row
        self.assertIs(raw.row_factory, sqlite3.Row)
        self.assertIsNotNone(c.execute("SELECT 1").fetchone())
        c.commit()
        c.close()


class TestSerializedWrites(unittest.TestCase):
    def setUp(self):
        self._old = dict(os.environ)
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_enabled_by_default_and_can_be_disabled(self):
        # 显式清掉外部 env，测的是「没有任何配置时的默认值」
        os.environ.pop("AGENTLAB_STORE_SERIALIZE_WRITES", None)
        s = core.Store(self.path)
        self.assertTrue(s._serialize_writes)
        s.close()
        os.environ["AGENTLAB_STORE_SERIALIZE_WRITES"] = "0"
        s2 = core.Store(self.path)
        self.assertFalse(s2._serialize_writes)
        self.assertIsNone(s2.write_gate_stats())
        s2.close()

    def test_concurrent_writes_have_no_locked_errors(self):
        os.environ["AGENTLAB_STORE_SERIALIZE_WRITES"] = "1"
        s = core.Store(self.path)
        errs, total = [], [0]
        lock = threading.Lock()

        def worker(k):
            for i in range(200):
                try:
                    s.create_experiment("s%d_%d" % (k, i), "order", "A1001",
                                        "none", 0, 7, "D")
                    with lock:
                        total[0] += 1
                except Exception as exc:        # noqa: BLE001
                    errs.append(repr(exc))

        try:
            ts = [threading.Thread(target=worker, args=(k,))
                  for k in range(24)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [], "写串行化后不该再有 locked：%s"
                             % errs[:2])
            self.assertEqual(total[0], 4800)
            self.assertGreater(s.write_gate_stats()["acquired"], 0)
        finally:
            s.close()

    def test_pool_returns_raw_connection_not_nested_proxy(self):
        os.environ["AGENTLAB_STORE_POOL"] = "1"
        s = core.Store(self.path)
        try:
            with s.conn() as c:
                c.execute("INSERT INTO experiments(id,name) VALUES('a','b')")
            with s.conn() as c2:
                raw2 = getattr(c2, "_raw", c2)
                self.assertIsInstance(
                    raw2, sqlite3.Connection,
                    "池里必须是原始连接，不能嵌套代理")
                if s._serialize_writes:
                    self.assertIsInstance(c2, core._WriteGuardConn)
        finally:
            s.close()

    def test_fast_mode_skips_serialization(self):
        s = core.Store(self.path, fast=True)
        try:
            self.assertFalse(s._serialize_writes,
                             "单线程离线评测不需要写锁")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
