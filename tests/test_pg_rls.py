# -*- coding: utf-8 -*-
"""WS-06：PG RLS 可部署 SQL + 租户隔离语义（SQLite 等价，双进程验证）。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import pg_rls as P                                          # noqa: E402


class TestRLSDDL(unittest.TestCase):
    def test_force_rls_present(self):
        """FORCE 是最常漏配的：不加 owner 能绕过隔离。"""
        self.assertIn("FORCE  ROW LEVEL SECURITY", P.RLS_DDL)

    def test_all_three_tables_covered(self):
        for t in P.REQUIRED_COLUMNS:
            self.assertIn("ALTER TABLE %s" % t, P.RLS_DDL)
            self.assertIn("ENABLE ROW LEVEL SECURITY", P.RLS_DDL)
            self.assertIn("tenant_isolation_%s" % {
                "runs": "runs", "audit_events": "audit",
                "skills": "skills"}[t], P.RLS_DDL)

    def test_using_and_with_check_both_present(self):
        self.assertIn("USING (tenant_id =", P.RLS_DDL)
        self.assertIn("WITH CHECK (tenant_id =", P.RLS_DDL)

    def test_no_guc_fails_closed(self):
        # current_setting(..., true) 在未 SET 时返回 NULL -> 无行可见
        self.assertIn("current_setting('app.current_tenant', true)", P.RLS_DDL)
        self.assertIn("fail-closed", P.RLS_DDL)

    def test_guc_sql_escapes_quote(self):
        self.assertIn("SET LOCAL app.current_tenant = 't1'",
                      P.connection_guc_sql("t1"))
        with self.assertRaises(ValueError):
            P.connection_guc_sql("t'1")

    def test_describe_honest_about_no_postgres(self):
        d = P.describe()
        self.assertFalse(d["postgres_available"])
        self.assertTrue(d["not_verified"])


def _rls_probe(tenant, want, path, out_path):
    """模块级函数：Windows spawn 不能 pickle 局部函数。用文件传结果。"""
    import json
    store = P.TenantScopedStore(path)
    result = {"tenant": tenant,
              "own": store.get(tenant, "a1" if tenant == "A" else "b1"),
              "cross": store.get(tenant, "b1" if tenant == "A" else "a1"),
              "list": store.list(tenant), "want": want}
    store.close()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f)


class TestTenantIsolation(unittest.TestCase):
    def test_cross_tenant_read_returns_none(self):
        tmp = tempfile.mkdtemp(prefix="evo-rls-")
        s = P.TenantScopedStore(os.path.join(tmp, "rls.db"))
        s.put("t1", "r1", "tenant1-secret")
        self.assertEqual(s.get("t1", "r1"), "tenant1-secret")
        self.assertIsNone(s.get("t2", "r1"), "跨租户读到了别人的行")
        self.assertEqual(s.list("t2"), [])
        self.assertEqual(s.count_all_unsafe(), 1)  # 物理上确实有 1 行
        s.close()

    def test_no_tenant_id_rejected(self):
        s = P.TenantScopedStore()
        for bad in (None, "", 123):
            with self.assertRaises(ValueError):
                s.get(bad, "r1")
        s.close()

    def test_two_processes_cannot_cross_read(self):
        import multiprocessing as mp

        d = tempfile.mkdtemp(prefix="evo-rls-mp-")
        path = os.path.join(d, "rls.db")
        seed = P.TenantScopedStore(path)
        seed.put("A", "a1", "A-secret")
        seed.put("B", "b1", "B-secret")
        seed.close()

        import json
        outs = [os.path.join(d, "A.json"), os.path.join(d, "B.json")]
        ps = [mp.Process(target=_rls_probe,
                         args=("A", "A-secret", path, outs[0])),
              mp.Process(target=_rls_probe,
                         args=("B", "B-secret", path, outs[1]))]
        for p in ps:
            p.start()
        for p in ps:
            p.join(timeout=30)
        rows = []
        for f in outs:
            with open(f, encoding="utf-8") as fh:
                rows.append(json.load(fh))
        for r in rows:
            self.assertEqual(r["own"], r["want"])
            self.assertIsNone(r["cross"], "跨进程+跨租户读到了别人的行")
            self.assertEqual(len(r["list"]), 1)


if __name__ == "__main__":
    unittest.main()
