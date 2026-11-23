# -*- coding: utf-8 -*-
"""pg_store 的方言翻译与 DSN 解析（纯函数，不需要真 PG 实例）。

这两个函数是「同一份 schema 在两种数据库上跑」的全部转换逻辑，错了就是
参数错位或注入式拼串。原先只有 3 条覆盖，且没有把「字面量/注释里的 ? 不能
换」这条真实缺口锁住。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import pg_store as PG                                      # noqa: E402


class TestParseDsn(unittest.TestCase):
    def test_full_url(self):
        d = PG.parse_dsn("postgresql://u:p@db.example.com:6543/mydb")
        self.assertEqual(d["host"], "db.example.com")
        self.assertEqual(d["port"], 6543)
        self.assertEqual(d["user"], "u")
        self.assertEqual(d["password"], "p")
        self.assertEqual(d["database"], "mydb")

    def test_defaults(self):
        d = PG.parse_dsn("postgresql://u@localhost")
        self.assertEqual(d["port"], 5432)
        self.assertEqual(d["database"], "postgres")

    def test_percent_encoded_password_is_decoded(self):
        d = PG.parse_dsn("postgresql://u:p%40ss%3Aword@h/db")
        self.assertEqual(d["password"], "p@ss:word")

    def test_postgres_scheme_also_accepted(self):
        self.assertEqual(PG.parse_dsn("postgres://u@h/db")["database"], "db")

    def test_bad_scheme_rejected(self):
        for bad in ("mysql://u@h/db", "sqlite:///x.db", "", "h"):
            with self.subTest(bad=bad):
                with self.assertRaises(PG.PGStoreError):
                    PG.parse_dsn(bad)


class TestTranslate(unittest.TestCase):
    def test_placeholder_converted(self):
        self.assertEqual(PG.translate("SELECT * FROM t WHERE a=? AND b=?"),
                         "SELECT * FROM t WHERE a=%s AND b=%s")

    def test_no_placeholder_untouched(self):
        sql = "SELECT 1"
        self.assertEqual(PG.translate(sql), sql)

    def test_question_mark_inside_single_quotes_is_kept(self):
        """'a?b' 里的 ? 不是占位符，换掉会让参数个数对不上。"""
        out = PG.translate("SELECT 'a?b' AS x, ? AS y")
        self.assertEqual(out, "SELECT 'a?b' AS x, %s AS y")

    def test_question_mark_inside_double_quotes_is_kept(self):
        out = PG.translate('SELECT "col?" FROM t WHERE k=?')
        self.assertEqual(out, 'SELECT "col?" FROM t WHERE k=%s')

    def test_doubled_quote_escape_does_not_end_literal(self):
        """SQL 标准 '' 转义：字符串没有结束，后面的 ? 仍属字面量。"""
        out = PG.translate("SELECT 'it''s a ? here', ? ")
        self.assertIn("it''s a ? here", out)
        self.assertTrue(out.rstrip().endswith("%s"))

    def test_line_comment_question_mark_is_kept(self):
        out = PG.translate("SELECT 1 -- what? really\nWHERE a=?")
        self.assertIn("-- what? really", out)
        self.assertIn("WHERE a=%s", out)

    def test_block_comment_question_mark_is_kept(self):
        out = PG.translate("SELECT 1 /* why? */ WHERE a=?")
        self.assertIn("/* why? */", out)
        self.assertIn("a=%s", out)

    def test_autoincrement_stripped(self):
        out = PG.translate("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        self.assertNotIn("AUTOINCREMENT", out.upper())
        self.assertIn("PRIMARY KEY", out)

    def test_unterminated_literal_does_not_crash(self):
        self.assertEqual(PG.translate("SELECT 'oops"), "SELECT 'oops")

    def test_mixed_case_regression_on_real_schema_shape(self):
        sql = ("INSERT INTO runs(id,success) VALUES(?,?)\n"
               "ON CONFLICT(id) DO UPDATE SET success=?")
        out = PG.translate(sql)
        self.assertEqual(out.count("%s"), 3)
        self.assertNotIn("?", out)


class TestDriverBoundary(unittest.TestCase):
    def test_missing_driver_raises_actionable_error(self):
        """没装 pg8000 时必须说清怎么装，而不是 ImportError 裸抛。"""
        try:
            import pg8000  # noqa: F401
            self.skipTest("本机已装 pg8000，跳过缺失路径")
        except ImportError:
            pass
        with self.assertRaises(PG.PGStoreError) as ctx:
            PG._load_driver()
        self.assertIn("pg8000", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
