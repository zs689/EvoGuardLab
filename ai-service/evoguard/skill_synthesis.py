# -*- coding: utf-8 -*-
"""WS-04-T2：从失败轨迹到 Skill 合成 → 独立验证 → 晋升/回滚的闭环编排。

把已有模块串成一条可复验的路径（不重写它们）：

    trajectory_diagnose.diagnose_trace  →  定位首个违例（不认工具名）
    experience.Experience.extract       →  结构化经验（可选，提供轨迹时）
    skills.propose                      →  声明式候选 Skill
    conditional_skill.accept            →  沙箱门禁（schema + 权限）
    skills.evaluate                     →  独立留出集 A/B（含 guard 负迁移）
    skills.promote / skills.rollback    →  晋升或回滚

**关键约束**：
- 验证用例必须与训练任务/家族无重叠（由 skills.evaluate 强制检查）；
- 候选 Skill 不得声明写/外发工具（conditional_skill 强制）；
- 出现未授权/不安全操作，业务收益不能抵消（paired_check / evaluate 判定）；
- 故意注入一条错误经验时，门禁必须拒绝晋升并触发回滚。

零第三方依赖。
"""
import copy
import json

try:
    from . import skills as _sk
    from . import conditional_skill as _cs
    from . import trajectory_diagnose as _td
    from . import security as _sec
except ImportError:
    import skills as _sk                                    # type: ignore
    import conditional_skill as _cs                         # type: ignore
    import trajectory_diagnose as _td                       # type: ignore
    import security as _sec                                 # type: ignore


#: 新领域工具链：文件 / HTTP / SQL 混合，**不在 evolution_bench 里硬编码**
NEW_DOMAIN_TOOLS = ("file.read", "http.fetch", "sql.query", "sql.update")


def synthesize(store, steps, *, task_type="newdomain",
               failure_type=None, expected_total=None, policy=None):
    """从失败轨迹合成一个候选 Skill（未落库、未晋升）。

    返回 {diagnosis, skill, accepted}；门禁拒绝时 accepted 里带 errors。
    """
    diag = _td.diagnose_trace(steps, expected_total=expected_total)
    if diag.get("none"):
        return {"diagnosis": diag, "skill": None,
                "accepted": {"accepted": False,
                             "errors": ["轨迹无违例，无可合成的经验"]}}
    cause = failure_type or diag["root_cause"]
    # 只读工具越界时不要给出工具名，避免把「观察到的一次」泛化成
    # 「所有新领域工具都这样」—— 声明里只记策略作用域。
    exp = {"id": "synth_" + str(diag.get("first_bad_step")),
           "task_type": task_type,
           "failure_type": cause,
           "recovery_action": ["retry"],
           "fault_scope": "single"}
    # skills.propose 产出的是老格式（strategy 链），conditional_skill 要求
    # 完整声明式 schema（applies_when/required_evidence/decision_params/
    # stop_when/fallback）。这里做**格式升级**，不改门禁、不放宽校验。
    skill = _sk.propose(exp)
    skill.update({
        "schema_version": _cs.SCHEMA_VERSION,
        "applies_when": {"agent": task_type,
                         "fault_type": cause,
                         "first_bad_logical": diag.get("first_bad_logical")},
        "required_evidence": ["tool_status", "error_type"],
        "decision_params": {"evidence_selector": "first",
                            "max_steps": 3},
        "stop_when": {"no_evidence": True, "unauthorized": True},
        "fallback": {"action": "abstain"},
        "strategy": ["retry"],
    })
    # 候选只能声明只读工具：写/外发工具必须走审批，不能由技能声明。
    skill["allowed_tools"] = list(NEW_DOMAIN_TOOLS[:3])
    pol = policy or {"allowed_tools": list(NEW_DOMAIN_TOOLS),
                     "write_tools": ["sql.update"]}
    try:
        accepted = _cs.accept(skill, pol)
    except _cs.SkillRejected as e:
        accepted = {"accepted": False, "errors": list(e.args[0])
                    if e.args and isinstance(e.args[0], list) else [str(e)]}
    return {"diagnosis": diag, "skill": skill, "accepted": accepted}


def evaluate_and_promote(store, skill, target_cases, guard_cases,
                         run_case, *, training_cases=(),
                         auto_rollback_on_regression=True):
    """独立验证 → 晋升；出现退化则回滚。返回完整审计 dict。"""
    ev = _sk.evaluate(store, skill, target_cases, guard_cases,
                      run_case, training_cases=training_cases)
    out = {"evaluation": ev, "promoted": False, "rolled_back": False,
           "status": ev["verdict"]}
    if ev["verdict"] == "PROMOTE":
        _sk.save(store, skill)
        _sk.promote(store, skill["id"], evaluation_id=ev["id"])
        out["promoted"] = True
        out["status"] = "active"
        # 注意：这里**不做**「线上退化检查」。
        # 旧实现在此处拿本次晋升所依据的那份评估结果当「线上指标」，
        # 但能 PROMOTE 就说明负迁移为 0、增益为正，判据恒假 —— 回滚分支是死代码。
        # 线上退化必须用**上线后的真实运行指标**，见 monitor_and_rollback()。
    elif ev["verdict"] == "REJECT" and auto_rollback_on_regression:
        # 被拒绝的候选不该留下 active 状态；若库里已有同名 active 则回滚
        try:
            _sk.rollback(store, skill["id"])
            out["rolled_back"] = True
        except (KeyError, ValueError):
            pass
    return out


#: 线上监控默认阈值。改这里等于改「什么算退化」，必须有测试兜着。
MONITOR_DEFAULTS = {
    "min_runs": 5,            # 样本太少不判，避免 1 条失败就回滚
    "success_drop_pp": 15.0,  # 成功率比晋升基线低多少算退化
    "max_unauthorized": 0,    # 越权副作用：出现一次就不容忍
    "max_false_positive_rate": 0.25,
}


def collect_online_stats(store, skill, *, since=None):
    """从 runs / experiments / security_effects 汇总**上线后**的真实指标。

    **关联方式**：优先用 `runs.skill_id` 精确归因（EVO-LOOP-01-T2
    给 runs 加了列，runtime 在 skill 命中时写回）；只有库里该技能
    没有任何 skill_id 归属时才回退「时间窗 + task_type」近似，
    兼容功能上线前的旧库。返回的 association 字段会说明本次用哪种。
    """
    task_type = skill.get("task_type")
    start = since if since is not None else (skill.get("promoted_at") or 0)
    _sk.ensure_schema(store)
    # security_effects / security_events 由 security 模块建；不确保存在
    # 会让监控在「从未跑过扫描」的库上直接抛错。
    _sec.ensure_schema(store)
    with store.conn() as c:
        # EVO-LOOP-01-T2：**优先按 runs.skill_id 精确归因**。
        # 只有该列在本库没有任何归属记录时（旧库/该功能上线前的运行），
        # 才回退到「时间窗 + task_type」近似 —— 避免无关运行稀释信号，
        # 也避免「同 task_type 下别人的失败误伤本技能」。
        cur = c.execute("PRAGMA table_info(runs)")
        cols = {r[1] for r in cur.fetchall()}
        exact = False
        if "skill_id" in cols:
            rows_exact = c.execute(
                "SELECT id, success FROM runs "
                "WHERE skill_id=? AND success IS NOT NULL",
                (skill.get("id"),)).fetchall()
            if rows_exact:
                rows = rows_exact
                exact = True
        if not exact:
            rows = c.execute(
                "SELECT r.id, r.success FROM runs r "
                "LEFT JOIN experiments e ON e.id=r.experiment_id "
                "WHERE r.started_at >= ? AND (e.agent_type IS NULL OR e.agent_type=?) "
                "AND r.success IS NOT NULL", (start, task_type)).fetchall()
        run_ids = [r["id"] for r in rows]
        n = len(rows)
        ok = sum(1 for r in rows if r["success"])
        unauthorized = 0
        if run_ids:
            q = ("SELECT COUNT(*) AS n FROM security_effects WHERE executed=1 "
                 "AND tool IS NOT NULL AND effect!='unchanged' "
                 "AND run_id IN (%s)" % ",".join("?" * len(run_ids)))
            unauthorized = int(c.execute(q, run_ids).fetchone()["n"])
        fp = 0
        if run_ids:
            q = ("SELECT COUNT(*) AS n FROM security_events WHERE "
                 "severity='warn' AND run_id IN (%s)"
                 % ",".join("?" * len(run_ids)))
            fp = int(c.execute(q, run_ids).fetchone()["n"])
    return {"n": n, "success": ok,
            "success_rate": round(ok / n, 4) if n else None,
            "unauthorized": unauthorized, "warnings": fp,
            "since": start, "task_type": task_type,
            "association": ("runs.skill_id (精确归因)" if exact
                            else "time_window+task_type (无 skill_id 归属，回退近似)")}


def baseline_from_evaluation(store, skill_id):
    """取晋升时绑定的评估作为基线（after_rate 就是上线前的目标域成功率）。"""
    with store.conn() as c:
        r = c.execute(
            "SELECT after_rate, gain_pp FROM skill_evaluations "
            "WHERE skill_id=? ORDER BY created_at DESC LIMIT 1",
            (skill_id,)).fetchone()
    if not r or r["after_rate"] is None:
        return None
    return {"success_rate": float(r["after_rate"]),
            "gain_pp": float(r["gain_pp"] or 0.0)}


def _judge_degradation(baseline, online, thresholds=None):
    """退化判定。返回 (degraded, reasons[]) —— 每条理由都可核对。"""
    t = dict(MONITOR_DEFAULTS)
    t.update(thresholds or {})
    reasons = []
    if online["n"] < t["min_runs"]:
        return False, ["样本不足(%d<%d)，不判退化"
                       % (online["n"], t["min_runs"])]
    if online["unauthorized"] > t["max_unauthorized"]:
        reasons.append("越权副作用 %d 次 > %d"
                       % (online["unauthorized"], t["max_unauthorized"]))
    if baseline and baseline.get("success_rate") is not None \
            and online.get("success_rate") is not None:
        drop = (baseline["success_rate"] - online["success_rate"]) * 100
        if drop > t["success_drop_pp"]:
            reasons.append("成功率下降 %.1fpp > %.1fpp（基线 %.3f → 线上 %.3f）"
                           % (drop, t["success_drop_pp"],
                              baseline["success_rate"], online["success_rate"]))
    if online["n"]:
        rate = online["warnings"] / float(online["n"])
        if rate > t["max_false_positive_rate"]:
            reasons.append("告警率 %.3f > %.3f"
                           % (rate, t["max_false_positive_rate"]))
    return bool(reasons), reasons


def monitor_and_rollback(store, skill_id, online_runs=None, *,
                         thresholds=None, since=None, dry_run=False):
    """线上退化监控 → 真实回滚。

    `online_runs` 为 None 时从 store 的 runs/security_effects 汇总
    （见 collect_online_stats 的关联近似说明）；显式传入时用它做指标，
    便于外部监控系统回灌。

    返回 {skill_id, baseline, online, degraded, reasons, rolled_back, status}。
    """
    sk = None
    for row in _sk.list_skills(store):
        if row["id"] == skill_id:
            sk = row
            break
    out = {"skill_id": skill_id, "baseline": None, "online": None,
           "degraded": False, "reasons": [], "rolled_back": False,
           "status": None}
    if sk is None:
        out["error"] = "skill_not_found"
        return out
    out["status"] = sk.get("status")
    if sk.get("status") != _sk.ACTIVE:
        out["reasons"].append("技能未处于 active（%s），不监控" % sk.get("status"))
        return out

    baseline = baseline_from_evaluation(store, skill_id)
    out["baseline"] = baseline
    if online_runs is None:
        online = collect_online_stats(store, sk, since=since)
    else:
        online = dict(online_runs)
        online.setdefault("association", "caller_supplied")
    out["online"] = online

    degraded, reasons = _judge_degradation(baseline, online, thresholds)
    out["degraded"] = degraded
    out["reasons"] = reasons
    if not degraded:
        return out
    if dry_run:
        out["reasons"] = reasons + ["dry_run：未执行回滚"]
        return out

    before_version = int(sk.get("active_version") or sk.get("version") or 0)
    try:
        _sk.rollback(store, skill_id)
    except (KeyError, ValueError) as exc:
        # EVO-LOOP-01-T1：v1（首次晋升）没有更旧快照可退，但**不能因此
        # 放着闯祸的技能继续激活**。止血兜底：disable（先别用了），
        # 与 rollback（换回旧正文继续用）严格区分。
        out["error"] = "rollback_unavailable:" + str(exc)[:100]
        out["degraded"] = True
        out["rolled_back"] = False
        try:
            _sk.disable(store, skill_id)
            out["disabled"] = True
            out["status"] = "disabled"
            out["reasons"] = reasons + [
                "仅 v1 无旧快照可退，已 disable 止血（未回滚正文）"]
        except Exception as dexc:                          # noqa: BLE001
            # 连 disable 都失败必须明说，不能让调用方以为已经止血
            out["disabled"] = False
            out["error"] += ";disable_failed:" + type(dexc).__name__
        return out

    # **回读确认**：rollback 的语义是「恢复旧版本正文并重新启用」，
    # 库里 status 仍会是 active；不能靠自己写个 'rolled_back' 字符串冒充。
    # 真正的证据是 active_version 退回了更旧的快照。
    after = None
    for row in _sk.list_skills(store):
        if row["id"] == skill_id:
            after = row
            break
    after_version = int((after or {}).get("active_version")
                        or (after or {}).get("version") or 0)
    out["status"] = (after or {}).get("status")
    out["version_before"] = before_version
    out["version_after"] = after_version
    out["rolled_back"] = after_version < before_version
    if not out["rolled_back"]:
        out["error"] = "rollback_noop:版本未退回(%d->%d)" % (
            before_version, after_version)
    out["rolled_back_at"] = (after or {}).get("rolled_back_at")
    # 回滚是副作用，必须留痕（谁因什么把技能退了）
    try:
        with store.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO skill_rollback_audit("
                "id,skill_id,ts,reasons,baseline,online) VALUES(?,?,?,?,?,?)",
                ("rb_" + str(skill_id) + "_" + str(int(__import__("time").time())),
                 skill_id, __import__("time").time(),
                 json.dumps(reasons, ensure_ascii=False),
                 json.dumps(baseline, ensure_ascii=False),
                 json.dumps(online, ensure_ascii=False)))
    except Exception:                                       # noqa: BLE001
        out.setdefault("audit_error", True)
    return out


def closed_loop(store, steps, *, target_cases, guard_cases, run_case,
                training_cases=(), task_type="newdomain",
                expected_total=None, policy=None):
    """一步到位：合成 → 门禁 → 独立验证 → 晋升/回滚。"""
    s = synthesize(store, steps, task_type=task_type,
                   expected_total=expected_total, policy=policy)
    if not s["accepted"].get("accepted") or s["skill"] is None:
        return {"stage": "synthesis", "synthesis": s,
                "promotion": None}
    p = evaluate_and_promote(
        store, s["skill"], target_cases, guard_cases, run_case,
        training_cases=training_cases)
    return {"stage": "promotion", "synthesis": s, "promotion": p}


def describe():
    return {"version": 1, "tools": list(NEW_DOMAIN_TOOLS),
            "stages": ["diagnose", "synthesize", "sandbox_accept",
                       "independent_eval", "promote_or_rollback"]}
