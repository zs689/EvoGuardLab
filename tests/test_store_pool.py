# -*- coding: utf-8 -*-
"""EG-010 连接池：复用、有界、异常丢弃，以及 WAL 是否真的可用了。"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                 # noqa: E402


class PooledStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="evo-pool-")
        self.path = os.path.join(self.dir, "a.db")
        self._old = dict(os.environ)
        os.environ["AGENTLAB_STORE_POOL"] = "1"
        os.environ["AGENTLAB_STORE_POOL_SIZE"] = "4"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_pool_off_by_default(self):
        os.environ.pop("AGENTLAB_STORE_POOL", None)
        s = core.Store(self.path)
        try:
            self.assertIsNone(s.pool_stats(), "默认不能改既有连接语义")
        finally:
            s.close()

    def test_connections_are_reused(self):
        s = core.Store(self.path)
        try:
            for i in range(50):
                with s.conn() as c:
                    c.execute("SELECT 1")
            st = s.pool_stats()
            self.assertIsNotNone(st)
            self.assertLess(st["created"], 10,
                            "50 次调用不该开 50 条连接：%s" % st)
            self.assertEqual(s.conn_opened, 51, "含 init() 那一次")
        finally:
            s.close()

    def test_concurrent_writes_all_land(self):
        s = core.Store(self.path)
        errs = []

        def one(i):
            try:
                s.create_experiment("e%d" % i, "order", "A1001",
                                    "none", 0, 7, "D")
            except Exception as exc:        # noqa: BLE001
                errs.append(repr(exc))

        try:
            ts = [threading.Thread(target=one, args=(i,)) for i in range(24)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [])
            self.assertEqual(s.health()["experiments"], 24)
        finally:
            s.close()

    def test_broken_connection_is_discarded(self):
        s = core.Store(self.path)
        try:
            before = s.pool_stats()["discarded"]
            with self.assertRaises(sqlite3.OperationalError):
                with s.conn() as c:
                    c.execute("SELECT * FROM table_that_does_not_exist")
            self.assertEqual(s.pool_stats()["discarded"], before + 1)
            # 池还能继续用，没有把坏连接放回去
            with s.conn() as c:
                self.assertEqual(c.execute("SELECT 1").fetchone()[0], 1)
        finally:
            s.close()

    def test_close_marks_pool_closed(self):
        s = core.Store(self.path)
        s.close()
        self.assertTrue(s.pool_stats()["closed"])

    def test_wal_is_stable_with_persistent_connections(self):
        """上轮撤回 WAL 的根因：最后一个连接关闭会删 -wal/-shm。

        连接池让连接常开，理论上这个竞态就没有了。这里用 24 并发写实测，
        不靠推理下结论。
        """
        s = core.Store(self.path)
        try:
            with s.conn() as c:
                mode = c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
            errs = []

            def one(i):
                try:
                    s.create_experiment("w%d" % i, "order", "A1001",
                                        "none", 0, 7, "D")
                except Exception as exc:    # noqa: BLE001
                    errs.append(repr(exc))

            ts = [threading.Thread(target=one, args=(i,)) for i in range(24)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [],
                             "WAL + 常开连接仍报错：%s" % errs[:2])
            self.assertEqual(s.health()["experiments"], 24)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
