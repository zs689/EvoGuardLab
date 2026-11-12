# -*- coding: utf-8 -*-
"""持久化审计表与 schema 迁移（sqlite3，纯标准库）。

真实现：审计事件落表（带 tenant_id 非空约束）、版本化迁移（幂等、可重入）、
按租户查询、按保留期清理、**逐行哈希链**（可验证未被篡改）。

预留接口：`AuditSink` —— 企业可同时把事件推到 SIEM / Kafka，只要实现 `emit`。

与既有 `security_effects` 表**分离**：那张表记「检测/执行后果」，这张表记
「谁在什么时候调了什么接口、结果如何」。两者都不含请求正文。
"""
import hashlib
import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 3


def _migrate_1(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            tenant_id TEXT NOT NULL CHECK (length(tenant_id) > 0),
            request_id TEXT NOT NULL,
            actor TEXT,
            method TEXT,
            path TEXT,
            status INTEGER,
            decision TEXT,
            detail TEXT
        )
    """)


def _migrate_2(conn):
    # 增量迁移示例：加索引 + 加一列（老库平滑升级）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts "
                 "ON audit_events(tenant_id, ts)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_events)")}
    if "latency_ms" not in cols:
        conn.execute("ALTER TABLE audit_events ADD COLUMN latency_ms REAL")


def _migrate_3(conn):
    """加防篡改哈希链：每行哈希覆盖「上一行哈希 + 本行业务字段」。

    没有这一层，审计表就只是一张能随便 UPDATE 的日志表 —— 出了事
    拿它当证据是不成立的。删中间一行、改一个 status，链条立刻断。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_events)")}
    if "prev_hash" not in cols:
        conn.execute("ALTER TABLE audit_events ADD COLUMN prev_hash TEXT")
    if "row_hash" not in cols:
        conn.execute("ALTER TABLE audit_events ADD COLUMN row_hash TEXT")


MIGRATIONS = [(1, _migrate_1), (2, _migrate_2), (3, _migrate_3)]


def _canonical(ts, tenant, request_id, actor, method, path, status, decision,
               detail, latency_ms, prev_hash):
    """哈希输入：字段顺序固定、浮点定点化，避免平台差异导致验链失败。"""
    payload = json.dumps({
        "prev": prev_hash or "",
        "ts": "%.6f" % float(ts),
        "tenant_id": tenant,
        "request_id": request_id,
        "actor": actor,
        "method": method,
        "path": path,
        "status": int(status),
        "decision": decision,
        "detail": detail,
        "latency_ms": None if latency_ms is None else "%.3f" % float(latency_ms),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditSink:
    def emit(self, event):                 # pragma: no cover - 接口
        raise NotImplementedError


class AuditStore(AuditSink):
    def __init__(self, path=":memory:", clock=None):
        self.path = path
        self._clock = clock or time.time
        self._lock = threading.Lock()
        if path not in (":memory:", ""):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.version = self.migrate()

    # ---------------- 迁移 ----------------
    def migrate(self):
        """按版本顺序应用未执行的迁移；已是最新则无操作。幂等。"""
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                )
            """)
            done = {r[0] for r in
                    self._conn.execute("SELECT version FROM schema_migrations")}
            for version, fn in MIGRATIONS:
                if version in done:
                    continue
                fn(self._conn)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (?, ?)", (version, self._clock()))
            self._conn.commit()
            return max([0] + [v for v, _ in MIGRATIONS])

    # ---------------- 写入 ----------------
    def emit(self, event):
        """`AuditSink` 实现；缺 tenant_id 直接拒绝（不允许匿名租户）。"""
        tenant = (event.get("tenant_id") or "").strip()
        if not tenant:
            raise ValueError("审计事件必须带 tenant_id")
        ts = float(event.get("ts") or self._clock())
        tenant = tenant
        request_id = str(event.get("request_id") or "-")
        actor = str(event.get("actor") or "anonymous")
        method = str(event.get("method") or "-")
        path = str(event.get("path") or "-")
        status = int(event.get("status") or 0)
        decision = str(event.get("decision") or "-")
        detail = (json.dumps(event.get("detail"), ensure_ascii=False,
                             sort_keys=True)
                  if event.get("detail") is not None else None)
        latency_ms = event.get("latency_ms")
        with self._lock:
            # 取链尾：serialize 写入，避免并发 emit 生成分叉链。
            prev = self._conn.execute(
                "SELECT row_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev[0] if prev and prev[0] else ""
            row_hash = _canonical(ts, tenant, request_id, actor, method, path,
                                  status, decision, detail, latency_ms,
                                  prev_hash)
            cur = self._conn.execute(
                "INSERT INTO audit_events(ts,tenant_id,request_id,actor,method,"
                "path,status,decision,detail,latency_ms,prev_hash,row_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, tenant, request_id, actor, method, path, status, decision,
                 detail, latency_ms, prev_hash, row_hash))
            self._conn.commit()
            return cur.lastrowid

    record = emit

    # ---------------- 防篡改校验 ----------------
    def verify(self, limit=None):
        """从头重算整条链，返回 (ok, 详情)。

        - `checked`：实际参与校验的行数
        - `unhashed`：迁移前写入、没有哈希的旧行（如实计数，不假装校验过）
        - `first_bad_id`：第一个断链的行 id（None 表示没断）
        断链原因分三种：`hash_mismatch`（行被改）、`prev_mismatch`（被删/插）、
        `unhashed`（旧行）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,ts,tenant_id,request_id,actor,method,path,status,"
                "decision,detail,latency_ms,prev_hash,row_hash "
                "FROM audit_events ORDER BY id ASC" + (
                    " LIMIT %d" % int(limit) if limit else "")).fetchall()
        checked = unhashed = 0
        prev_hash = ""
        for r in rows:
            (rid, ts, tenant, request_id, actor, method, path, status,
             decision, detail, latency_ms, stored_prev, stored_hash) = r
            if not stored_hash:
                unhashed += 1
                prev_hash = ""
                continue
            if (stored_prev or "") != prev_hash:
                return False, {"reason": "prev_mismatch", "first_bad_id": rid,
                               "checked": checked, "unhashed": unhashed}
            want = _canonical(ts, tenant, request_id, actor, method, path,
                              status, decision, detail, latency_ms, prev_hash)
            if want != stored_hash:
                return False, {"reason": "hash_mismatch", "first_bad_id": rid,
                               "checked": checked, "unhashed": unhashed}
            prev_hash = stored_hash
            checked += 1
        return True, {"reason": "ok", "first_bad_id": None,
                      "checked": checked, "unhashed": unhashed}

    # ---------------- 查询 ----------------
    def query(self, tenant_id, since=None, limit=100):
        sql = ("SELECT id,ts,tenant_id,request_id,actor,method,path,status,"
               "decision,detail,latency_ms FROM audit_events WHERE tenant_id = ?")
        params = [tenant_id]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(float(since))
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        keys = ("id", "ts", "tenant_id", "request_id", "actor", "method",
                "path", "status", "decision", "detail", "latency_ms")
        return [dict(zip(keys, r)) for r in rows]

    def count(self, tenant_id=None):
        with self._lock:
            if tenant_id is None:
                return self._conn.execute(
                    "SELECT COUNT(*) FROM audit_events").fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id = ?",
                (tenant_id,)).fetchone()[0]

    def purge_before(self, ts):
        """保留策略：删除早于 ts 的事件，返回删除条数。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM audit_events WHERE ts < ?",
                                     (float(ts),))
            self._conn.commit()
            return cur.rowcount

    def purge_older_than(self, days, now=None):
        cutoff = (now or self._clock()) - float(days) * 86400
        return self.purge_before(cutoff)

    def close(self):
        with self._lock:
            self._conn.close()


class MemoryAuditSink(AuditSink):
    """测试与无盘环境用；同样强制 tenant_id。"""

    def __init__(self, limit=1000):
        self.limit = limit
        self.events = []

    def emit(self, event):
        if not (event.get("tenant_id") or "").strip():
            raise ValueError("审计事件必须带 tenant_id")
        self.events.append(dict(event))
        if len(self.events) > self.limit:
            del self.events[:len(self.events) - self.limit]
        return len(self.events)
