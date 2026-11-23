# -*- coding: utf-8 -*-
"""pg_rls 的交付 SQL 验证。

`RLS_DDL_IF_EXISTS` 在文档里说"已加幂等版"，但全仓没有任何执行路径 ——
它是一段给部署方粘贴/由 initdb 执行的 SQL 载荷（像序列化载荷一样，源码里
零引用不等于没用）。但**从来没被验证过**：里面少一条 FORCE、策略条件写反、
或者重复执行报错，都会让真库上的租户隔离形同虚设。

这里做两件事：
1. 纯文本校验关键语句（无需 PG，CI 常驻）；
2. 本机有真 PG 时，真跑两次幂等版（docker exec psql），确认能重复应用。
"""
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import pg_rls as PR                                        # noqa: E402


class TestDeliverableSQL(unittest.TestCase):
    def test_rls_ddl_enables_and_forces_all_tables(self):
        for sql in (PR.RLS_DDL, PR.RLS_DDL_IF_EXISTS):
            upper = re.sub(r"\s+", " ", sql.upper())   # SQL 里空格不固定
            for table in ("RUNS", "AUDIT_EVENTS", "SKILLS"):
                self.assertIn(table, upper,
                              "%s 漏了表 %s" % ("RLS_DDL" if sql == PR.RLS_DDL
                                                else "IF_EXISTS", table))
            self.assertIn("ENABLE ROW LEVEL SECURITY", upper)
            self.assertIn("FORCE ROW LEVEL SECURITY", upper,
                          "少了 FORCE：表 owner 会绕过 RLS")

    def test_idempotent_version_guards_missing_tables(self):
        upper = PR.RLS_DDL_IF_EXISTS.upper()
        self.assertIn("TO_REGCLASS", upper, "必须容忍应用表尚未创建")
        self.assertIn("DROP POLICY IF EXISTS", upper,
                      "重复执行前要先删旧策略，否则 CREATE POLICY 冲突")
        self.assertIn("CREATE POLICY", upper)

    def test_guc_sql_binds_tenant(self):
        sql = PR.connection_guc_sql("tenant-a")
        self.assertIn("tenant-a", sql)
        self.assertIn("SET LOCAL", sql.upper(),
                      "必须在事务内 SET LOCAL，避免跨请求串租户")

    def test_fail_closed_when_no_guc(self):
        """未 SET GUC 时 current_setting 返回 NULL -> 所有行不可见。"""
        upper = re.sub(r"\s+", " ", PR.RLS_DDL.upper())
        # current_setting(..., true) 在 GUC 未设时返回 NULL -> 零行可见
        self.assertIn("CURRENT_SETTING('APP.CURRENT_TENANT', TRUE)", upper,
                      "需要 fail-closed 的 GUC 读取")
        for clause in re.findall(r"USING \(([^)]*)\)", upper):
            self.assertIn("TENANT_ID", clause,
                          "USING 不能无条件放行：%s" % clause)

    def test_required_columns_declared(self):
        self.assertTrue({"runs", "audit_events", "skills"}
                        <= PR.REQUIRED_COLUMNS)
        self.assertEqual(PR.TENANT_COLUMN, "tenant_id")


@unittest.skipUnless(shutil.which("docker"), "无 docker CLI")
class TestRealPostgresIdempotency(unittest.TestCase):
    """本机有 PG 容器时真跑两遍；没有就明确 skip，不伪造通过。"""

    @classmethod
    def setUpClass(cls):
        try:
            r = subprocess.run(
                ["docker", "exec", "evoguard-pg", "pg_isready", "-U",
                 "postgres"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise unittest.SkipTest("evoguard-pg 容器未运行")
        except (subprocess.SubprocessError, OSError) as exc:
            raise unittest.SkipTest(str(exc)) from exc

    def test_apply_twice_is_idempotent(self):
        for i in range(2):
            p = subprocess.run(
                ["docker", "exec", "-i", "evoguard-pg", "psql", "-U",
                 "postgres", "-d", "evoguard", "-v", "ON_ERROR_STOP=1"],
                input=PR.RLS_DDL_IF_EXISTS, capture_output=True,
                text=True, timeout=30)
            self.assertEqual(p.returncode, 0,
                             "第 %d 次应用幂等 DDL 失败：%s"
                             % (i + 1, p.stderr[-300:]))


if __name__ == "__main__":
    unittest.main()
