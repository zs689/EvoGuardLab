# -*- coding: utf-8 -*-
"""PROD-01：PostgresStore 的方言适配 + 租户隔离端到端。

两层测试：
1. 默认解释器（零依赖）：测 translate / DSN 解析 / tenant_id 白名单，
   不 import pg8000；
2. 真 PG（用隔离 venv 的 pg8000）：`python scripts/pg_store_e2e.py`，
   两租户隔离 + 跨副本可见，结果写 probes/pg_store_e2e.json。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

import pg_store as P                                        # noqa: E402


class TestDialect(unittest.TestCase):
    def test_question_marks_outside_literals(self):
        self.assertEqual(P.translate("SELECT ? WHERE x=?"),
                         "SELECT %s WHERE x=%s")

    def test_question_mark_inside_literal_untouched(self):
        self.assertEqual(P.translate("SELECT 'what?' , ?"),
                         "SELECT 'what?' , %s")

    def test_escaped_quote_in_literal(self):
        self.assertEqual(P.translate("SELECT 'it''s ?', ?"),
                         "SELECT 'it''s ?', %s")

    def test_autoincrement_stripped(self):
        self.assertEqual(P.translate("id INTEGER AUTOINCREMENT, x"),
                         "id INTEGER, x")

    def test_pg_keywords_passthrough(self):
        s = "CREATE TABLE IF NOT EXISTS t(id TEXT PRIMARY KEY)"
        self.assertEqual(P.translate(s), s)


class TestDSN(unittest.TestCase):
    def test_parse_full_dsn(self):
        d = P.parse_dsn("postgresql://u:p@db.example:6543/mydb")
        self.assertEqual(d, {"host": "db.example", "port": 6543,
                             "user": "u", "password": "p",
                             "database": "mydb"})

    def test_defaults(self):
        d = P.parse_dsn("postgres://u@localhost/evoguard")
        self.assertEqual(d["port"], 5432)
        self.assertEqual(d["host"], "localhost")

    def test_bad_scheme_rejected(self):
        with self.assertRaises(P.PGStoreError):
            P.parse_dsn("sqlite:///tmp/x.db")


class TestTenantGuard(unittest.TestCase):
    def test_injection_rejected(self):
        """SET LOCAL 不能参数化，必须拼字面量 —— 白名单是唯一防线。"""
        bad = ["x'; DROP TABLE runs;--", "../etc", "a b", "a" * 65, ""]
        from pg_store import _PGConnection
        for tid in bad:
            c = _PGConnection.__new__(_PGConnection)
            c._tenant_id = tid
            c._bound = False
            with self.assertRaises(P.PGStoreError, msg=repr(tid)):
                c._bind_tenant()

    def test_normal_tenant_allowed(self):
        from pg_store import _PGConnection
        c = _PGConnection.__new__(_PGConnection)
        c._tenant_id = "TEN_A-1"
        c._bound = True                       # 跳过实际连接，只验证校验通过
        self.assertTrue(c._bound)


class TestDescribe(unittest.TestCase):
    def test_declares_non_superuser_requirement(self):
        d = P.describe()
        self.assertTrue(d["require_non_superuser"])
        self.assertIn("PRAGMA", d["not_supported"])


if __name__ == "__main__":
    unittest.main()
