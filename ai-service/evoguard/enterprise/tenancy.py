# -*- coding: utf-8 -*-
"""多租户上下文、每租户配额与行级隔离守门。

真实现：从请求头解析租户与主体、按租户独立限流、SQL 必须带租户过滤的守门。
预留接口：`TenantResolver` —— 企业可换成 JWT / SSO / API-key 解析，只要返回
`TenantContext` 即可，其余代码不用改。

**边界（必须写进接入文档）**：
- 这里是**单库行级隔离**，不是物理隔离。SQLite 没有 RLS，靠调用方传
  `tenant_id` 条件 + `require_tenant_filter()` 守门；企业版应换 Postgres RLS
  或每租户独立库。
- `X-Tenant-Id` / `X-Actor` 是**自报头**，只有在反向代理已完成鉴权并覆写它们时
  才可信；`trusted_proxies` 见 `tls_proxy.py`。
"""
import re
import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    actor: str = "anonymous"
    scopes: tuple = ()

    def has_scope(self, scope):
        return scope in self.scopes or "*" in self.scopes


class TenantResolver:
    """解析接口：返回 TenantContext 或 None（None = 拒绝）。"""

    def resolve(self, headers):            # pragma: no cover - 接口
        raise NotImplementedError


class HeaderResolver(TenantResolver):
    """默认实现：读 `X-Tenant-Id` / `X-Actor` / `X-Scopes`。

    仅用于「反代已鉴权并覆写这些头」的部署；直接在公网暴露时必须换掉。
    """

    def __init__(self, default_tenant="default", allowed_tenants=None):
        self.default_tenant = default_tenant
        self.allowed = set(allowed_tenants) if allowed_tenants else None

    def resolve(self, headers):
        raw = (headers.get("X-Tenant-Id") or "").strip()
        tenant = raw or self.default_tenant
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$", tenant):
            return None
        if self.allowed is not None and tenant not in self.allowed:
            return None
        actor = (headers.get("X-Actor") or "anonymous").strip()[:128]
        scopes = tuple(s.strip() for s in
                       (headers.get("X-Scopes") or "").split(",") if s.strip())
        return TenantContext(tenant_id=tenant, actor=actor, scopes=scopes)


@dataclass
class TenantPolicy:
    """每租户策略。

    `request_quota=0` 表示**不限**（由服务层全局限流兜底）——这是默认态，
    避免与全局限流重复计数、也保证既有调用方不被误伤。企业显式配置才收紧。
    """

    tenant_id: str
    request_quota: int = 0
    window_s: float = 60.0
    allowed_tools: tuple = ("kb.search", "order.get_order")
    data_retention_days: int = 30
    max_body_bytes: int = 1 << 20


DEFAULT_POLICY = TenantPolicy(tenant_id="default")


class TenantRegistry:
    def __init__(self, policies=None, unknown_policy=None):
        self._policies = {p.tenant_id: p for p in (policies or [])}
        self._unknown = unknown_policy or DEFAULT_POLICY

    def policy(self, tenant_id):
        """已注册用注册策略；`default` 用默认策略；其它未知租户保守收紧。"""
        p = self._policies.get(tenant_id)
        if p is not None:
            return p
        if tenant_id == self._unknown.tenant_id:
            return self._unknown
        return TenantPolicy(tenant_id=tenant_id,
                            allowed_tools=self._unknown.allowed_tools,
                            request_quota=10,
                            window_s=self._unknown.window_s,
                            data_retention_days=self._unknown.data_retention_days,
                            max_body_bytes=self._unknown.max_body_bytes)

    def allows_tool(self, tenant_id, tool):
        return tool in self.policy(tenant_id).allowed_tools


class TenantQuota:
    """每租户滑动窗口，与全局限流相互独立。线程安全。"""

    def __init__(self, registry, clock=time.monotonic):
        self.registry = registry
        self.clock = clock
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, tenant_id):
        p = self.registry.policy(tenant_id)
        if p.request_quota <= 0:            # 不限：交给服务层全局限流兜底
            return True, 0.0
        now = self.clock()
        with self._lock:
            hits = [t for t in self._hits.get(tenant_id, [])
                    if now - t < p.window_s]
            if len(hits) >= p.request_quota:
                self._hits[tenant_id] = hits
                return False, round(max(0.001, p.window_s - (now - hits[0])), 3)
            hits.append(now)
            self._hits[tenant_id] = hits
            return True, 0.0

    def usage(self, tenant_id):
        p = self.registry.policy(tenant_id)
        now = self.clock()
        with self._lock:
            hits = [t for t in self._hits.get(tenant_id, [])
                    if now - t < p.window_s]
            self._hits[tenant_id] = hits
        return {"used": len(hits), "quota": p.request_quota,
                "window_s": p.window_s}


_TENANT_PREDICATE = re.compile(r"\btenant_id\s*(=|IN)\s*[\(?]", re.IGNORECASE)


def require_tenant_filter(sql, params=()):
    """守门：读写租户数据表的 SQL 必须带 tenant_id 条件。

    这是**开发期护栏**，不是数据库级强制；企业应换 Postgres RLS。
    参数里也必须真的带租户值，否则拒绝（防止只写条件不绑参）。
    """
    if not _TENANT_PREDICATE.search(sql or ""):
        raise ValueError("SQL 缺少 tenant_id 过滤条件（单库行级隔离要求）")
    if not params:
        raise ValueError("SQL 带 tenant_id 条件但未绑定参数")
    return True


def scope_query(base_sql, tenant_id, extra_where=None):
    """给查询追加租户条件；只接受 SELECT，防止误用在 DDL 上。"""
    s = (base_sql or "").strip()
    if not s.lower().startswith("select"):
        raise ValueError("scope_query 只接受 SELECT")
    clause = "tenant_id = ?"
    if extra_where:
        clause += " AND (%s)" % extra_where
    if re.search(r"\bwhere\b", s, re.IGNORECASE):
        return s + " AND " + clause, (tenant_id,)
    return s + " WHERE " + clause, (tenant_id,)
