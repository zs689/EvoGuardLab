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
import re
import uuid

try:
    from . import core
except ImportError:
    import core                                             # type: ignore

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
               failure_type=None, expected_total=None, policy=None,
               repair_steps=None, env_spec=None, require_info_gain=False):
    """从失败轨迹合成一个候选 Skill（未落库、未晋升）。

    返回 {diagnosis, skill, accepted}；门禁拒绝时 accepted 里带 errors。
    """
    diag = _td.diagnose_trace(steps, expected_total=expected_total)
    if diag.get("none"):
        return {"diagnosis": diag, "skill": None,
                "accepted": {"accepted": False,
                             "errors": ["轨迹无违例，无可合成的经验"]}}
    cause = failure_type or diag["root_cause"]
    # ZERO-05-T1：零增益候选**入口预筛**。历史数据 77 个候选仅 7 个
    # PROMOTE、83% 零增益；这些几乎全是「没有具体证据的泛泛套话」。
    # 在构造完整 skill 之前先廉价判一次，省掉无谓计算并给出明确理由。
    if require_info_gain:
        # 用同一个提取器先廉价判一次「有没有具体证据」，而不是看
        # 有没有 repair_steps —— 失败轨迹里的 KeyError 键名/边界数字
        # 本身就是可提取证据，不能误杀（既有测试覆盖了这个情形）。
        fail_here = [x for x in (steps or []) if isinstance(x, dict)]
        ok_here = [x for x in (repair_steps or []) if isinstance(x, dict)]
        cheap_inv = extract_env_invariants(fail_here, ok_here, env_spec)
        cheap_fix = _extract_structured(diag, steps, repair_steps, cause)
        concrete_fix = str(cheap_fix.get("fix_pattern") or "") not in (
            "", "unknown_no_repair_trace")
        if not (cheap_inv or concrete_fix):
            return {
                "diagnosis": diag, "skill": None,
                "accepted": {
                    "accepted": False,
                    "errors": ["入口剪枝（信息增量门禁）：失败轨迹无可提取的"
                               "具体环境证据或修复模式；按历史 83% 零增益"
                               "规律拒绝生成泛泛套话技能"],
                    "pruned_at": "synthesize_entry",
                },
            }
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
    # SCORE-04-T1：从**真实的失败->成功成对轨迹**提取可执行经验。
    # 没有成功轨迹时如实标 unknown_*，绝不编造「修复模式」。
    structured = _extract_structured(diag, steps, repair_steps, cause,
                                     env_spec=env_spec)
    skill.update(structured)
    # 候选只能声明只读工具：写/外发工具必须走审批，不能由技能声明。
    skill["allowed_tools"] = list(NEW_DOMAIN_TOOLS[:3])
    pol = policy or {"allowed_tools": list(NEW_DOMAIN_TOOLS),
                     "write_tools": ["sql.update"]}
    try:
        accepted = _cs.accept(skill, pol, require_info_gain=require_info_gain)
    except _cs.SkillRejected as e:
        accepted = {"accepted": False, "errors": list(e.args[0])
                    if e.args and isinstance(e.args[0], list) else [str(e)]}
    return {"diagnosis": diag, "skill": skill, "accepted": accepted}


def _stderr_of(step):
    """取一个步骤的错误文本（stderr 优先，其次 error/output）。"""
    out = step.get("output")
    if isinstance(out, dict):
        return str(out.get("stderr") or out.get("error") or "")
    if isinstance(out, str):
        return out
    return ""


# EVO-REBUILD-01-T1：私有环境约定的识别特征。
# 与「通用工程常识」的区别：这些是模型预训练**不可能知道**的项目/环境事实，
# 必须从真实失败证据里提取，提不到就不产出（绝不编造）。
_NAMING_EV = re.compile(r"KeyError:\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
_ATTR_EV = re.compile(r"AttributeError:.*?['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
_PAGING_EV = re.compile(
    r"(?:offset|page|per_?page|page_?size|\bsize\b|cursor|next_cursor|"
    r"\blimit\b).{0,80}?"
    r"(?:>=\s*1|must|invalid|out of range|422|start at 1|clamp|"
    r"exceeds|prefix|none)", re.I | re.S)
_TZ_EV = re.compile(
    r"(?:timezone|tzinfo|iso\s*8601|invalid date|naive datetime|"
    r"date format|tz-aware|ending with z)", re.I)
_CFG_EV = re.compile(
    r"(?:yaml|toml|json|ini|env).{0,60}?(?:parse|decode|scan|format).{0,30}?error",
    re.I | re.S)
# EVO-BENCH-01：金额「单位」约定（分 vs 元）——预训练里没有哪个是默认。
_UNIT_EV = re.compile(
    r"(?:cents|centavos|divide by 100|convert to yuan|unit mismatch)", re.I)
# 多登记处耦合：声明与校验表必须同步，只改一处会静默失效。
_COUPLING_EV = re.compile(
    r"(?:whitelist|allowlist|\ballowed\b|required table|not treated like|"
    r"missing from|must also|must be added to|sync(?:hronize)? with)", re.I)


_CODEY = ("import ", "def ", "exec(", "eval(", "__", "os.", "sys.",
          "subprocess", "lambda ", "compile(")


def _sanitize_evidence(text):
    """清洗观测证据，避免 traceback 片段触发 conditional_skill 的代码特征黑名单。

    证据是「观察到的事实」而非代码，但仍按最严门禁处理：压成单行、
    限长、把类代码标记替换成占位符。不因此放宽任何校验。
    """
    t = " ".join(str(text or "").split())[:160]
    for m in _CODEY:
        t = t.replace(m, "·")
    return t


#: EVO-DEEP-01-T1：规约「槽位」。同一槽位的新规约表示环境语义发生了变更
#: （例如分页从 1-based 改成 0-based），旧规约必须退役而不是并列共存。
_SLOT_HINTS = {
    "pagination": (
        ("cursor", "pagination.cursor_format"),
        ("limit", "pagination.limit_cap"),
        ("尾页", "pagination.tail_sentinel"),
        ("1 开始", "pagination.start_index"),
        ("从 1", "pagination.start_index"),
        ("从 0", "pagination.start_index"),
    ),
    "unit": (("分", "unit.scale"), ("元", "unit.scale")),
    "timezone": (("utc", "timezone.suffix"), ("z 结尾", "timezone.suffix"),
                 ("时区", "timezone.offset")),
    "coupling": (("required", "coupling.required_sync"),
                 ("同步", "coupling.required_sync")),
}


def _slot_key(domain, rule):
    """把一条规约映射到稳定槽位；同槽位视为「同一件事的不同版本」。"""
    r = str(rule or "").lower()
    for kw, key in _SLOT_HINTS.get(str(domain), ()):
        if kw.lower() in r:
            return key
    return "%s.general" % domain


def active_invariants(skill):
    """当前生效的规约（排除已被新版本取代的旧规约）。"""
    return [x for x in ((skill or {}).get("env_invariants") or [])
            if isinstance(x, dict) and not x.get("superseded_by")]


def extract_env_invariants(fail_steps, repair_steps=None, env_spec=None):
    """从**真实失败轨迹**提取私有环境约定（env_invariant）。

    只输出有证据的条目：每条都必须能指回一段真实 stderr/错误文本。
    提不到就返回空列表 —— 空列表会被 conditional_skill 的信息增量门禁拒收，
    这正是「泛泛流程提示一律不要」的落实。

    `env_spec` 允许调用方显式提供项目私有规约
    （[{domain, rule, match}]），仍要求轨迹里命中 `match` 才算有证据，
    避免把「没发生过的规约」写进技能。
    """
    fails = [s for s in (fail_steps or []) if isinstance(s, dict)]
    texts = []
    for s in fails:
        if s.get("status") == "ok" and not s.get("error_type"):
            continue
        t = _stderr_of(s)
        if t.strip():
            texts.append(t)
    if not texts:
        return []
    blob = "\n".join(texts)
    invs = []

    def _add(domain, rule, evidence):
        ev = _sanitize_evidence(evidence)
        for x in invs:
            if x["domain"] == domain and x["rule"] == rule:
                return
        invs.append({"domain": domain, "rule": rule[:160], "evidence": ev,
                     "slot_key": _slot_key(domain, rule)})

    # 1) 显式规约：要求轨迹里真有命中（evidence 非空）
    for spec in (env_spec or []):
        if not isinstance(spec, dict):
            continue
        match = str(spec.get("match") or "")
        rule = str(spec.get("rule") or "").strip()
        if not rule:
            continue
        hit = ""
        if match:
            m = re.search(match, blob, re.I | re.S)
            hit = m.group(0) if m else ""
        if hit:
            _add(str(spec.get("domain") or "project_rule"), rule, hit)

    # 2) 命名约定：真实 KeyError/AttributeError 的键名就是环境事实
    m = _NAMING_EV.search(blob) or _ATTR_EV.search(blob)
    if m:
        key = m.group(1)
        if "_" in key or key.islower():
            _add("naming",
                 "字段/键名必须与既有定义完全一致（本项目用 snake_case，"
                 "如 %s），不要自行改成 camelCase" % key,
                 m.group(0))
        else:
            _add("naming",
                 "字段/键名必须与既有定义完全一致（如 %s）" % key, m.group(0))

    # 3) 分页约定（按证据生成具体条款，不套泛泛模板）
    m = _PAGING_EV.search(blob)
    if m:
        ev = m.group(0)
        parts = []
        if re.search(r"c_\s*prefix", blob, re.I):
            parts.append("cursor 必须以 c_ 开头")
        if re.search(r"next_cursor\s*=\s*none", blob, re.I):
            parts.append("尾页 next_cursor 必须是 None 而非空串")
        mm = re.search(r"limit exceeds (\d+)", blob, re.I) or \
            re.search(r"clamp(?:ed)? to (\d+)", blob, re.I)
        if mm:
            parts.append("limit 超过 %s 必须抛 ValueError" % mm.group(1))
        if re.search(r"invalid cursor", blob, re.I):
            parts.append("非法 cursor 必须抛 ValueError")
        if not parts:
            # 兜底也必须反映**证据里的实际起始索引**，不能写死 1：
            # 否则「1-based -> 0-based」这种环境反转会提取出同一条规则，
            # 冲突检测永远触发不了。
            mm2 = re.search(r"start(?:s)? at (\d+)|from (\d+)", blob, re.I)
            base = (mm2.group(1) or mm2.group(2)) if mm2 else None
            parts.append("分页从 %s 开始且每页有上限，越界要显式拒绝"
                         % (base if base is not None else "1"))
        _add("pagination", "；".join(parts), ev)

    # 4) 时区/日期格式约定
    m = _TZ_EV.search(blob)
    if m:
        ev = m.group(0)
        # 证据里出现「以 Z 结尾/ending with Z」时，把这条具体写进规约
        ztail = ""
        if re.search(r"ending with z|ends? with z", blob, re.I):
            ztail = "，且字符串必须以 Z 结尾"
        _add("timezone",
             "日期时间必须带时区并按 ISO 8601 传输（UTC）%s，"
             "不接受 naive datetime" % ztail, ev)

    # 5) 配置格式约定
    m = _CFG_EV.search(blob)
    if m:
        _add("config_format",
             "配置必须是本项目既定格式并可被严格解析，缩进/引号错误直接失败",
             m.group(0))

    # 6) 金额单位约定（分 vs 元）
    m = _UNIT_EV.search(blob)
    if m:
        _add("unit",
             "金额对外单位与本项目内部一致（分），不要做除以 100 的元换算",
             m.group(0))

    # 7) 多登记处耦合
    m = _COUPLING_EV.search(blob)
    if m:
        ev = m.group(0)
        # 证据里点名了哪个登记处就写哪个，避免「白名单」这类泛称对不上代码
        named = []
        for name in ("FIELDS", "REQUIRED", "ALLOWED", "VALID_FIELDS"):
            if name in blob:
                named.append(name)
        if "REQUIRED" in named and "FIELDS" in named:
            rule = ("新字段必须同时登记到 FIELDS 与 REQUIRED 两处，"
                    "并在 REQUIRED 中设为 True（新字段默认必填，"
                    "与既有字段同等对待），只改 FIELDS 会放行空值")
        elif named:
            # 即使证据只点名了一个登记处，也要说清另一处叫什么、怎么改
            rule = ("新字段必须同时登记到 FIELDS 与 %s，并在 %s 中设为 True"
                    "（默认必填，与既有字段同等对待）；只加字段声明会放行空值"
                    % (named[0], named[0]))
        else:
            # 证据只点名了一个登记处（如 "missing from REQUIRED table"）时
            # 也要给出可执行要求；旧泛化表述让模型以为只是"加进 FIELDS"。
            rule = ("新字段必须同时登记到 FIELDS 与 REQUIRED，并在 REQUIRED "
                    "中设为 True（默认必填，与既有字段同等对待），"
                    "只改 FIELDS 会放行空值")
        _add("coupling", rule, ev)

    return invs[:4]


def extract_pre_checks(fail_steps, repair_steps=None):
    """从修复轨迹提取**可执行**前置/后置检查（pre_check）。

    只记录真实发生过、且能复用的动作：修完跑过测试、改动跨多个文件。
    没有真实修复动作就返回空（不编造检查项）。
    """
    repairs = [s for s in (repair_steps or []) if isinstance(s, dict)
               and s.get("status") == "ok"]
    if not repairs:
        return []
    checks = []
    test_tools = [str(s.get("name") or "") for s in repairs
                  if str(s.get("name") or "").startswith("code.")
                  or "test" in str(s.get("name") or "")]
    if test_tools:
        checks.append({"action": "run_tests_after_write",
                       "trigger": "write",
                       "tool": sorted(set(test_tools))[0],
                       "detail": "写操作后必须跑一次验证，再宣称完成"})
    files = []
    for s in repairs:
        out = s.get("output")
        if isinstance(out, dict) and out.get("path"):
            p = str(out["path"])
            if p not in files:
                files.append(p)
    if len(files) >= 2:
        checks.append({"action": "check_coupled_files",
                       "trigger": "write",
                       "files": files[:4],
                       "detail": "这些文件存在隐式耦合，改一个必须同步核对其余"})
    return checks


def _extract_structured(diag, steps, repair_steps, cause, env_spec=None):
    """从失败/成功成对轨迹提取四类结构化字段（不做模型调用）。

    error_signature：异常类型 + 首次坏步的错误类型（可精确检索）
    diagnostic_steps：为了定位根因而真实读过的步骤名
    fix_pattern：成功轨迹里真正做过的修复动作（只取可核对的字符串）
    verification_check：成功轨迹的验证步骤与返回值
    """
    fail = [s for s in (steps or []) if isinstance(s, dict)]
    ok = [s for s in (repair_steps or []) if isinstance(s, dict)]
    sig = set()
    for s in fail:
        et = str(s.get("error_type") or "")
        if et:
            sig.add(et)
        stderr = str((s.get("output") or {}).get("stderr") or "") \
            if isinstance(s.get("output"), dict) else ""
        import re as _re
        sig.update(_re.findall(r"([A-Za-z]+Error)\b", stderr)[:4])
    tools = [str(s.get("name") or "") for s in fail
             if s.get("name") and s.get("status") != "ok"][:4]
    if not sig:
        sig.add(str(cause or "unknown"))
    fix = []
    verify = []
    if ok:
        for s in ok:
            name = str(s.get("name") or "")
            out = s.get("output")
            detail = ""
            if isinstance(out, dict):
                detail = str(out.get("replaced") or out.get("stderr")
                             or out.get("returncode") or "")[:120]
            if name:
                if s.get("status") == "ok" and \
                        ("patch" in name or "write" in name or "fix" in name):
                    fix.append((name + ": " + detail).strip())
                elif name.startswith("code.") or "test" in name:
                    verify.append((name + ": " + detail).strip())
    return {
        "error_signature": "|".join(sorted(sig)),
        "diagnostic_steps": tools,
        "fix_pattern": " -> ".join(fix) if fix else "unknown_no_repair_trace",
        # EVO-REBUILD-01-T1：两类高信息增量产物。提不到具体证据就是空列表，
        # 交给 conditional_skill 的信息增量门禁拒收（不再输出泛泛提示）。
        "env_invariants": extract_env_invariants(fail, ok, env_spec),
        "pre_checks": extract_pre_checks(fail, ok),
        "verification_check": " -> ".join(verify)
                              if verify else "unknown_no_verification_trace",
        "structured_from": "paired_trace" if ok else "failure_only",
    }


def evaluate_and_promote(store, skill, target_cases, guard_cases,
                         run_case, *, training_cases=(),
                         auto_rollback_on_regression=True,
                         require_info_gain=False,
                         protocol="v2"):
    """独立验证 → 晋升；出现退化则回滚。返回完整审计 dict。

    EVO-REBUILD-01-T1：晋升前强制信息增量门禁——泛泛流程提示即使 A/B
    数字好看也不许上线（实测这类提示在真模型上 -20pp）。
    """
    # HARD-03-T1：真实晋升路径默认走统计显著 v2（n>=8 且 p<0.05）。
    # 小样本工程机制场景（<=7 题，历史闭环单测/演示范例）自动降级 v1_legacy，
    # 并在结果里显式标注 small_n_legacy —— 不冒充统计显著。
    effective_protocol = protocol
    small_n_legacy = False
    if protocol == "v2" and len(target_cases or []) < 8:
        effective_protocol = "v1_legacy"
        small_n_legacy = True
    ev = _sk.evaluate(store, skill, target_cases, guard_cases,
                      run_case, training_cases=training_cases,
                      protocol=effective_protocol)
    ev["small_n_legacy"] = small_n_legacy
    out = {"evaluation": ev, "promoted": False, "rolled_back": False,
           "status": ev["verdict"],
           "small_n_legacy": small_n_legacy}
    if require_info_gain:
        gain_errors = _cs.validate_info_gain(skill)
        if gain_errors:
            out["promoted"] = False
            out["status"] = "REJECT"
            out["info_gain_blocked"] = gain_errors
            return out
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

    # **归因门禁**：线上 runs 若没有 skill_id 归属，只能按 task_type 时间窗
    # 近似汇总 —— 那份成功率是「同类型全部任务」的，不是「用了本技能」的。
    # 拿它对 100% 基线做比较一定会判退化（实测 7 个 active 全被判死）。
    # 安全阀不能只防漏放，也必须防误杀：无归因时拒绝自动回滚。
    assoc = str(online.get("association") or "")
    if (online.get("n") or 0) > 0 and ("无 skill_id 归属" in assoc
                                       or "回退近似" in assoc):
        out["degraded"] = False
        out["attribution_missing"] = True
        out["reasons"] = [
            "线上统计无 skill_id 归属（%s），无法把退化归因到本技能；"
            "拒绝据此回滚，需先补齐运行侧 skill_id 打点" % assoc[:60]]
        out["online"] = online
        return out

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
    else:
        # 版本退回去了不等于「换药」：v1->v2 若只改了晋升标记，正文 hash 完全
        # 相同，回滚后仍是同一份退化正文 —— 此时必须 disable 才叫止血。
        try:
            before_hash = _sk.content_hash(sk)
            after_hash = _sk.content_hash(after) if after else None
        except Exception:                                # noqa: BLE001
            before_hash = after_hash = None
        if before_hash and after_hash and before_hash == after_hash:
            out["rolled_back"] = False
            out["rollback_noop_content"] = True
            try:
                _sk.disable(store, skill_id)
                out["disabled"] = True
                out["status"] = "disabled"
                out["reasons"] = list(out["reasons"]) + [
                    "回滚目标正文与当前一致（hash 相同），已改为 disable 止血"]
                after = None
                for row in _sk.list_skills(store):
                    if row["id"] == skill_id:
                        after = row
                        break
                out["status"] = (after or {}).get("status")
            except Exception as dexc:                    # noqa: BLE001
                out["error"] = "content_same_disable_failed:" + type(dexc).__name__
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


def latest_snapshot(store, skill_id):
    """取某技能最新版本的**完整快照**（含 skills 表没有的列，如 env_invariants）。

    多轮累积必须从这里恢复，而不是 `skills._get_skill()` —— 后者只返回表字段，
    会丢掉上一轮刚学到的私有规约（NEXT-01 的根因缺陷）。
    """
    with store.conn() as c:
        r = c.execute(
            "SELECT version,snapshot FROM skill_versions WHERE skill_id=? "
            "ORDER BY version DESC, rowid DESC LIMIT 1", (skill_id,)).fetchone()
    if not r:
        return None
    snap = json.loads(r["snapshot"]) if isinstance(
        r["snapshot"], str) else dict(r["snapshot"] or {})
    snap["version"] = r["version"]
    return snap


def _inv_key(inv):
    if not isinstance(inv, dict):
        return ""
    return (str(inv.get("domain") or "") + "|" +
            re.sub(r"\s+", "", str(inv.get("rule") or "")))


def refine_skill(store, skill_id, new_traces, *, require_info_gain=True,
                 env_spec=None):
    """NEXT-01：多轮累积精炼。把新失败轨迹里**新暴露**的规约合并进 Skill。

    - 从不可变版本快照恢复完整技能（保留上一轮全部规约）；
    - 新轨迹只过 `extract_env_invariants` 的证据提取器，每条必须有 evidence；
    - 按 (domain, rule 归一化) 去重，绝不把同一条规约重复写入；
    - 写入一条**新的不可变版本快照**（version+1），旧快照原样保留可回滚；
    - 没有新证据时如实返回 added=[]，不伪造"学进步"。
    """
    base = latest_snapshot(store, skill_id)
    if base is None:
        return {"accepted": {"accepted": False,
                             "errors": ["技能无版本快照，无法累积精炼"]},
                "skill": None, "added": []}
    fails = [x for x in (new_traces or []) if isinstance(x, dict)]
    repairs = [x for x in (new_traces or []) if isinstance(x, dict)
               and x.get("status") == "success"]
    new_invs = extract_env_invariants(fails, repairs, env_spec)
    if require_info_gain:
        # 每条都必须带具体 evidence，泛泛套话一律拒收
        new_invs = [x for x in new_invs
                    if str(x.get("evidence") or "").strip()]
    existing = base.get("env_invariants") or []
    seen = {_inv_key(x) for x in existing}
    added = []
    for inv in new_invs:
        k = _inv_key(inv)
        if k and k in seen:
            continue
        seen.add(k)
        added.append(inv)
    if not added:
        return {"accepted": {"accepted": False,
                             "errors": ["新轨迹没有暴露出可验证的新规约"]},
                "skill": None, "added": [], "version": base.get("version")}
    out = copy.deepcopy(base)
    out["env_invariants"] = list(existing) + added
    out["version"] = int(base.get("version") or 1) + 1
    out["id"] = skill_id
    out["created_at"] = core.now()
    # EVO-DEEP-01-T1：同槽位的新规约使旧规约退役（环境语义已变更）。
    # 旧条目不删除、只打 superseded_by，因此版本快照仍可完整回滚。
    superseded = []
    for inv in added:
        slot = inv.get("slot_key")
        if not slot:
            continue
        for old in out["env_invariants"]:
            if old is inv or old.get("superseded_by"):
                continue
            if old.get("slot_key") == slot and old.get("rule") != inv["rule"]:
                old["superseded_by"] = out["version"]
                superseded.append({"slot": slot, "old_rule": old.get("rule"),
                                   "new_rule": inv["rule"]})
    # 写成新的不可变快照（不覆盖旧版本）
    with store.conn() as c:
        c.execute(
            "INSERT INTO skill_versions(id,skill_id,version,snapshot,created_at) "
            "VALUES(?,?,?,?,?)",
            ("skv_" + uuid.uuid4().hex[:8], skill_id, out["version"],
             core.dumps(dict(out)), out["created_at"]))
    return {"accepted": {"accepted": True, "errors": []},
            "skill": out, "added": added, "superseded": superseded,
            "version": out["version"], "base_version": base.get("version")}


def closed_loop(store, steps, *, target_cases, guard_cases, run_case,
                training_cases=(), task_type="newdomain",
                expected_total=None, policy=None, env_spec=None,
                require_info_gain=True):
    """一步到位：合成 → 门禁 → 独立验证 → 晋升/回滚（线上路径，强制信息增量）。"""
    s = synthesize(store, steps, task_type=task_type,
                   expected_total=expected_total, policy=policy,
                   env_spec=env_spec, require_info_gain=require_info_gain)
    if not s["accepted"].get("accepted") or s["skill"] is None:
        return {"stage": "synthesis", "synthesis": s,
                "promotion": None}
    p = evaluate_and_promote(
        store, s["skill"], target_cases, guard_cases, run_case,
        training_cases=training_cases,
        require_info_gain=require_info_gain)
    return {"stage": "promotion", "synthesis": s, "promotion": p}


def describe():
    return {"version": 1, "tools": list(NEW_DOMAIN_TOOLS),
            "stages": ["diagnose", "synthesize", "sandbox_accept",
                       "independent_eval", "promote_or_rollback"]}


# ---------------- EVO-REBUILD-01-T2：无人值守自动监控 ----------------
#: 进程内节流计数：(store 标识, skill_id) -> 已累积的运行次数
_AUTO_COUNTS = {}
#: 自动监控窗口：每 N 次运行检查一次（越权则立即检查）
AUTO_MONITOR_WINDOW = 5


def _count_unauthorized(store, run_id):
    """统计某次运行里被门禁拦下的步骤数（越权信号）。"""
    try:
        with store.conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(steps)")}
            if "kind" not in cols:
                return 0
            row = c.execute(
                "SELECT COUNT(*) FROM steps WHERE run_id=? AND "
                "kind IN ('denied','rails_denied','loop_guard')",
                (run_id,)).fetchone()
            return int(row[0] or 0) if row else 0
    except Exception:                                  # noqa: BLE001
        return 0


def _write_auto_anchor(payload):
    """把自动回滚事件写进**库外**只追加锚点（异地取证）。"""
    path = __import__("os").environ.get("EVOGUARD_AUDIT_ANCHOR_FILE")
    if not path:
        return False
    try:
        import external_anchor as _ea
        _ea.ExternalAnchorSink(path).append_event("auto_rollback", payload)
        return True
    except Exception:                                  # noqa: BLE001
        return False


def auto_monitor_after_run(store, run_id, *, window=None,
                           force_on_unauthorized=True, anchor=True):
    """每次 run 落盘后自动检查该技能是否退化，退化即回滚/止血。

    触发条件（任一）：
      1) 该 run 出现越权/门禁拦截步骤（`unauthorized > 0`）—— 立即检查；
      2) 该技能累积运行次数达到窗口（默认 5）—— 定期体检。
    触发后调用 `monitor_and_rollback`，并把结果写入库外审计锚点。
    全程 try/except：监控失败绝不影响主运行链路。
    """
    win = int(window or AUTO_MONITOR_WINDOW)
    try:
        with store.conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
            if "skill_id" not in cols:
                return {"skipped": "no_skill_id_column"}
            row = c.execute("SELECT skill_id FROM runs WHERE id=?",
                            (run_id,)).fetchone()
        skill_id = row[0] if row else None
        if not skill_id:
            return {"skipped": "run_has_no_skill"}

        unauthorized = _count_unauthorized(store, run_id)
        key = (str(getattr(store, "path", "") or id(store)), str(skill_id))
        n = int(_AUTO_COUNTS.get(key, 0)) + 1
        _AUTO_COUNTS[key] = n

        trigger = bool(force_on_unauthorized and unauthorized > 0) or \
            (n % win == 0)
        if not trigger:
            return {"skill_id": skill_id, "checked": False,
                    "runs_since_check": n, "unauthorized": unauthorized}

        out = monitor_and_rollback(store, skill_id)
        out.update({"auto": True, "unauthorized": unauthorized,
                    "runs_since_check": n, "window": win})
        if anchor:
            out["anchored"] = _write_auto_anchor({
                "skill_id": skill_id, "run_id": run_id,
                "degraded": out.get("degraded"),
                "rolled_back": out.get("rolled_back"),
                "disabled": out.get("disabled"),
                "status": out.get("status"),
                "reasons": (out.get("reasons") or [])[:4],
                "unauthorized": unauthorized,
            })
        return out
    except Exception as exc:                           # noqa: BLE001
        # 监控自身故障不能拖垮业务运行，但要如实返回原因
        return {"error": "auto_monitor_failed:%s" % type(exc).__name__,
                "detail": str(exc)[:160]}
