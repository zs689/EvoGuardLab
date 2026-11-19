# -*- coding: utf-8 -*-
"""EVO-LOOP-01-T2：旧库幂等补 runs.skill_id 列。

`CREATE TABLE IF NOT EXISTS runs(...)` 对已存在的旧表整表跳过，不会补新列。
Store.init() 必须幂等 ALTER 补列，否则 _record_skill_id 写归因时
no such column 被静默吞掉，精确归因在升级后的旧库永久失效。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                  # noqa: E402
import runtime                                               # noqa: E402


def _old_style_db(path):
    """造一个 skill_id 引入之前的 runs 表，带一行历史数据。"""
    c = sqlite3.connect(path)
    try:
        c.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, started_at REAL, "
                  "success INTEGER)")
        c.execute("INSERT INTO runs VALUES('r-old', 100.0, 1)")
        c.commit()
    finally:
        c.close()


class TestRunsSkillIdMigration(unittest.TestCase):
    def test_old_db_gets_skill_id_column_without_data_loss(self):
        d = tempfile.mkdtemp(prefix="runs-migrate-")
        path = os.path.join(d, "old.db")
        _old_style_db(path)

        # 打开即触发 init() 幂等迁移
        s = core.Store(path)
        try:
            with s.conn() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
                self.assertIn("skill_id", cols, "旧库未补上 skill_id 列")
                row = c.execute(
                    "SELECT success FROM runs WHERE id='r-old'").fetchone()
            self.assertEqual(row["success"], 1, "迁移破坏了历史数据")
        finally:
            s.close()

    def test_migration_is_idempotent(self):
        d = tempfile.mkdtemp(prefix="runs-migrate-idem-")
        path = os.path.join(d, "old.db")
        _old_style_db(path)

        core.Store(path).close()          # 第一次：补列
        s = core.Store(path)              # 第二次：不重复 ALTER，不报错
        try:
            with s.conn() as c:
                cols = [r[1] for r in c.execute("PRAGMA table_info(runs)")]
            self.assertEqual(cols.count("skill_id"), 1,
                             "skill_id 列被重复添加：%s" % cols)
        finally:
            s.close()

    def test_skill_id_can_be_written_on_migrated_old_db(self):
        """迁移后 _record_skill_id 必须能真正写入（不再静默 no such column）。"""
        d = tempfile.mkdtemp(prefix="runs-migrate-write-")
        path = os.path.join(d, "old.db")
        _old_style_db(path)
        s = core.Store(path)
        try:
            runtime._record_skill_id(s, "r-old", "skill-xyz")
            with s.conn() as c:
                got = c.execute(
                    "SELECT skill_id FROM runs WHERE id='r-old'").fetchone()
            self.assertEqual(got["skill_id"], "skill-xyz")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
