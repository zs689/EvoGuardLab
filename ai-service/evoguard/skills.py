# -*- coding: utf-8 -*-
"""技能层：候选技能 → 独立验证 → 晋升 / 拒绝 / 回滚，以及负迁移检测。

四条硬规则（规格第十一部分「严禁」逐条落实）：
  1. 候选技能**不得覆盖**旧版本，只能新增版本，晋升才生效
  2. 技能只能声明"遇到 X 用 Y 策略"，**不得**携带可执行代码
  3. 晋升必须通过独立验证集，且必须同时检查「有没有把原本能做的事弄坏」
  4. 评测标准在代码里固定，技能自己改不了
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core      # noqa: E402
import experience as exp_mod  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills(
  id TEXT PRIMARY KEY, name TEXT, task_type TEXT, fault_type TEXT, strategy TEXT,
  required_tools TEXT, safety TEXT, status TEXT, version INTEGER, created_at REAL);

CREATE TABLE IF NOT EXISTS skill_versions(
  id TEXT PRIMARY KEY, skill_id TEXT, version INTEGER, snapshot TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS skill_evaluations(
  id TEXT PRIMARY KEY, skill_id TEXT, target_n INTEGER, before_rate REAL, after_rate REAL,
  gain_pp REAL, guard_n INTEGER, neg_transfer_rate REAL, verdict TEXT, created_at REAL);
"""

CANDIDATE, ACTIVE, REJECTED, NEEDS_REVIEW = "candidate", "active", "rejected", "needs_review"

# 晋升门槛（固定在这里，技能与模型都改不了）
MIN_GAIN_PP = 15.0
MAX_NEG_TRANSFER = 0.05


def ensure_schema(store):
    with store.conn() as c:
        c.executescript(SCHEMA)


def propose(exp):
    """从一条已验证经验生成候选技能。声明式，不含任何可执行代码。"""
    return {
        "id": "sk_" + uuid.uuid4().hex[:10],
        "name": "遇到 %s 时改用 %s" % (exp["failure_type"], exp["recovery_action"]),
        "task_type": exp["task_type"],
        "fault_type": exp["failure_type"],
        "strategy": exp["recovery_action"],
        "required_tools": [],
        "safety": {"max_retry": 2, "require_approval_for_write": True},
        "status": CANDIDATE,
        "version": 1,
        "created_at": round(time.time(), 3),
        "from_experience": exp["id"],
    }


def save(store, skill):
    ensure_schema(store)
    with store.conn() as c:
        c.execute("INSERT INTO skills(id,name,task_type,fault_type,strategy,required_tools,"
                  "safety,status,version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (skill["id"], skill["name"], skill["task_type"], skill["fault_type"],
                   skill["strategy"], core.dumps(skill["required_tools"]),
                   core.dumps(skill["safety"]), skill["status"], skill["version"],
                   skill["created_at"]))
        c.execute("INSERT INTO skill_versions(id,skill_id,version,snapshot,created_at) "
                  "VALUES(?,?,?,?,?)",
                  ("skv_" + uuid.uuid4().hex[:8], skill["id"], skill["version"],
                   core.dumps(skill), skill["created_at"]))
    exp_mod.event(store, "skill_proposed", skill["id"], skill["name"])
    return skill


def lookup(store, agent_type, fault_type, mode):
    """恢复期查技能。这是 A/B/C/D 四组差异的关键开关。

    D：只认 active 技能，且必须 **agent + 故障类型都匹配**（严格）
    C：认 candidate + active，且 **只按 agent 匹配**（不严格 → 经验被过度泛化 → 负迁移）
    """
    ensure_schema(store)
    if mode == "D":
        sql = ("SELECT * FROM skills WHERE status=? AND task_type=? AND fault_type=? "
               "ORDER BY version DESC LIMIT 1")
        args = (ACTIVE, agent_type, fault_type)
    elif mode == "C":
        sql = ("SELECT * FROM skills WHERE status IN (?,?) AND task_type=? "
               "ORDER BY version DESC LIMIT 1")
        args = (CANDIDATE, ACTIVE, agent_type)
    else:
        return None
    with store.conn() as c:
        r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def evaluate(store, skill, target_cases, guard_cases, run_case):
    """在独立验证集上测候选技能。

    target_cases：该技能声称能解决的病例（同 agent + 同故障）
    guard_cases ：**不该被影响**的病例（同 agent、不同故障）—— 用来测负迁移
    run_case(case, use_skill) -> bool 是否成功
    """
    ensure_schema(store)

    def run_all(cases, use_skill):
        ok = 0
        for c in cases:
            if run_case(c, skill if use_skill else None):
                ok += 1
        return ok

    t_n = len(target_cases) or 1
    before = run_all(target_cases, False)
    after = run_all(target_cases, True)
    before_rate = before / t_n
    after_rate = after / t_n
    gain_pp = round((after_rate - before_rate) * 100, 1)

    g_n = len(guard_cases) or 1
    g_before = run_all(guard_cases, False)
    g_after = run_all(guard_cases, True)
    neg = round(max(0, g_before - g_after) / g_n, 4)

    if gain_pp >= MIN_GAIN_PP and neg <= MAX_NEG_TRANSFER:
        verdict = "PROMOTE"
    elif neg > MAX_NEG_TRANSFER:
        verdict = "REJECT"          # 收益再高也不许用它换破坏
    elif gain_pp <= 0:
        verdict = "REJECT"
    else:
        verdict = "NEEDS_REVIEW"

    with store.conn() as c:
        c.execute("INSERT INTO skill_evaluations(id,skill_id,target_n,before_rate,after_rate,"
                  "gain_pp,guard_n,neg_transfer_rate,verdict,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("ske_" + uuid.uuid4().hex[:10], skill["id"], len(target_cases),
                   round(before_rate, 4), round(after_rate, 4), gain_pp,
                   len(guard_cases), neg, verdict, round(time.time(), 3)))
    exp_mod.event(store, "skill_evaluated", skill["id"],
                  "gain=%spp neg=%s verdict=%s" % (gain_pp, neg, verdict))
    return {"gain_pp": gain_pp, "before_rate": round(before_rate, 4),
            "after_rate": round(after_rate, 4), "neg_transfer_rate": neg,
            "verdict": verdict, "target_n": len(target_cases),
            "guard_n": len(guard_cases)}


def promote(store, skill_id):
    """晋升：新增版本并置为 active。旧版本保留，不覆盖。"""
    with store.conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not r:
            raise KeyError("技能不存在：%s" % skill_id)
        new_ver = int(r["version"]) + 1
        c.execute("UPDATE skills SET status=?, version=? WHERE id=?",
                  (ACTIVE, new_ver, skill_id))
        snap = dict(r)
        snap["version"] = new_ver
        snap["status"] = ACTIVE
        c.execute("INSERT INTO skill_versions(id,skill_id,version,snapshot,created_at) "
                  "VALUES(?,?,?,?,?)",
                  ("skv_" + uuid.uuid4().hex[:8], skill_id, new_ver,
                   core.dumps(snap), round(time.time(), 3)))
    exp_mod.event(store, "skill_promoted", skill_id, "v%d" % new_ver)
    return new_ver


def rollback(store, skill_id, to_version=None):
    """回滚：把状态退回候选，并记录到指定历史版本。旧快照不删除。"""
    with store.conn() as c:
        rows = c.execute("SELECT * FROM skill_versions WHERE skill_id=? ORDER BY version",
                         (skill_id,)).fetchall()
        if not rows:
            raise KeyError("没有可回滚的版本：%s" % skill_id)
        target = [r for r in rows if int(r["version"]) == int(to_version)] if to_version else rows[:-1]
        if not target:
            raise KeyError("目标版本不存在")
        snap = core.loads(target[-1]["snapshot"], {})
        c.execute("UPDATE skills SET status=?, version=? WHERE id=?",
                  (CANDIDATE, int(snap.get("version") or 1), skill_id))
    exp_mod.event(store, "skill_rolled_back", skill_id,
                  "to v%s" % (snap.get("version") or 1))
    return snap.get("version")


def list_skills(store, status=None):
    ensure_schema(store)
    sql = "SELECT * FROM skills"
    args = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    with store.conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY created_at DESC", args).fetchall()]
