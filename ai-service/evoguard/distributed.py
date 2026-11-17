# -*- coding: utf-8 -*-
"""WS-06：分布式底座的**接口 + 零依赖默认实现**。

核心约束（来自升级方案 global_constraints）：
  核心库零第三方运行时依赖；Redis/PG **可选注入**，不装也能跑通。

这里提供：
1. `RateLimiter` 协议 + `MemoryLimiter`（行为与 service_controls 一致）
   + `SQLiteLimiter`（跨进程共享，标准库 sqlite3）；
2. `CircuitBreakerState`（可注入共享后端，默认内存）；
3. `TenantResolver`：Bearer JWT 的**验签与声明提取接口**——
   不自带 JWT 库；传 verifier 回调，未配置时明确拒绝（fail-closed）；
4. `WebhookApprovalNotifier`：异步审批通知 + 幂等回执，纯 urllib 标准库。

**边界（诚实）**：这里只做到「接口 + 默认实现 + 可选后端骨架」。
真正的 RedisLimiter / PostgresStore / OIDC discovery / Docker Compose 压测
需要外部服务，本机没有，不假称完成。未实现的部分在 `describe()` 里
明确列 `not_implemented`，不静默当成可用。
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque

try:
    from . import service_controls as _sc
except ImportError:
    import service_controls as _sc                          # type: ignore


class RateLimiter:
    """协议：check(actor, limit, window, now) -> (ok, retry_after)。"""

    def check(self, actor, limit, window, now=None):
        raise NotImplementedError

    def kind(self):
        return type(self).__name__


class MemoryLimiter(RateLimiter):
    """单进程滑动窗口（与 service_controls.check_rate 等价）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = defaultdict(deque)

    def check(self, actor, limit, window, now=None):
        now = time.monotonic() if now is None else now
        cutoff = now - window
        with self._lock:
            for k in list(self._hits):
                dq = self._hits[k]
                while dq and dq[0] <= cutoff:
                    dq.popleft()
                if not dq:
                    del self._hits[k]
            dq = self._hits[actor]
            if len(dq) >= limit:
                return False, max(0.0, dq[0] + window - now)
            dq.append(now)
            return True, 0.0


class SQLiteLimiter(RateLimiter):
    """跨进程共享的滑动窗口；SQLite WAL 支持多读单写。

    默认库路径 ``:memory:``（测试用）；生产传一个共享文件路径，
    多个 API 副本指向同一文件即可共享配额（单机多进程有效，
    跨主机仍需 Redis —— 见 describe() 的 not_implemented）。
    """

    def __init__(self, path=":memory:"):
        self.path = path
        self._lock = threading.Lock()
        # 单个长连接：:memory: 每次新建都是空库；同时避免连接泄漏。
        self._conn = sqlite3.connect(self.path, timeout=5,
                                     check_same_thread=False)
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS rl_hits("
                           "actor TEXT, ts REAL, id INTEGER PRIMARY KEY "
                           "AUTOINCREMENT)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS rl_actor_ts "
                           "ON rl_hits(actor, ts)")
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def check(self, actor, limit, window, now=None):
        now = time.time() if now is None else now
        cutoff = now - window
        with self._lock:
            c = self._conn
            c.execute("DELETE FROM rl_hits WHERE ts <= ?", (cutoff,))
            n = c.execute("SELECT COUNT(*) FROM rl_hits WHERE actor=?",
                          (actor,)).fetchone()[0]
            if n >= limit:
                oldest = c.execute(
                    "SELECT MIN(ts) FROM rl_hits WHERE actor=?",
                    (actor,)).fetchone()[0]
                c.commit()
                return False, max(0.0, (oldest or now) + window - now)
            c.execute("INSERT INTO rl_hits(actor,ts) VALUES(?,?)",
                      (actor, now))
            c.commit()
        return True, 0.0


class CircuitBreakerState:
    """可注入共享后端的熔断器状态；未注入后端时进程内（默认行为不变）。"""

    def __init__(self, backend=None):
        self.backend = backend or {}
        self._lock = threading.Lock()

    def _snapshot_unlocked(self, key):
        """内部读取：调用方已持锁，**绝不能**再拿一次锁（会自死锁）。"""
        return dict(self.backend.get(key) or
                    {"failures": 0, "open_until": 0.0})

    def snapshot(self, key):
        with self._lock:
            return self._snapshot_unlocked(key)

    def record(self, key, failure, now=None, threshold=5, cooldown=30.0):
        now = time.monotonic() if now is None else now
        with self._lock:
            s = self._snapshot_unlocked(key)
            if failure:
                s["failures"] += 1
                if s["failures"] >= threshold:
                    s["open_until"] = now + cooldown
            else:
                s["failures"] = 0
                s["open_until"] = 0.0
            self.backend[key] = s
            return s

    def is_open(self, key, now=None):
        now = time.monotonic() if now is None else now
        return self.snapshot(key)["open_until"] > now


class TenantResolver:
    """JWT/OIDC 租户解析：验签回调由调用方注入（`verify(token)->claims`）。

    **不自带 JWT 实现**（核心零依赖）。没配 verifier 时 fail-closed：
    任何请求都拒绝，绝不因为没有鉴权就当「超级租户」。
    """

    def __init__(self, verifier=None, *, required_claim="tenant_id"):
        self.verifier = verifier
        self.required_claim = required_claim

    def resolve(self, authorization):
        if not self.verifier:
            return None, "no_oidc_verifier_configured"
        if not authorization or not authorization.lower().startswith("bearer "):
            return None, "missing_bearer"
        token = authorization.split(None, 1)[1].strip()
        try:
            claims = self.verifier(token)
        except Exception as exc:                            # noqa: BLE001
            return None, "verify_failed:" + type(exc).__name__
        if not isinstance(claims, dict):
            return None, "bad_claims"
        tid = claims.get(self.required_claim)
        if not tid:
            return None, "missing_tenant_claim"
        return {"tenant_id": str(tid),
                "roles": sorted(str(x) for x in (claims.get("roles") or []))}, ""


class WebhookApprovalNotifier:
    """审批 Webhook 通知 + 幂等回执校验（标准库 urllib，无第三方依赖）。"""

    def __init__(self, url=None, secret=None, timeout=5):
        self.url = url
        self.secret = secret
        self.timeout = timeout

    def notify(self, approval_id, payload, *, opener=None):
        if not self.url:
            return {"sent": False, "reason": "no_webhook_configured"}
        body = json.dumps({"approval_id": approval_id, "payload": payload},
                          ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-EvoGuard-Signature"] = hmac.new(
                self.secret.encode("utf-8"), body,
                hashlib.sha256).hexdigest()
        req = urllib.request.Request(self.url, data=body,
                                     headers=headers, method="POST")
        try:
            fn = opener or urllib.request.urlopen
            with fn(req, timeout=self.timeout) as r:
                return {"sent": True, "status": getattr(r, "status", None)}
        except (urllib.error.URLError, OSError) as exc:
            return {"sent": False, "reason": type(exc).__name__}

    def verify_receipt(self, approval_id, signature):
        if not self.secret:
            return False
        expect = hmac.new(self.secret.encode("utf-8"),
                          str(approval_id).encode("utf-8"),
                          hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, str(signature or ""))


def describe():
    """如实列出已实现与未实现；不把骨架说成生产可用。"""
    return {
        "version": 1,
        "implemented": ["RateLimiter", "MemoryLimiter", "SQLiteLimiter",
                        "CircuitBreakerState", "TenantResolver",
                        "WebhookApprovalNotifier",
                        "RedisLimiter/RedisCircuitBreaker/RedisJobQueue"
                        "（redis_backend.py，纯标准库 RESP）",
                        "JWT HS256 验签（jwt_hs256.py，标准库 HMAC）"],
        "verified": ["双进程真并发：限流跨副本 20/20 不倍增",
                     "双进程真 Redis：40 个作业 0 重复 0 丢失",
                     "双进程真 Redis：A 写熔断 B 可见",
                     "JWT alg=none / 过期 / 篡改签名 fail-closed"],
        "not_implemented": ["PostgresStore + Row-Level Security"
                            "（docker info rc=1，daemon 未起）",
                            "OIDC discovery / RS256（需 cryptography，"
                            "核心零依赖故用 verifier 回调注入）",
                            "Docker Compose 全套压测（本机 daemon 不可用）"],
        "note": "核心零依赖；Redis 后端用标准库 socket 手搓 RESP，"
                "不装 redis-py。压测用真 redis-server 5.0.9 + multiprocessing。",
    }
