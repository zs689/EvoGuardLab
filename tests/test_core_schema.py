# -*- coding: utf-8 -*-
"""core_schema 的独立测试：DDL 完整性。

schema 是**所有持久化的地基**。表漏一张，某个功能就会在运行时才发现
"no such table"；索引漏一条，数据量上来才暴露慢查询；而一旦在升级路径里
混入 DROP，旧库数据就没了。这些都不该等线上才发现。
"""
import os
import re
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from core_schema import SCHEMA                             # noqa: E402


def _tables():
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", SCHEMA))


def _indexes():
    return re.findall(r"CREATE INDEX IF NOT EXISTS\s+(\w+)\s+ON\s+(\w+)", SCHEMA)


class TestSchemaCompleteness(unittest.TestCase):
    REQUIRED = {"experiments", "runs", "steps", "checkpoints", "faults",
                "diagnoses", "recoveries", "events", "evals",
                "agent_sessions", "agent_plans", "synthesized_tools"}

    def test_all_domain_tables_declared(self):
        missing = self.REQUIRED - _tables()
        self.assertEqual(missing, set(), "缺表：%s" % (missing,))

    def test_every_table_has_primary_key(self):
        blocks = re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(([\s\S]*?)\);", SCHEMA)
        no_pk = [name for name, body in blocks if "PRIMARY KEY" not in body.upper()]
        self.assertEqual(no_pk, [], "这些表没有主键：%s" % (no_pk,))

    def test_every_index_targets_existing_table(self):
        bad = [(idx, tbl) for idx, tbl in _indexes() if tbl not in _tables()]
        self.assertEqual(bad, [], "索引指向不存在的表：%s" % (bad,))

    def test_index_names_unique(self):
        names = [i for i, _t in _indexes()]
        self.assertEqual(len(names), len(set(names)), "索引名重复")

    def test_no_destructive_ddl(self):
        """升级路径只允许 IF NOT EXISTS 追加，不允许 DROP 既有数据。"""
        upper = SCHEMA.upper()
        self.assertNotIn("DROP TABLE", upper)
        self.assertNotIn("DELETE FROM", upper)
        self.assertNotIn("ALTER TABLE", upper)

    def test_schema_is_idempotent(self):
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(SCHEMA)
            c.executescript(SCHEMA)      # 第二次必须无异常
        finally:
            c.close()

    def test_schema_creates_expected_columns(self):
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
            self.assertTrue({"id", "status", "success", "seed"} <= cols,
                            "runs 关键列缺失：%s" % (cols,))
        finally:
            c.close()

    def test_all_ddl_uses_if_not_exists(self):
        """升级路径上的每条 DDL 都必须幂等（旧库已有同名表不能报错）。"""
        stmts = [s.strip() for s in SCHEMA.split(";") if s.strip()]
        non_idempotent = [s[:60] for s in stmts
                          if s.upper().startswith(("CREATE TABLE", "CREATE INDEX"))
                          and "IF NOT EXISTS" not in s.upper()]
        self.assertEqual(non_idempotent, [],
                         "非幂等 DDL：%s" % (non_idempotent,))

    def test_runs_baseline_columns(self):
        """基础 DDL 只需建出最小可用 runs；其余列由迁移测试单独覆盖。"""
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
            self.assertTrue({"id", "experiment_id", "mode", "status"} <= cols,
                            "runs 基础列缺失：%s" % (cols,))
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
