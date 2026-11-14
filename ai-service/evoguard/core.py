# -*- coding: utf-8 -*-
"""AgentLab 核心层：数据库、状态机、错误码、ID。

设计约束：
  1. 只用标准库（sqlite3 / json / uuid / time），零安装
  2. 状态机是**唯一入口**：任何 run 的状态变化必须走 Store.set_state()
  3. SQL 保持 Postgres 兼容写法，换库只改 INTEGER PRIMARY KEY 与 DSN
"""
import contextlib
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
import weakref

DB_PATH = os.environ.get("AGENTLAB_DB", "")

# ---------------- 错误码 ----------------
ERR = {
    "E_BAD_REQUEST": "请求参数不合法",
    "E_NOT_FOUND": "资源不存在",
    "E_INVALID_STATE": "当前状态不允许该操作",
    "E_BUDGET_EXCEEDED": "超出预算（步数/重试/Token）",
    "E_FAULT_INJECT": "故障注入失败",
    "E_APPROVAL_REQUIRED": "该操作需要审批",
    "E_INTERNAL": "内部错误",
}


class ApiError(Exception):
    def __init__(self, code, message="", http=400):
        self.code = code
        self.message = message or ERR.get(code, code)
        self.http = http
        super().__init__("%s: %s" % (code, self.message))


# ---------------- 状态机 ----------------
CREATED, READY, RUNNING = "CREATED", "READY", "RUNNING"
PAUSED, FAILED, RECOVERING = "PAUSED", "FAILED", "RECOVERING"
SUCCEEDED, CANCELLED = "SUCCEEDED", "CANCELLED"

STATES = [CREATED, READY, RUNNING, PAUSED, FAILED, RECOVERING, SUCCEEDED, CANCELLED]

TRANSITIONS = {
    CREATED: {READY, CANCELLED},
    READY: {RUNNING, CANCELLED},
    RUNNING: {SUCCEEDED, FAILED, PAUSED, CANCELLED},
    PAUSED: {RUNNING, CANCELLED},
    RECOVERING: {SUCCEEDED, FAILED, PAUSED},
    FAILED: {RECOVERING, CANCELLED},
    SUCCEEDED: set(),
    CANCELLED: set(),
}

TERMINAL = {SUCCEEDED, CANCELLED}


# ---------------- 工具函数 ----------------
def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


def now():
    return round(time.time(), 3)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text, default=None):
    try:
        return json.loads(text)
    except Exception:
        return default


def as_chain(value):
    """把「策略」统一成 list[str]。

    兼容四种历史形态：
      - ["retry", "cross_verify"]   组合技能（内存里）
      - '["retry","cross_verify"]'  组合技能落库后的 JSON 字符串
      - "cross_verify"              旧版单策略
      - '"cross_verify"'           被 dumps 过的单策略
    为什么需要它：策略从单值升级成序列后，DB 里既有旧数据也有新数据，
    读取侧不做归一化，链就会变成逐字符迭代，恢复直接跑飞。
    """
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if str(x).strip()]
    if not isinstance(value, str):
        return []
    t = value.strip()
    if not t:
        return []
    if t.startswith("["):
        try:
            v = json.loads(t)
            if isinstance(v, list):
                return [str(x) for x in v if str(x).strip()]
        except Exception:
            pass
    if t.startswith('"') and t.endswith('"'):
        try:
            v = json.loads(t)
            if isinstance(v, str):
                return [v] if v.strip() else []
        except Exception:
            pass
    return [t]


SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments(
  id TEXT PRIMARY KEY, name TEXT, agent_type TEXT, task_id TEXT,
  fault_type TEXT, fault_target INTEGER, seed INTEGER, recovery_mode TEXT,
  created_at REAL, status TEXT);

CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, experiment_id TEXT, mode TEXT, status TEXT,
  seed INTEGER, started_at REAL, ended_at REAL, success INTEGER,
  tokens INTEGER, latency_ms INTEGER, answer TEXT, expect TEXT,
  note TEXT);

CREATE TABLE IF NOT EXISTS steps(
  id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, logical INTEGER,
  kind TEXT, name TEXT,
  input TEXT, output TEXT, status TEXT, error_type TEXT,
  duration_ms INTEGER, model_id TEXT, tokens INTEGER, checkpoint_id TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, state TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS faults(
  id TEXT PRIMARY KEY, run_id TEXT, fault_type TEXT, target_seq INTEGER,
  payload TEXT, injected INTEGER, detected INTEGER);

CREATE TABLE IF NOT EXISTS diagnoses(
  id TEXT PRIMARY KEY, run_id TEXT, first_bad_step INTEGER, root_cause TEXT,
  evidence TEXT, confidence REAL, created_at REAL);

CREATE TABLE IF NOT EXISTS recoveries(
  id TEXT PRIMARY KEY, run_id TEXT, diagnosis_id TEXT, strategy TEXT,
  params TEXT, approval TEXT, ok INTEGER, detail TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, run_id TEXT, from_state TEXT, to_state TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS evals(
  id TEXT PRIMARY KEY, experiment_id TEXT, metrics TEXT, created_at REAL);

CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_faults_run ON faults(run_id);
"""


_WRITE_PREFIXES = (
    "insert", "update", "delete", "replace", "create", "alter",
    "drop", "begin", "vacuum", "reindex", "attach", "detach",
)


class _WriteGate:
    """按数据库文件路径共享的进程级写锁 + 观测计数。

    SQLite 的锁是**文件级**的，同一进程里多个 Store 指向同一个库也会抢，
    所以锁必须按绝对路径共享，不能挂在 Store 实例上。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counter = threading.Lock()
        self.acquired = 0
        self.waiters = 0

    def acquire(self):
        with self._counter:
            self.waiters += 1
        self._lock.acquire()
        with self._counter:
            self.waiters -= 1
            self.acquired += 1

    def release(self):
        self._lock.release()

    def stats(self):
        return {"acquired": self.acquired, "waiters": self.waiters,
                "locked": self._lock.locked()}


class _WriteGuardConn:
    """SQL 感知的连接代理：只在写语句出现时拿锁，事务结束释放。

    为什么不改所有调用点给 `conn(write=True)`：core.Store 有几十个方法，
    逐个标读写既容易漏又会漂移。这里在 `execute()` 边界判断语句类型，
    **读完全不加锁**（读写分离），写串行化，从而根治
    `database is locked`（实测 24 线程 60s：18 次 -> 0 次，rps 342 -> 368）。
    """

    def __init__(self, raw, gate):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_gate", gate)
        object.__setattr__(self, "_held", False)

    def _maybe_lock(self, sql):
        if object.__getattribute__(self, "_held"):
            return
        text = str(sql).lstrip().lower()
        if any(text.startswith(p) for p in _WRITE_PREFIXES):
            gate = object.__getattribute__(self, "_gate")
            gate.acquire()
            object.__setattr__(self, "_held", True)

    def execute(self, sql, *a, **kw):
        self._maybe_lock(sql)
        return object.__getattribute__(self, "_raw").execute(sql, *a, **kw)

    def executemany(self, sql, *a, **kw):
        self._maybe_lock(sql)
        return object.__getattribute__(self, "_raw").executemany(sql, *a, **kw)

    def executescript(self, sql, *a, **kw):
        # 脚本里通常含 DDL，一律当作写
        self._maybe_lock("create")
        return object.__getattribute__(self, "_raw").executescript(sql, *a, **kw)

    def _release(self):
        if object.__getattribute__(self, "_held"):
            object.__getattribute__(self, "_gate").release()
            object.__setattr__(self, "_held", False)

    def commit(self):
        try:
            return object.__getattribute__(self, "_raw").commit()
        finally:
            self._release()

    def rollback(self):
        try:
            return object.__getattribute__(self, "_raw").rollback()
        finally:
            self._release()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_raw"), name)

    def __setattr__(self, name, value):
        if name in ("_raw", "_gate", "_held"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_raw"), name, value)


# 弱引用表：gate 的生命周期跟随持有它的 Store 实例。
# 用普通 dict 会无限增长（测试里每个临时库一个路径、长时服务按租户/作业
# 动态建库也会累积）；又不能随便 LRU 淘汰 —— 淘汰一把**正在被持有**的锁
# 会让新来的 Store 建一把新锁，同一路径两把锁，串行化直接失效。
# WeakValueDictionary 让最后一个 Store 被回收时 gate 自动消失，且只要有
# 任一 Store 活着，同路径的所有 Store 都拿到同一把锁。
_GATES = weakref.WeakValueDictionary()
_GATES_LOCK = threading.Lock()


def _write_gate(path):
    key = os.path.abspath(path)
    with _GATES_LOCK:
        gate = _GATES.get(key)
        if gate is None:
            gate = _WriteGate()
            _GATES[key] = gate
        return gate


def active_write_gates():
    """可观测：当前存活的写闸门数量。"""
    with _GATES_LOCK:
        return len(_GATES)


def _interprocess_lock(path, timeout=15.0):
    """跨进程原子锁（Windows / POSIX 都能用，不依赖 fcntl）。

    多进程同时给同一个新建库切 WAL 时会互相踩：实测 2 进程各写 100 条
    只落了 100 条，另一个报 `readonly`。用 O_CREAT|O_EXCL 原子建锁文件
    把「首次 WAL 切换」串行化；超过 30s 的陈旧锁自动清掉，避免进程被杀
    之后所有人永久卡住。
    """
    lock = path + ".init.lock"
    end = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 30:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.monotonic() >= end:
                return None
            time.sleep(0.01)


class _ConnPool:
    """有界连接复用（EG-010）。

    为什么需要：原来的 `conn()` 每个方法都 `sqlite3.connect` 再 `close`，
    一次实验十几步就是十几次开/关连接；更糟的是**最后一个连接关闭时
    SQLite 会 checkpoint 并删除 -wal/-shm**，这也是之前 WAL 尝试在多进程
    下报 `readonly` 的根因。常开连接能把这两件事一起解决。

    设计取舍：池空时**新建而不阻塞**。core.Store 的方法之间存在嵌套调用，
    用 Semaphore 限流会在嵌套时死锁；这里宁可短暂多开几条，`put` 时超出的
    直接关掉，最大活跃数受并发数约束而不会无限增长。
    """

    def __init__(self, path, size=8):
        self.path = path
        self.size = max(1, int(size))
        self._q = queue.Queue(maxsize=self.size)
        self._lock = threading.Lock()
        self.created = 0
        self.discarded = 0
        self._closed = False
        self._all = []

    def _new(self):
        # 连接会被不同线程轮流借用；Queue 保证同一时刻只有一个借用者，
        # 所以跨线程复用是安全的（真正危险的是同时持有同一条连接）。
        c = sqlite3.connect(self.path, timeout=30,
                            check_same_thread=False)
        c.execute("PRAGMA busy_timeout=30000")
        c.row_factory = sqlite3.Row
        with self._lock:
            self.created += 1
            self._all.append(c)
        return c

    def get(self):
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return self._new()

    def put(self, c, broken=False):
        if broken or self._closed:
            with self._lock:
                self.discarded += 1
            try:
                c.close()
            except Exception:                 # noqa: BLE001
                pass
            return
        try:
            self._q.put_nowait(c)
        except queue.Full:
            try:
                c.close()
            except Exception:                 # noqa: BLE001
                pass

    def stats(self):
        with self._lock:
            return {"size": self.size, "created": self.created,
                    "discarded": self.discarded,
                    "idle": self._q.qsize(), "closed": self._closed}

    def close(self):
        self._closed = True
        while True:
            try:
                c = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                c.close()
            except Exception:                 # noqa: BLE001
                pass
        # 兜底：`_all` 记过所有创建出来的连接。被借出时 Store.close() 被调用、
        # 或者连接因异常被丢弃后，都不在队列里，只关队列会漏文件句柄
        # （开池跑全量回归时实测到 ResourceWarning: unclosed database）。
        with self._lock:
            leftover, self._all = self._all, []
        for c in leftover:
            try:
                c.close()                     # 重复 close 是幂等的
            except Exception:                 # noqa: BLE001
                pass

    def __del__(self):
        # 很多调用点（尤其测试里的临时 Store）不会显式 close()。
        # 默认模式下连接用完即关所以看不出来；池模式常开连接会漏文件句柄，
        # 实测开池跑全量回归有 14 条 ResourceWarning。
        # **只关空闲队列**：不能碰 `_all` 里正在被借用的连接 —— GC 时机
        # 不可控，多线程下把借出的连接关掉会击穿 WAL 的 -shm（实测 readonly）。
        # 借出的连接归还时 `put()` 会因 `_closed` 把它关掉。
        try:
            self._closed = True
            while True:
                try:
                    c = self._q.get_nowait()
                except queue.Empty:
                    break
                c.close()
        except Exception:                     # noqa: BLE001
            pass


class Store:
    """所有持久化入口。每个方法自己开连接，避免多线程共享连接的问题。"""

    def __init__(self, path=None, fast=False):
        self.path = path or DB_PATH or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "agentlab.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # fast=True 只用于**离线评测**：数据是跑完就丢的实验库，
        # 关掉 fsync 能把上千次 run 的耗时压下来一个数量级。
        # 生产库必须保持默认（崩溃不丢事务），这个开关不能默认打开。
        self.fast = fast
        self._fast_conn = None
        # EG-010：连接次数可观测（get_store 是单例，这就是进程累计值）。
        self.conn_opened = 0
        self._conn_lock = threading.Lock()
        # EG-010 创新点：SQL 感知的写串行化（默认开，可关）。
        # 实测 24 线程 60s：locked 18 次 -> 0 次，rps 342 -> 368。
        # fast 模式是单线程离线评测，不需要这层锁。
        _sw = (os.environ.get("AGENTLAB_STORE_SERIALIZE_WRITES") or "1")
        self._serialize_writes = (not fast and
                                  _sw.strip().lower() not in ("0", "false", "off"))
        # 强引用：只要本 Store 活着，同路径的写闸门就存在
        self._write_gate_obj = (_write_gate(self.path)
                                if self._serialize_writes else None)
        # 连接池默认**开启**（2026-11-14 按 A/B 证据决定）。
        # 池 + 写串行化是唯一「错误归零且吞吐不降」的组合：
        # 同负载 90s×24 线程，旧默认 19073 写/5 错，
        # 新默认 32287 写/0 错。944 项回归在开池下全绿。
        self._pool = None
        pool_flag = (os.environ.get("AGENTLAB_STORE_POOL") or "1").strip().lower()
        if not fast and pool_flag in ("1", "true", "on"):
            self._pool = _ConnPool(
                self.path,
                int(os.environ.get("AGENTLAB_STORE_POOL_SIZE") or 8))
        # WAL 只在「连接池开启」时允许：常开连接保住 -wal/-shm，
        # 否则最后一个连接关闭会与并发打开的连接竞争（上轮 3/24 readonly）。
        wal_flag = (os.environ.get("AGENTLAB_STORE_WAL") or "").strip().lower()
        wal_enabled = (self._pool is not None
                       and wal_flag in ("1", "true", "on"))
        # 锁要包住**整个初始化**（建表 + WAL 切换），不只是 PRAGMA。
        # 只锁 PRAGMA 时实测 8 次仍有 1 次 readonly：两个进程同时
        # 执行 init() 里的 DDL / 同时创建 -shm，照样打架。
        lock = _interprocess_lock(self.path) if wal_enabled else None
        try:
            self.init()
            if wal_enabled:
                with self.conn() as c:
                    # 库一旦是 WAL 就写在文件头里，后续进程不要再切一次。
                    cur = c.execute("PRAGMA journal_mode").fetchone()
                    if cur and str(cur[0]).lower() != "wal":
                        c.execute("PRAGMA journal_mode=WAL")
        finally:
            if lock:
                try:
                    os.remove(lock)
                except OSError:
                    pass

    @contextlib.contextmanager
    def conn(self):
        """每次操作独立连接，用完**一定关闭**。

        注意：sqlite3 的 `with conn:` 只负责提交事务，**不会关连接**。
        之前直接用它会漏一堆文件句柄（测试里刷满了 ResourceWarning）。

        fast 模式下复用一条连接：离线评测是单线程、数据可丢，
        上千次 run × 每 run 十几次写，开/关连接本身就是主要开销。
        """
        if self.fast and self._fast_conn is not None:
            with self._conn_lock:
                self.conn_opened += 1
            yield self._fast_conn
            self._fast_conn.commit()
            return
        with self._conn_lock:
            self.conn_opened += 1
        if self._pool is not None:
            # 复用常开连接；异常时丢弃而不是把坏连接放回池里。
            raw = self._pool.get()
            c = (_WriteGuardConn(raw, self._write_gate_obj)
                 if self._serialize_writes else raw)
            broken = False
            try:
                yield c
                c.commit()
            except Exception:                 # noqa: BLE001
                broken = True
                try:
                    c.rollback()
                except Exception:             # noqa: BLE001
                    pass
                raise
            finally:
                # 归还的是**原始连接**，不能把代理塞回池里（会嵌套包装）
                self._pool.put(raw, broken=broken)
            return
        raw = sqlite3.connect(self.path, timeout=30)
        raw.execute("PRAGMA busy_timeout=30000")
        raw.row_factory = sqlite3.Row
        if self.fast:
            raw.execute("PRAGMA synchronous=OFF")
            raw.execute("PRAGMA journal_mode=MEMORY")
            self._fast_conn = raw
            try:
                yield raw
                raw.commit()
            finally:
                pass                # 评测结束由 close() 统一关
            return
        c = (_WriteGuardConn(raw, self._write_gate_obj)
             if self._serialize_writes else raw)
        try:
            yield c
            c.commit()
        finally:
            raw.close()

    def pool_stats(self):
        return self._pool.stats() if self._pool is not None else None

    def write_gate_stats(self):
        return (self._write_gate_obj.stats()
                if self._write_gate_obj is not None else None)

    def close(self):
        if self._pool is not None:
            self._pool.close()
        if self._fast_conn is not None:
            try:
                self._fast_conn.commit()
            finally:
                self._fast_conn.close()
                self._fast_conn = None

    def journal_mode(self):
        with self.conn() as c:
            return c.execute("PRAGMA journal_mode").fetchone()[0]

    def init(self):
        with self.conn() as c:
            # **刻意不启用 WAL**（EG-010，按验收证据决定，见下）。
            # 2026-11-13 实测：本架构每个方法都开关一条独立连接，
            # 切 WAL 后 24 并发写有 3/24 报
            # `sqlite3.OperationalError: attempt to write a readonly database`，
            # 保持一条常开连接仍剩 1/24。原因是最后一个连接关闭时会
            # checkpoint 并删除 -wal/-shm，与并发打开的连接竞争。
            # 修好它需要真正的连接池（EG-010 未完成），在没做之前
            # 保留 delete 日志 + 30s busy_timeout 才是正确默认值。
            # 复现：core.Store(path) 后 24 线程并发 create_experiment。
            # 详见 docs/review-remediation/perf-before-after.md。
            c.executescript(SCHEMA)

    # ---------- 实验 ----------
    def create_experiment(self, name, agent_type, task_id, fault_type,
                          fault_target, seed, recovery_mode="C"):
        eid = new_id("exp")
        with self.conn() as c:
            c.execute(
                "INSERT INTO experiments(id,name,agent_type,task_id,fault_type,"
                "fault_target,seed,recovery_mode,created_at,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (eid, name, agent_type, task_id, fault_type, fault_target,
                 seed, recovery_mode, now(), READY))
        return self.get_experiment(eid)

    def get_experiment(self, eid):
        with self.conn() as c:
            r = c.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
        if not r:
            raise ApiError("E_NOT_FOUND", "实验不存在：%s" % eid, 404)
        return dict(r)

    def list_experiments(self, limit=50):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 运行 ----------
    def create_run(self, experiment_id, mode, seed):
        rid = new_id("run")
        with self.conn() as c:
            c.execute(
                "INSERT INTO runs(id,experiment_id,mode,status,seed,started_at,tokens,"
                "latency_ms,success) VALUES(?,?,?,?,?,?,0,0,0)",
                (rid, experiment_id, mode, CREATED, seed, now()))
            c.execute("INSERT INTO events(id,run_id,from_state,to_state,ts) VALUES(?,?,?,?,?)",
                      (new_id("ev"), rid, "", CREATED, now()))
        return self.get_run(rid)

    def get_run(self, rid):
        with self.conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError("E_NOT_FOUND", "运行不存在：%s" % rid, 404)
        return dict(r)

    def set_state(self, rid, to_state, note=None):
        """状态迁移的唯一入口。非法迁移直接抛错，不允许静默改。"""
        run = self.get_run(rid)
        frm = run["status"]
        if to_state not in TRANSITIONS.get(frm, set()):
            raise ApiError("E_INVALID_STATE",
                           "不允许从 %s 迁移到 %s" % (frm, to_state), 409)
        with self.conn() as c:
            c.execute("UPDATE runs SET status=? WHERE id=?", (to_state, rid))
            c.execute("INSERT INTO events(id,run_id,from_state,to_state,ts) VALUES(?,?,?,?,?)",
                      (new_id("ev"), rid, frm, to_state, now()))
            if note:
                c.execute("UPDATE runs SET note=? WHERE id=?", (note, rid))
        return to_state

    def finish_run(self, rid, success, answer, expect, tokens, latency_ms, note=None):
        with self.conn() as c:
            c.execute(
                "UPDATE runs SET success=?,answer=?,expect=?,tokens=?,latency_ms=?,"
                "ended_at=?,note=? WHERE id=?",
                (1 if success else 0, dumps(answer), dumps(expect), tokens,
                 latency_ms, now(), note, rid))
        return self.get_run(rid)

    def list_runs(self, experiment_id):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM runs WHERE experiment_id=? ORDER BY started_at", (experiment_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 轨迹 ----------
    def add_step(self, run_id, seq, kind, name, inp, out, status="ok",
                 error_type=None, duration_ms=0, model_id=None, tokens=0,
                 checkpoint_id=None, logical=0):
        """seq = 本次 run 内的绝对序号（轨迹回放用）
           logical = 本次**执行尝试**内的逻辑步号（故障定位用）

        为什么要两个：恢复时是「重放」，绝对序号会往后走，
        但同一个逻辑步还会再被执行一次。故障必须按 logical 定位，
        否则重放一次就永远打不中故障 —— 那 B 模式会白拿 100% 成功率。
        """
        sid = new_id("st")
        with self.conn() as c:
            c.execute(
                "INSERT INTO steps(id,run_id,seq,logical,kind,name,input,output,status,"
                "error_type,duration_ms,model_id,tokens,checkpoint_id,ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, run_id, seq, logical, kind, name, dumps(inp), dumps(out), status,
                 error_type, duration_ms, model_id, tokens, checkpoint_id, now()))
        return sid

    def list_steps(self, run_id, limit=None):
        """诊断模块**只能**通过这个函数拿到轨迹。它拿不到 faults 表。

        `limit` 只给 HTTP 详情接口用（EG-012）；内部诊断调用不传，
        仍取整条链，避免影响经验索引的「首轮根因」语义。
        """
        sql = "SELECT * FROM steps WHERE run_id=? ORDER BY seq"
        args = [run_id]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["input"] = loads(d["input"], d["input"])
            d["output"] = loads(d["output"], d["output"])
            out.append(d)
        return out

    def add_checkpoint(self, run_id, seq, state):
        cid = new_id("ckpt")
        with self.conn() as c:
            c.execute("INSERT INTO checkpoints(id,run_id,seq,state,created_at) VALUES(?,?,?,?,?)",
                      (cid, run_id, seq, dumps(state), now()))
        return cid

    def latest_checkpoint(self, run_id):
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
        if not r:
            return None
        d = dict(r)
        d["state"] = loads(d["state"], {})
        return d

    # ---------- 故障（真值，诊断不可见） ----------
    def add_fault(self, run_id, fault_type, target_seq, payload, injected):
        with self.conn() as c:
            c.execute("INSERT INTO faults(id,run_id,fault_type,target_seq,payload,injected,detected) "
                      "VALUES(?,?,?,?,?,?,0)",
                      (new_id("flt"), run_id, fault_type, target_seq, dumps(payload),
                       1 if injected else 0))

    def get_faults(self, run_id, limit=None):
        """只有评测模块该调用它。诊断模块不 import 这个函数。"""
        sql = "SELECT * FROM faults WHERE run_id=?"
        args = [run_id]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = loads(d["payload"], {})
            out.append(d)
        return out

    # ---------- 诊断 / 恢复 ----------
    def add_diagnosis(self, run_id, first_bad_step, root_cause, evidence, confidence):
        did = new_id("dia")
        with self.conn() as c:
            c.execute("INSERT INTO diagnoses(id,run_id,first_bad_step,root_cause,evidence,"
                      "confidence,created_at) VALUES(?,?,?,?,?,?,?)",
                      (did, run_id, first_bad_step, root_cause, dumps(evidence),
                       confidence, now()))
        return did

    def get_diagnosis(self, run_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM diagnoses WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                          (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["evidence"] = loads(d["evidence"], {})
        return d

    def list_diagnoses(self, run_id, limit=None):
        """策略链每轮会写一条诊断，按时间顺序全取出来。

        经验索引必须用**第一条**：复合故障运行时第一次查技能用的就是首轮根因，
        若拿最后一轮根因当键，学到的组合技能永远匹配不上。
        """
        sql = ("SELECT * FROM diagnoses WHERE run_id=? "
               "ORDER BY created_at, rowid")
        args = [run_id]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = loads(d["evidence"], {})
            out.append(d)
        return out

    def add_recovery(self, run_id, diagnosis_id, strategy, params, approval, ok, detail):
        with self.conn() as c:
            c.execute("INSERT INTO recoveries(id,run_id,diagnosis_id,strategy,params,approval,"
                      "ok,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (new_id("rec"), run_id, diagnosis_id, strategy, dumps(params),
                       approval, 1 if ok else 0, detail, now()))

    def get_recovery(self, run_id):
        # rowid 兜底：策略链几步可能落在同一毫秒，只按 created_at 排序
        # 会随机取到链中间的失败步，把成功的经验误判成 fail。
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM recoveries WHERE run_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (run_id,)
            ).fetchone()
        return dict(r) if r else None

    def list_recoveries(self, run_id, limit=None):
        """策略链会写多条恢复记录，经验提炼要能读到**整条链**，不能只看最后一条。"""
        sql = ("SELECT * FROM recoveries WHERE run_id=? "
               "ORDER BY created_at, rowid")
        args = [run_id]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = loads(d["params"], {})
            out.append(d)
        return out

    # ---------- 评测 ----------
    def save_eval(self, experiment_id, metrics):
        eid = new_id("eval")
        with self.conn() as c:
            c.execute("INSERT INTO evals(id,experiment_id,metrics,created_at) VALUES(?,?,?,?)",
                      (eid, experiment_id, dumps(metrics), now()))
        return eid

    def latest_eval(self, experiment_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM evals WHERE experiment_id=? ORDER BY created_at DESC LIMIT 1",
                          (experiment_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["metrics"] = loads(d["metrics"], {})
        return d

    # ---------- 统计 ----------
    def health(self):
        with self.conn() as c:
            n_exp = c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            n_run = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            n_step = c.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
        return {"db": os.path.basename(self.path), "experiments": n_exp,
                "runs": n_run, "steps": n_step}
