# -*- coding: utf-8 -*-
"""WS-06：PostgreSQL Row-Level Security 策略（可部署 SQL）+ 租户隔离语义验证。

**边界（务必读）**：本机没有 postgres 二进制、没有 dockerd、Docker Desktop
未安装，所以**无法跑真 PostgreSQL**。这里交付两样本机可验证的东西：

1. `RLS_DDL`：可直接粘到真实 PostgreSQL 14+ 执行的 RLS 策略 SQL
   （按 `app.current_tenant` session GUC 隔离，`FORCE ROW LEVEL SECURITY`，
   owner 也被 RLS 约束——这是 RLS 最常见的漏配点）；
2. `TenantScopedStore`：本机用 SQLite 文件 + 强制 `WHERE tenant_id=?` 实现
   **等价的隔离语义**，供双进程并发压测验证「A 租户读不到 B 租户的行」。
   它验证的是隔离逻辑，不是 PG 本身；写清这点，不假称跑过 PG。

连接真 PG 需要 `psycopg[binary]`（enterprise extra）和 `DATABASE_URL`；
本模块**不 import 任何第三方包**，核心零依赖不变。
"""
import os
import sqlite3
import threading

try:
    from . import enterprise as _ent  # noqa: F401
except ImportError:
    pass

#: 真实 PostgreSQL 部署用：session GUC `app.current_tenant` 由连接池在
#: 每次借出连接时 `SET LOCAL app.current_tenant = '<tenant>'` 设置。
RLS_DDL = """
-- EvoGuardLab multi-tenant RLS (PostgreSQL 14+)
-- 用法：在每个需要隔离的表上启用；事务开始先 SET LOCAL app.current_tenant。
ALTER TABLE runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs        FORCE  ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE  ROW LEVEL SECURITY;
ALTER TABLE skills      ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills      FORCE  ROW LEVEL SECURITY;

-- 策略：只允许读写 current_tenant 匹配的行。
-- FORCE 保证表 owner 也受约束（否则 owner 连接可绕过隔离）。
CREATE POLICY tenant_isolation_runs ON runs
  USING (tenant_id = current_setting('app.current_tenant', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_audit ON audit_events
  USING (tenant_id = current_setting('app.current_tenant', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_skills ON skills
  USING (tenant_id = current_setting('app.current_tenant', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

-- 备用：无 GUC（未 SET）时 current_setting 返回 NULL，所有行都不可见
-- —— fail-closed，绝不因为漏配就把全部租户数据暴露出去。
"""

#: 每种隔离表的最低列要求（部署前自检）
REQUIRED_COLUMNS = {"runs", "audit_events", "skills"}
TENANT_COLUMN = "tenant_id"


class TenantScopedStore:
    """SQLite 上的**强制租户过滤**包装：验证 RLS 等价语义，不是真 PG。

    任何 CRUD 都必须带 tenant_id；不带就抛 ValueError（fail-closed）。
    单连接 + 进程内锁，多进程指向同一文件时用 SQLite 文件锁保证一致性。
    """

    def __init__(self, path=":memory:"):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS scoped_data("
                           "id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
                           "body TEXT)")
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    @staticmethod
    def _check(tenant_id):
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id 必须非空字符串（fail-closed，"
                             "禁止无租户上下文访问）")

    def put(self, tenant_id, row_id, body):
        self._check(tenant_id)
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO scoped_data"
                               "(id,tenant_id,body) VALUES(?,?,?)",
                               (row_id, tenant_id, body))
            self._conn.commit()

    def get(self, tenant_id, row_id):
        self._check(tenant_id)
        with self._lock:
            r = self._conn.execute(
                "SELECT body FROM scoped_data WHERE id=? AND tenant_id=?",
                (row_id, tenant_id)).fetchone()
            return r[0] if r else None

    def list(self, tenant_id):
        """强制带 tenant_id 条件——这是 SQLite 侧对 RLS 的等价约束。"""
        self._check(tenant_id)
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT id FROM scoped_data WHERE tenant_id=?",
                (tenant_id,)).fetchall()]

    def count_all_unsafe(self):
        """仅供压测核对「物理上确实有两租户的行」，生产禁止调用。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM scoped_data").fetchone()[0]


def connection_guc_sql(tenant_id):
    """真实 PG 连接池借出连接时应执行的 SET LOCAL 语句。"""
    if not tenant_id or "'" in str(tenant_id):
        raise ValueError("非法 tenant_id")
    return "SET LOCAL app.current_tenant = '%s'" % tenant_id


def describe():
    return {
        "postgres_available": False,
        "reason": "本机无 postgres 二进制 / dockerd / Docker Desktop（仅 CLI）",
        "deliverables": ["RLS_DDL（可部署 PG14+）",
                         "connection_guc_sql（SET LOCAL 模板）",
                         "TenantScopedStore（SQLite 等价语义，双进程验证）"],
        "not_verified": ["真 PostgreSQL 上的 RLS 行为",
                         "PG 连接池 SET LOCAL 事务绑定",
                         "owner 绕过测试（DDL 里 FORCE 已覆盖，未在真库跑）"],
    }
