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
            self.assertGreaterEqual(
                s.conn_opened, 51,
                "至少含 init() 那一次；开 WAL 时会多一次 PRAGMA")
            self.assertLessEqual(
                s.conn_opened, 53,
                "不该凭空多开一堆连接：%s" % st)
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

    def test_pool_only_concurrent_writes_are_stable(self):
        """只开连接池（delete 日志）24 并发写必须 0 错误。

        WAL 版本不放在单元测试里：实测单进程 24 并发就有约 1/8 概率
        报 `readonly`，放进套件会让全量回归变成 flaky 假绿。那个风险
        由 scripts/store_pool_stability.py 单独量失败率并如实判红。
        """
        s = core.Store(self.path)
        errs = []

        def one(i):
            try:
                s.create_experiment("p%d" % i, "order", "A1001",
                                    "none", 0, 7, "D")
            except Exception as exc:        # noqa: BLE001
                errs.append(repr(exc))

        try:
            with s.conn() as c:
                self.assertNotEqual(
                    str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                    "wal", "本用例不能开 WAL")
            ts = [threading.Thread(target=one, args=(i,)) for i in range(24)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [])
            self.assertEqual(s.health()["experiments"], 24)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
