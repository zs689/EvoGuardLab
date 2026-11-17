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
except ImportError:
    import skills as _sk                                    # type: ignore
    import conditional_skill as _cs                         # type: ignore
    import trajectory_diagnose as _td                       # type: ignore


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
        # 晋升后做一次线上指标复核：模拟退化 → 必须回滚
        if auto_rollback_on_regression and _detect_regression(ev):
            _sk.rollback(store, skill["id"])
            out["rolled_back"] = True
            out["status"] = "rolled_back"
    elif ev["verdict"] == "REJECT" and auto_rollback_on_regression:
        # 被拒绝的候选不该留下 active 状态；若库里已有同名 active 则回滚
        try:
            _sk.rollback(store, skill["id"])
            out["rolled_back"] = True
        except (KeyError, ValueError):
            pass
    return out


def _detect_regression(ev):
    """模拟「验证通过后线上指标退化」的判据：负迁移越过 0 或增益转负。"""
    neg = ev.get("neg_transfer_rate")
    gain = ev.get("gain_pp")
    return bool((neg is not None and float(neg) > 0)
                or (gain is not None and float(gain) < 0))


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
