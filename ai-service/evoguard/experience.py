# -*- coding: utf-8 -*-
"""经验层：从一次「故障→诊断→恢复」的轨迹里提炼结构化经验，并做准入校验。

这是 EvoGuard 与普通「记住一段对话」的根本区别：
  1. 经验必须带 evidence_refs（指向具体 step_id），没有证据一律拒绝
  2. 经验有 validation_status，未验证的**不得**作为可信执行指导
  3. 失败的恢复只能当反面案例，不能当经验

表在本模块内按需创建（规格要求：按阶段逐步迁移，不一次建全）。
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiences(
  id TEXT PRIMARY KEY, source_run_id TEXT, task_type TEXT, environment TEXT,
  failure_type TEXT, root_cause TEXT, recovery_action TEXT, recovery_result TEXT,
  evidence_refs TEXT, confidence REAL, validation_status TEXT,
  created_at REAL, version INTEGER);

CREATE TABLE IF NOT EXISTS evolution_events(
  id TEXT PRIMARY KEY, ts REAL, kind TEXT, subject_id TEXT, detail TEXT);

CREATE INDEX IF NOT EXISTS idx_exp_type ON experiences(task_type, failure_type);
CREATE INDEX IF NOT EXISTS idx_exp_status ON experiences(validation_status);
"""

VALIDATED, PENDING, REJECTED = "validated", "pending", "rejected"


def ensure_schema(store):
    with store.conn() as c:
        c.executescript(SCHEMA)


def event(store, kind, subject_id, detail=""):
    # 审计表在本模块里，但别的模块（skills）也会来写事件。
    # 不加这一行，先调 skills 再调 event 就会报 no such table。
    ensure_schema(store)
    with store.conn() as c:
        c.execute("INSERT INTO evolution_events(id,ts,kind,subject_id,detail) VALUES(?,?,?,?,?)",
                  ("ev_" + uuid.uuid4().hex[:10], round(time.time(), 3), kind,
                   subject_id, str(detail)[:500]))


def extract(store, run_id):
    """从一次运行里提炼经验。没有诊断记录就返回 None —— 没诊断就没有可提炼的东西。"""
    run = store.get_run(run_id)
    dia = store.get_diagnosis(run_id)
    if not dia:
        return None
    rec = store.get_recovery(run_id)
    steps = store.list_steps(run_id)

    # 证据：定位到的那一步 + 恢复过程产生的步
    refs = []
    # 注意：diagnosis 里存的是**绝对 seq**（报告要对得上轨迹行），
    # 这里必须按 seq 匹配，比成 logical 会一条证据都取不到 ->
    # 经验被判「没有 evidence_refs」-> 探索出来的经验全被拒。
    for s in steps:
        if s.get("seq") == dia["first_bad_step"]:
            refs.append(s["id"])
    for s in steps:
        if s["kind"] == "replay":
            refs.append(s["id"])

    exp = {
        "id": "exp_" + uuid.uuid4().hex[:10],
        "source_run_id": run_id,
        "task_type": _task_type_of(store, run),
        "environment": "mock",
        "failure_type": dia["root_cause"],
        "root_cause": dia["root_cause"],
        "recovery_action": (rec or {}).get("strategy") or "",
        "recovery_result": "success" if (rec or {}).get("ok") else ("fail" if rec else "none"),
        "evidence_refs": refs,
        "confidence": float(dia.get("confidence") or 0.0),
        "validation_status": PENDING,
        "created_at": round(time.time(), 3),
        "version": 1,
    }
    return exp


def _task_type_of(store, run):
    try:
        return store.get_experiment(run["experiment_id"])["agent_type"]
    except Exception:
        return "unknown"


def validate(exp):
    """准入校验。返回 (status, reasons)。三条硬规则，任何一条不过就是 rejected。"""
    reasons = []
    if not exp.get("evidence_refs"):
        reasons.append("没有 evidence_refs")
    if not exp.get("root_cause") or exp["root_cause"] == "unknown":
        reasons.append("根因未知，无法形成可复用经验")
    if exp.get("recovery_result") != "success":
        reasons.append("恢复未成功，只能作为反面案例")
    if float(exp.get("confidence") or 0) < 0.5:
        reasons.append("置信度过低")
    status = VALIDATED if not reasons else REJECTED
    return status, reasons


def save(store, exp):
    """保存经验。先校验，再落库 —— 不允许绕过校验直接写。"""
    status, reasons = validate(exp)
    exp["validation_status"] = status
    with store.conn() as c:
        c.execute(
            "INSERT INTO experiences(id,source_run_id,task_type,environment,failure_type,"
            "root_cause,recovery_action,recovery_result,evidence_refs,confidence,"
            "validation_status,created_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (exp["id"], exp["source_run_id"], exp["task_type"], exp["environment"],
             exp["failure_type"], exp["root_cause"], exp["recovery_action"],
             exp["recovery_result"], core.dumps(exp["evidence_refs"]),
             exp["confidence"], status, exp["created_at"], exp["version"]))
    event(store, "experience_saved", exp["id"],
          "%s/%s/%s -> %s" % (exp["task_type"], exp["failure_type"],
                              exp["recovery_action"], status))
    return exp, reasons


def retrieve(store, task_type=None, failure_type=None, include_pending=False):
    """记忆检索（治理在这里生效）。

    - 默认只返回 validated
    - include_pending=True 时把 pending 也返回 —— 这是 C 模式（不严格验证）用的，
      目的是**故意让它吃到未验证经验**，从而测出负迁移
    - rejected 永远不返回
    """
    ensure_schema(store)
    sql = "SELECT * FROM experiences WHERE 1=1"
    args = []
    if task_type:
        sql += " AND task_type=?"
        args.append(task_type)
    if failure_type:
        sql += " AND failure_type=?"
        args.append(failure_type)
    allowed = (PENDING, VALIDATED) if include_pending else (VALIDATED,)
    sql += " AND validation_status IN (%s)" % ",".join("?" * len(allowed))
    args.extend(allowed)
    with store.conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence_refs"] = core.loads(d["evidence_refs"], [])
        out.append(d)
    return out


def stats(store):
    ensure_schema(store)
    with store.conn() as c:
        rows = c.execute("SELECT validation_status, COUNT(*) FROM experiences "
                         "GROUP BY validation_status").fetchall()
    return {r[0]: r[1] for r in rows}
