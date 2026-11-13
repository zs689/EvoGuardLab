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

SCHEMA_VERSION = 4


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


def _migrate_4(conn):
    """归档锚点表（EG-007）。

    按保留期删掉旧行后，剩余第一行的 prev_hash 指向一条已不存在的行，
    朴素链式校验必然报 prev_mismatch —— 也就是说「合规地清理数据」和
    「有人偷偷删了审计」在旧实现里长得一模一样。这不行。

    锚点表记录**授权删除**：被删区间的最后一行哈希、区间长度、删后
    剩余首行 id。verify() 只有在缺口处找到匹配锚点才放行；手工 DELETE
    没有锚点，仍然报 prev_mismatch。两者必须给出不同结果。

    注意：锚点表本身应保存在独立权限域（离线备份 / 只追加日志），
    否则能改数据库的人可以顺手插一条锚点伪造删除 —— 见
    docs/enterprise-readiness.md 的已知限制。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_anchors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            after_id INTEGER NOT NULL,
            anchor_hash TEXT NOT NULL,
            deleted_rows INTEGER NOT NULL,
            created_at REAL NOT NULL,
            tenant_id TEXT,
            note TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_anchors_after "
                 "ON audit_anchors(after_id)")


MIGRATIONS = [(1, _migrate_1), (2, _migrate_2), (3, _migrate_3),
              (4, _migrate_4)]


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
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     timeout=30.0)
        # **刻意不启用 WAL**（EG-008，按验收证据决定）。
        # 2026-11-13 实测：多进程共享这个 WAL 库时，10 次跨进程并发测试
        # 有 3 次报 `attempt to write a readonly database` —— 一个进程关闭
        # 连接时 checkpoint 并删除 -wal/-shm，与另一个进程正在打开的
        # 连接竞争。EG-008 要的是「跨进程事务边界」，由下面的
        # BEGIN IMMEDIATE（emit/migrate）+ busy_timeout 提供，不需要 WAL。
        # 复现与数据见 docs/review-remediation/perf-before-after.md。
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.version = self.migrate()

    # ---------------- 迁移 ----------------
    def migrate(self):
        """按版本顺序应用未执行的迁移；已是最新则无操作。幂等。

        EG-008：整个迁移在一个 `BEGIN IMMEDIATE` 事务里做，跨进程串行化。
        以前只用进程内锁，两个实例同时启动会各自读 schema_migrations
        然后同时 DDL / 插入同一版本号。
        """
        with self._lock:
            conn = self._conn
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at REAL NOT NULL
                    )
                """)
                done = {r[0] for r in
                        conn.execute("SELECT version FROM schema_migrations")}
                for version, fn in MIGRATIONS:
                    if version in done:
                        continue
                    fn(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (?, ?)", (version, self._clock()))
                conn.commit()
                return max([0] + [v for v, _ in MIGRATIONS])
            except Exception:
                conn.rollback()
                raise

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
            conn = self._conn
            if conn.in_transaction:
                conn.commit()
            # EG-008：读链尾 + 插入必须在**同一个跨进程事务**里，
            # 否则两个实例会读到同一个 prev_hash，写出分叉链。
            conn.execute("BEGIN IMMEDIATE")
            try:
                prev = conn.execute(
                    "SELECT row_hash FROM audit_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prev_hash = prev[0] if prev and prev[0] else ""
                row_hash = _canonical(ts, tenant, request_id, actor, method,
                                      path, status, decision, detail,
                                      latency_ms, prev_hash)
                cur = conn.execute(
                    "INSERT INTO audit_events(ts,tenant_id,request_id,actor,"
                    "method,path,status,decision,detail,latency_ms,"
                    "prev_hash,row_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, tenant, request_id, actor, method, path, status,
                     decision, detail, latency_ms, prev_hash, row_hash))
                rid = cur.lastrowid
                conn.commit()
                return rid
            except Exception:
                conn.rollback()
                raise

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
        checked = unhashed = archived = 0
        prev_hash = ""
        # EG-008：流式分批，大表校验的峰值内存不随总行数线性增长。
        # 以前一次性 fetchall()，几百万行会直接把内存吃满。
        sql = ("SELECT id,ts,tenant_id,request_id,actor,method,path,status,"
               "decision,detail,latency_ms,prev_hash,row_hash "
               "FROM audit_events ORDER BY id ASC" + (
                   " LIMIT %d" % int(limit) if limit else ""))
        with self._lock:
            anchors = {}
            for after_id, anchor_hash, n_del in self._conn.execute(
                    "SELECT after_id, anchor_hash, deleted_rows "
                    "FROM audit_anchors"):
                anchors.setdefault(after_id, []).append((anchor_hash, n_del))
            cur = self._conn.execute(sql)
            while True:
                batch = cur.fetchmany(1000)
                if not batch:
                    break
                for r in batch:
                    (rid, ts, tenant, request_id, actor, method, path, status,
                     decision, detail, latency_ms, stored_prev, stored_hash) = r
                    if not stored_hash:
                        unhashed += 1
                        prev_hash = ""
                        continue
                    effective_prev = stored_prev or ""
                    if effective_prev != prev_hash:
                        # 只有「授权归档」留下的锚点能解释这个缺口；
                        # 手工 DELETE / 插入伪造行没有锚点，一律判为断链。
                        hits = anchors.get(rid, [])
                        if not any(h == effective_prev for h, _ in hits):
                            return False, {
                                "reason": "prev_mismatch",
                                "first_bad_id": rid, "checked": checked,
                                "unhashed": unhashed,
                                "archived_gaps": archived}
                        archived += 1
                    # 用行里存的 prev 重算：正常时等于链尾，归档缺口时是锚点。
                    want = _canonical(ts, tenant, request_id, actor, method,
                                      path, status, decision, detail,
                                      latency_ms, effective_prev)
                    if want != stored_hash:
                        return False, {
                            "reason": "hash_mismatch", "first_bad_id": rid,
                            "checked": checked, "unhashed": unhashed,
                            "archived_gaps": archived}
                    prev_hash = stored_hash
                    checked += 1
        return True, {"reason": "ok", "first_bad_id": None,
                      "checked": checked, "unhashed": unhashed,
                      "archived_gaps": archived}

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

    def purge_before(self, ts, tenant_id=None):
        """保留策略：删除早于 ts 的事件，并**为每段删除留下归档锚点**。

        传 tenant_id 就只清该租户（EG-007 要求按租户执行保留政策）。
        删除按 id 连续区间分组，每组一条锚点；这样乱序时间戳、
        多租户交错写入、清理边界都不会让剩余链失去可验证性。
        返回删除条数。
        """
        with self._lock:
            sql = "SELECT id, row_hash FROM audit_events WHERE ts < ?"
            params = [float(ts)]
            if tenant_id is not None:
                sql += " AND tenant_id = ?"
                params.append(tenant_id)
            sql += " ORDER BY id ASC"
            rows = self._conn.execute(sql, params).fetchall()
            if not rows:
                return 0
            ids = [r[0] for r in rows]
            hashes = {r[0]: (r[1] or "") for r in rows}

            segments = []
            start = prev = ids[0]
            for i in ids[1:]:
                if i == prev + 1:
                    prev = i
                else:
                    segments.append((start, prev))
                    start = prev = i
            segments.append((start, prev))

            for lo, hi in segments:
                nxt = self._conn.execute(
                    "SELECT MIN(id) FROM audit_events WHERE id > ?",
                    (hi,)).fetchone()[0]
                after_id = nxt if nxt is not None else hi + 1
                self._conn.execute(
                    "INSERT INTO audit_anchors(after_id, anchor_hash, "
                    "deleted_rows, created_at, tenant_id, note) "
                    "VALUES (?,?,?,?,?,?)",
                    (after_id, hashes[hi], hi - lo + 1, self._clock(),
                     tenant_id, "retention purge ts<%s" % ts))
                self._conn.execute(
                    "DELETE FROM audit_events WHERE id BETWEEN ? AND ?",
                    (lo, hi))
            self._conn.commit()
            return len(ids)

    def purge_older_than(self, days, now=None, tenant_id=None):
        cutoff = (now or self._clock()) - float(days) * 86400
        return self.purge_before(cutoff, tenant_id=tenant_id)

    def anchors(self):
        """列出归档锚点，供外部备份到独立权限域 / 事后对账。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,after_id,anchor_hash,deleted_rows,created_at,"
                "tenant_id,note FROM audit_anchors ORDER BY id").fetchall()
        keys = ("id", "after_id", "anchor_hash", "deleted_rows", "created_at",
                "tenant_id", "note")
        return [dict(zip(keys, r)) for r in rows]

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
