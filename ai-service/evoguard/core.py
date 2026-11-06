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
import sqlite3
import time
import uuid

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


class Store:
    """所有持久化入口。每个方法自己开连接，避免多线程共享连接的问题。"""

    def __init__(self, path=None):
        self.path = path or DB_PATH or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "agentlab.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.init()

    @contextlib.contextmanager
    def conn(self):
        """每次操作独立连接，用完**一定关闭**。

        注意：sqlite3 的 `with conn:` 只负责提交事务，**不会关连接**。
        之前直接用它会漏一堆文件句柄（测试里刷满了 ResourceWarning）。
        """
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def init(self):
        with self.conn() as c:
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

    def list_steps(self, run_id):
        """诊断模块**只能**通过这个函数拿到轨迹。它拿不到 faults 表。"""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
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

    def get_faults(self, run_id):
        """只有评测模块该调用它。诊断模块不 import 这个函数。"""
        with self.conn() as c:
            rows = c.execute("SELECT * FROM faults WHERE run_id=?", (run_id,)).fetchall()
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

    def add_recovery(self, run_id, diagnosis_id, strategy, params, approval, ok, detail):
        with self.conn() as c:
            c.execute("INSERT INTO recoveries(id,run_id,diagnosis_id,strategy,params,approval,"
                      "ok,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (new_id("rec"), run_id, diagnosis_id, strategy, dumps(params),
                       approval, 1 if ok else 0, detail, now()))

    def get_recovery(self, run_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM recoveries WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                          (run_id,)).fetchone()
        return dict(r) if r else None

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
