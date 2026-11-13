# -*- coding: utf-8 -*-
"""有界持久作业队列（EG-009，纯标准库）。

为什么需要：独立审查实测 `POST /api/run` 8 并发 P95 3.5 s，吞吐反而比
串行低 —— 业务流程和请求线程耦合，SQLite 写锁互相顶。这一层把「提交」
和「执行」拆开：提交只写一行、立刻返回 job_id；单 worker 顺序执行，
避免 N 个请求同时抢同一把写锁。

语义（这些比性能更重要）：

- **幂等键**：同一租户同一 key 的重复提交返回原作业，绝不产生第二次副作用。
- **租约**：running 作业带 lease；进程崩溃后重启，过期租约被标为 `unknown`，
  **不自动重放** —— 副作用可能已经发生了，自动重试会造成重复效果。
  `unknown` 需要人工对账后显式 `retry()`（对应 EG-026 的「状态未知进入对账」）。
- **租户隔离**：每个租户一个独立 jobs.db（与业务库同样的物理隔离口径）。
- **有界**：队列有容量上限；满了返回明确拒绝，不无限堆积。

已知限制（如实）：单 worker、单进程；多副本需要共享队列与分布式租约。
`runtime.FORCED_SKILL` 是进程级全局，本实现用一把执行锁串行化并标注风险，
没有改 runtime 本身（EG-009 的「worker 隔离」没做完）。
"""
import json
import os
import sqlite3
import threading
import time
import uuid

TERMINAL = frozenset(("succeeded", "failed", "canceled"))
ACTIVE = frozenset(("queued", "running", "unknown"))

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    lease_owner TEXT,
    lease_until REAL,
    attempt INTEGER NOT NULL DEFAULT 0,
    UNIQUE(tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS jobs_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _row_to_dict(cur, row):
    keys = [d[0] for d in cur.description]
    d = dict(zip(keys, row))
    for k in ("payload", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


class JobStore:
    """一个租户一个库；调用方负责把 path 指到租户专属目录。"""

    def __init__(self, path=":memory:", clock=None, max_queue=256,
                 lease_seconds=120.0):
        self.path = path
        self._clock = clock or time.time
        self.max_queue = int(max_queue)
        self.lease_seconds = float(lease_seconds)
        self._lock = threading.Lock()
        self._exec_lock = threading.Lock()   # 串行化 runtime 全局状态
        if path not in (":memory:", ""):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = None
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO jobs_meta(key,value) VALUES(?,?)",
                ("schema_version", str(SCHEMA_VERSION)))
            self._conn.commit()

    # ---------------- 提交 ----------------
    def submit(self, tenant_id, payload, idempotency_key=None):
        """返回 (job_dict, created)。已存在同 key 时 created=False。"""
        tid = tenant_id or "default"
        key = str(idempotency_key) if idempotency_key else None
        with self._lock:
            if key is not None:
                old = self._conn.execute(
                    "SELECT id FROM jobs WHERE tenant_id=? AND idempotency_key=?",
                    (tid, key)).fetchone()
                if old:
                    return self._get_unlocked(tid, old[0]), False
            active = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE tenant_id=? AND status='queued'",
                (tid,)).fetchone()[0]
            if active >= self.max_queue:
                raise OverflowError("队列已满（%d），请稍后重试" % self.max_queue)
            jid = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO jobs(id,tenant_id,idempotency_key,status,payload,"
                "created_at,attempt) VALUES(?,?,?,?,?,?,0)",
                (jid, tid, key, "queued",
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 self._clock()))
            self._conn.commit()
            return self._get_unlocked(tid, jid), True

    def _get_unlocked(self, tenant_id, job_id):
        cur = self._conn.execute(
            "SELECT * FROM jobs WHERE tenant_id=? AND id=?",
            (tenant_id or "default", job_id))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    def get(self, tenant_id, job_id):
        with self._lock:
            return self._get_unlocked(tenant_id or "default", job_id)

    def list(self, tenant_id, limit=50):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM jobs WHERE tenant_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id or "default", min(200, max(1, int(limit)))))
            return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def cancel(self, tenant_id, job_id):
        """只能取消还在排队的；已经在跑的不能假装没跑过。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='canceled', finished_at=? "
                "WHERE tenant_id=? AND id=? AND status='queued'",
                (self._clock(), tenant_id or "default", job_id))
            self._conn.commit()
            return cur.rowcount == 1

    def retry(self, tenant_id, job_id):
        """人工对账后显式重放；**只允许 unknown / failed**，不重放 succeeded。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='queued', error=NULL, result=NULL, "
                "started_at=NULL, finished_at=NULL, lease_owner=NULL, "
                "lease_until=NULL WHERE tenant_id=? AND id=? "
                "AND status IN ('unknown','failed','canceled')",
                (tenant_id or "default", job_id))
            self._conn.commit()
            return cur.rowcount == 1

    # ---------------- worker ----------------
    def claim(self, worker_id):
        """原子领取：BEGIN IMMEDIATE 保证多 worker 也不会拿到同一条。"""
        now = self._clock()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id,tenant_id,payload,attempt FROM jobs "
                    "WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    conn.commit()
                    return None
                jid, tid, payload, attempt = row
                cur = conn.execute(
                    "UPDATE jobs SET status='running', started_at=?, "
                    "lease_owner=?, lease_until=?, attempt=? "
                    "WHERE id=? AND status='queued'",
                    (now, worker_id, now + self.lease_seconds,
                     attempt + 1, jid))
                conn.commit()
                if cur.rowcount != 1:
                    return None
                return {"id": jid, "tenant_id": tid,
                        "payload": json.loads(payload), "attempt": attempt + 1}
            except Exception:
                conn.rollback()
                raise

    def finish(self, job_id, status, result=None, error=None):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, result=?, error=?, finished_at=?, "
                "lease_owner=NULL, lease_until=NULL WHERE id=?",
                (status,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 str(error) if error else None, self._clock(), job_id))
            self._conn.commit()

    def recover_stale_leases(self, worker_id=None):
        """进程重启时调用：过期租约标 unknown，等人来核对副作用再决定重放。

        绝不自动改回 queued —— 那会把「外部可能已经执行过」的操作再跑一遍。
        """
        now = self._clock()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='unknown', "
                "error='lease_expired: 副作用状态未知，需人工对账', "
                "lease_owner=NULL, lease_until=NULL "
                "WHERE status='running' AND lease_until < ?", (now,))
            self._conn.commit()
            return cur.rowcount

    def stats(self, tenant_id="default"):
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE tenant_id=? GROUP BY status",
                (tenant_id or "default",)).fetchall()
        return dict(rows)

    def close(self):
        with self._lock:
            self._conn.close()


class JobWorker:
    """单后台 worker；可注入 executor 便于测试，不把业务逻辑绑死在这儿。"""

    def __init__(self, store, executor, poll_interval=0.05, on_error=None):
        self.store = store
        self.executor = executor
        self.poll_interval = poll_interval
        self.on_error = on_error
        self.worker_id = "worker-" + uuid.uuid4().hex[:8]
        self._stop = threading.Event()
        self._thread = None
        self.processed = 0

    def start(self):
        if self._thread is not None:
            return self
        self.store.recover_stale_leases(self.worker_id)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="evoguard-job-worker")
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            job = self.store.claim(self.worker_id)
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            try:
                with self.store._exec_lock:
                    result = self.executor(job["tenant_id"], job["payload"])
                self.store.finish(job["id"], "succeeded", result=result)
                self.processed += 1
            except Exception as exc:       # noqa: BLE001
                self.store.finish(job["id"], "failed", error=repr(exc))
                if self.on_error:
                    self.on_error(job, exc)

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self
