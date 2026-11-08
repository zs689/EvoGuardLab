# -*- coding: utf-8 -*-
"""技能层：候选技能 → 独立验证 → 晋升 / 拒绝 / 回滚，以及负迁移检测。

四条硬规则（规格第十一部分「严禁」逐条落实）：
  1. 候选技能**不得覆盖**旧版本，只能新增版本，晋升才生效
  2. 技能只能声明"遇到 X 用 Y 策略"，**不得**携带可执行代码
  3. 晋升必须通过独立验证集，且必须同时检查「有没有把原本能做的事弄坏」
  4. 评测标准在代码里固定，技能自己改不了
"""
import copy
import hashlib
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
  required_tools TEXT, safety TEXT, status TEXT, version INTEGER, created_at REAL,
  scope TEXT DEFAULT 'single');

CREATE TABLE IF NOT EXISTS skill_versions(
  id TEXT PRIMARY KEY, skill_id TEXT, version INTEGER, snapshot TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS skill_evaluations(
  id TEXT PRIMARY KEY, skill_id TEXT, target_n INTEGER, before_rate REAL, after_rate REAL,
  gain_pp REAL, guard_n INTEGER, neg_transfer_rate REAL, verdict TEXT, created_at REAL);
"""

CANDIDATE, ACTIVE, REJECTED, NEEDS_REVIEW = "candidate", "active", "rejected", "needs_review"

# 技能作用域：这条经验是「一个坏点」学来的，还是「两个坏点」学来的。
# 为什么必须分开：技能只按 (agent, fault_type) 建索引，而复合故障的根因是
# 它链路里的**第一个**故障 —— 于是复合学到的两步链会被挂到单故障的键上。
# 实测后果很具体：kb/retrieval_failure 上被放进一条两步链后，
# 单故障 retrieval_failure 的 6 个用例全挂了（本该用一步 fallback_tool）。
# 这不是「负迁移难测」，是键里少了一个维度。
SINGLE, MULTI = "single", "multi"

# 晋升门槛（固定在这里，技能与模型都改不了）
MIN_GAIN_PP = 15.0
MAX_NEG_TRANSFER = 0.05
VALIDATION_PROTOCOL = "paired-v2"
MIN_VALIDATION_TASKS = 3  # 工程证据下限，不是统计安全保证


def ensure_schema(store):
    exp_mod.ensure_schema(store)      # evolution_events 在经验模块里，一起建
    with store.conn() as c:
        c.executescript(SCHEMA)
        # 老库没有 scope 列，补上（幂等）。已有技能默认算 single ——
        # 它们大多是单故障经验；复合链会在重新训练时带上正确的作用域。
        cols = [r[1] for r in c.execute("PRAGMA table_info(skills)").fetchall()]
        if "scope" not in cols:
            c.execute("ALTER TABLE skills ADD COLUMN scope TEXT DEFAULT 'single'")
        cols = [r[1] for r in c.execute("PRAGMA table_info(skill_evaluations)")]
        # NULL 明确表示历史协议，不能给旧结果补上新版协议标签。
        for name in ("protocol_version", "details"):
            if name not in cols:
                c.execute("ALTER TABLE skill_evaluations ADD COLUMN %s TEXT" % name)


def propose(exp):
    """从一条已验证经验生成候选技能。声明式，不含任何可执行代码。

    strategy 允许是**策略链**（list），落库时序列化成 JSON 字符串；
    单策略仍存裸字符串，保证旧版技能与测试的行为完全一致。
    技能永远只声明「遇到 X 用哪几步策略」，不携带任何可执行代码。
    """
    chain = core.as_chain(exp["recovery_action"])
    stored = core.dumps(chain) if len(chain) > 1 else (chain[0] if chain else "")
    # 作用域取自经验本身：一次运行里诊断出几个坏点，就决定了这条技能能在
    # 「第几轮恢复」被命中。链长>1 但只有一个坏点时仍是 single。
    scope = exp.get("fault_scope") or (MULTI if len(chain) > 1 else SINGLE)
    return {
        "id": "sk_" + uuid.uuid4().hex[:10],
        "name": "遇到 %s 时按序执行 %s" % (exp["failure_type"], " -> ".join(chain) or "-"),
        "task_type": exp["task_type"],
        "fault_type": exp["failure_type"],
        "strategy": stored,
        "strategy_chain": chain,
        "scope": scope,
        "required_tools": [],
        "safety": {"max_retry": 2, "require_approval_for_write": True,
                   "max_chain": 3},
        "status": CANDIDATE,
        "version": 1,
        "created_at": round(time.time(), 3),
        "from_experience": exp["id"],
    }


def save(store, skill):
    ensure_schema(store)
    strategy = skill["strategy"]
    if isinstance(strategy, (list, tuple)):
        strategy = core.dumps(list(strategy))
    with store.conn() as c:
        c.execute("INSERT INTO skills(id,name,task_type,fault_type,strategy,required_tools,"
                  "safety,status,version,created_at,scope) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (skill["id"], skill["name"], skill["task_type"], skill["fault_type"],
                   strategy, core.dumps(skill["required_tools"]),
                   core.dumps(skill["safety"]), skill["status"], skill["version"],
                   skill["created_at"], skill.get("scope") or SINGLE))
        c.execute("INSERT INTO skill_versions(id,skill_id,version,snapshot,created_at) "
                  "VALUES(?,?,?,?,?)",
                  ("skv_" + uuid.uuid4().hex[:8], skill["id"], skill["version"],
                   core.dumps(skill), skill["created_at"]))
    exp_mod.event(store, "skill_proposed", skill["id"], skill["name"])
    return skill


def lookup(store, agent_type, fault_type, mode, scope=SINGLE):
    """恢复期查技能。这是 A/B/C/D 四组差异的关键开关。

    D：只认 active 技能，且必须 **agent + 故障类型都匹配**（严格）
    C：认 candidate + active，且 **只按 agent 匹配**（不严格 → 经验被过度泛化 → 负迁移）

    scope 是「现在是第几轮恢复」：
      第 1 轮（single）优先单故障技能，第 2 轮起（multi）优先多故障技能。
      只按 prefer 排序、不硬过滤 —— 多故障场景下一条单步策略仍可能是正确的第二步。
      硬过滤会让「单故障经验在复合场景下彻底失效」，那是另一个方向的错误。

    排序必须给足次序（prefer → version → created_at → rowid）：
    C 只看 agent，同一 agent 下必然有多条技能并列（技能库里的 version 通常都是 2）。
    只写 `ORDER BY version DESC` 时，SQLite 对并列行的取舍取决于扫描顺序，
    实测就是「永远取到最早插入的那条」—— 这会让「C 的成功率」变成
    技能库插入顺序的产物，而不是治理机制的效果。同一个 bug 之前在
    get_recovery 里踩过（毫秒并列取到链中间的失败步），这里用同样的兜底。
    """
    ensure_schema(store)
    ORDER = ("ORDER BY (scope=?) DESC, version DESC, created_at DESC, rowid DESC LIMIT 1")
    if mode == "D":
        sql = ("SELECT * FROM skills WHERE status=? AND task_type=? AND fault_type=? "
               + ORDER)
        args = (ACTIVE, agent_type, fault_type, scope)
    elif mode == "C":
        sql = ("SELECT * FROM skills WHERE status IN (?,?) AND task_type=? " + ORDER)
        args = (CANDIDATE, ACTIVE, agent_type, scope)
    else:
        return None
    with store.conn() as c:
        r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def _task_key(case):
    agent, task = case.get("agent_type"), case.get("task_id")
    if not agent or not task:
        raise ValueError("validation case requires agent_type and task_id")
    return (agent, task)


def _family_key(case):
    # 老数据没有 family_id 时，最多只能证明 task_id 不同。
    return ("family", case["family_id"]) if case.get("family_id") else _task_key(case)


def _case_key(case):
    return core.dumps({"task": _task_key(case), "fault_type": case.get("fault_type"),
                       "fault_chain": case.get("fault_chain") or [],
                       "fault_target": case.get("fault_target", 0),
                       "seed": case.get("seed", 7)})


def _outcome(result):
    """兼容旧 bool 和 (bool, run_id)，新适配器可提供 cost 字典。"""
    if isinstance(result, dict):
        success, rid, cost = result.get("success"), result.get("run_id"), result.get("cost")
    elif isinstance(result, tuple) and len(result) == 2:
        success, rid = result
        cost = None
    else:
        success, rid, cost = result, None, None
    if not isinstance(success, (bool, int)) or success not in (0, 1):
        raise ValueError("run_case must return an explicit boolean success")
    return bool(success), rid, cost


def evaluate(store, skill, target_cases, guard_cases, run_case, *, training_cases=()):
    """同 case/seed 成对验证。空分母不可估计；不足三项独立任务不得自动晋升。

    training_cases 用于排除训练任务/家族，两个正式入口必须传入。
    回调返回 bool、(bool, run_id) 或 {success, run_id, cost}。
    """
    target_cases, guard_cases = list(target_cases), list(guard_cases)
    training_cases = list(training_cases)
    seen_ids, seen_keys = set(), set()
    train_tasks = {_task_key(c) for c in training_cases}
    train_families = {_family_key(c) for c in training_cases}
    for case in target_cases + guard_cases:
        key = _case_key(case)
        cid = case.get("case_id") or "case_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if cid in seen_ids:
            raise ValueError("duplicate case_id in validation: %s" % cid)
        if key in seen_keys:
            raise ValueError("duplicate validation recipe: %s" % cid)
        if _task_key(case) in train_tasks or _family_key(case) in train_families:
            raise ValueError("validation overlaps training task/family: %s" % cid)
        seen_ids.add(cid)
        seen_keys.add(key)

    ensure_schema(store)
    pairs = []
    for group, cases in (("target", target_cases), ("guard", guard_cases)):
        for case in cases:
            key = _case_key(case)
            cid = case.get("case_id") or "case_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            # 每一侧独立拷贝，防止运行器修改 recipe 或候选，破坏配对。
            before, bid, bcost = _outcome(run_case(copy.deepcopy(case), None))
            after, aid, acost = _outcome(run_case(copy.deepcopy(case), copy.deepcopy(skill)))
            pairs.append({"group": group, "case_id": cid, "case": copy.deepcopy(case),
                          "seed": case.get("seed", 7), "fault_type": case.get("fault_type"),
                          "baseline_success": before, "candidate_success": after,
                          "baseline_run_id": bid, "candidate_run_id": aid,
                          "baseline_cost": bcost, "candidate_cost": acost})

    target = [p for p in pairs if p["group"] == "target"]
    guard = [p for p in pairs if p["group"] == "guard"]
    t_n, g_n = len(target), len(guard)
    before_rate = sum(p["baseline_success"] for p in target) / t_n if t_n else None
    after_rate = sum(p["candidate_success"] for p in target) / t_n if t_n else None
    gain_pp = (after_rate - before_rate) * 100 if t_n else None
    g_before = sum(p["baseline_success"] for p in guard)
    regressed = sum(p["baseline_success"] and not p["candidate_success"] for p in guard)
    improved = sum(not p["baseline_success"] and p["candidate_success"] for p in guard)
    retained = sum(p["baseline_success"] and p["candidate_success"] for p in guard)
    both_failed = g_n - regressed - improved - retained
    neg = regressed / g_before if g_before else None
    target_tasks = len({_family_key(c) for c in target_cases})
    guard_tasks = len({_family_key(c) for c in guard_cases})
    successful_guard_tasks = len({_family_key(p["case"]) for p in guard if p["baseline_success"]})
    reasons = []
    if target_tasks < MIN_VALIDATION_TASKS:
        reasons.append("target has fewer than 3 independent tasks/families")
    if guard_tasks < MIN_VALIDATION_TASKS:
        reasons.append("guard has fewer than 3 independent tasks/families")
    if successful_guard_tasks < MIN_VALIDATION_TASKS:
        reasons.append("guard has fewer than 3 baseline-success tasks/families")
    if neg is None:
        reasons.append("negative transfer is unestimable: no baseline guard successes")

    # 使用未舍入值判门槛；已观察到的过量退化优先拒绝。
    if neg is not None and neg > MAX_NEG_TRANSFER:
        verdict = "REJECT"
        reasons.append("paired negative transfer exceeds threshold")
    elif reasons or gain_pp is None:
        verdict = "NEEDS_REVIEW"
    elif gain_pp <= 0:
        verdict = "REJECT"
        reasons.append("no target gain")
    elif gain_pp >= MIN_GAIN_PP:
        verdict = "PROMOTE"
    else:
        verdict = "NEEDS_REVIEW"
        reasons.append("target gain below promotion threshold")

    result = {"id": "ske_" + uuid.uuid4().hex[:10], "skill_id": skill["id"],
              "protocol_version": VALIDATION_PROTOCOL,
              "gain_pp": gain_pp, "before_rate": before_rate, "after_rate": after_rate,
              "neg_transfer_rate": neg, "verdict": verdict, "reasons": reasons,
              "target_n": t_n, "guard_n": g_n, "target_tasks": target_tasks,
              "guard_tasks": guard_tasks, "guard_baseline_success": g_before,
              "guard_baseline_success_tasks": successful_guard_tasks,
              "guard_regressions": regressed, "guard_improvements": improved,
              "guard_retained": retained, "guard_both_failed": both_failed,
              "guard_regression_fraction": regressed / g_n if g_n else None,
              "training_overlap_checked": bool(training_cases),
              "thresholds": {"min_gain_pp": MIN_GAIN_PP,
                             "max_neg_transfer": MAX_NEG_TRANSFER,
                             "min_validation_tasks": MIN_VALIDATION_TASKS},
              "pairs": pairs}
    with store.conn() as c:
        c.execute("INSERT INTO skill_evaluations(id,skill_id,target_n,before_rate,after_rate,"
                  "gain_pp,guard_n,neg_transfer_rate,verdict,created_at,protocol_version,details) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (result["id"], skill["id"], t_n, before_rate, after_rate, gain_pp,
                   g_n, neg, verdict, core.now(), VALIDATION_PROTOCOL, core.dumps(result)))
    exp_mod.event(store, "skill_evaluated", skill["id"],
                  "%s gain=%spp neg=%s verdict=%s" % (VALIDATION_PROTOCOL, gain_pp, neg, verdict))
    return result


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
