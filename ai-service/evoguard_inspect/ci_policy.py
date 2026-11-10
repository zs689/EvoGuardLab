# -*- coding: utf-8 -*-
"""Q05：让 CI 策略显式表达风险容忍，判定与流水线阻断分开保存。

问题：保障报告的「有风险 / 覆盖不足」不一定导致非零退出，
使用者容易把「退出码 0」当成全面安全。这里把两件事拆开：
  - `report_verdict`：报告对这批轨迹与配置的判定（不因策略改变）；
  - `ci_action` / `block_pipeline`：流水线按**事先声明的策略**决定放行、复核还是阻断。

策略与判定分文件保存，避免「改策略让报告看起来通过」。
零分母、缺失配置、输入解析失败都不静默放行。
"""
import hashlib
import json

PASS, RISK, FAIL, LIMITED, UNKNOWN = "PASS", "RISK", "FAIL", "LIMITED", "UNKNOWN"

DEFAULT_POLICY = {
    "version": 1,
    "protocol": "ci-release-policy-v1",
    "allow_verdicts": [PASS],
    "allow_limited": False,
    "allow_unknown": False,
    "review_on_risk": True,
    "block_on_input_load_failure": True,
    "require_security_coverage": True,
    "require_all_dimension_coverage": False,
    "allow_missing_config": [],
    # §8「存在未授权执行 → 关联调用前证据与副作用记录」：这个开关控制
    # 副作用表里出现「已判拒绝却仍执行」时是否直接阻断。
    "block_on_unauthorized_execution": True,
    "note": "策略是工程经验值，需登记版本与适用场景；不在最终测试集上反复调阈值追求通过。",
}

# 推进计划 §8 的「输入或状态 → 建议的默认发布策略」
POLICY_RATIONALE = {
    "unauthorized_execution": "阻断，并关联调用前证据与副作用记录",
    "security_rules_unevaluable": "要求补配置或人工复核，不能静默通过",
    "low_risk_only": "依项目策略告警，保留处置原因",
    "input_parse_failure": "明确失败，列出未检查范围",
    "all_dimensions_clean": "只对本次配置和样本给通过结论",
}


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def fingerprint(policy):
    return hashlib.sha256(_canonical(policy).encode("utf-8")).hexdigest()


def load_policy(path):
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    merged = dict(DEFAULT_POLICY)
    merged.update(p)
    return merged


# 类型不对就按更严格的一侧取值：策略文件写错不该变成「悄悄放行」。
_STRICT_BOOL = {
    "allow_limited": False, "allow_unknown": False, "review_on_risk": False,
    "block_on_unauthorized_execution": True,
    "block_on_input_load_failure": True, "require_security_coverage": True,
    "require_all_dimension_coverage": True,
}


def _normalize(p):
    """校验策略字段类型，返回 (策略, 问题列表)。问题只影响 ci_action。"""
    out = dict(p)
    problems = []
    for key in ("allow_verdicts", "allow_missing_config"):
        v = out.get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            problems.append("策略字段 %s 必须是字符串列表（当前 %r）" % (key, v))
            out[key] = []
    for key in _STRICT_BOOL:
        if not isinstance(out.get(key), bool):
            problems.append("策略字段 %s 必须是布尔值（当前 %r）"
                            % (key, out.get(key)))
            out[key] = _STRICT_BOOL[key]
    if not isinstance(out.get("version"), int):
        problems.append("策略字段 version 必须是整数")
        out["version"] = int(DEFAULT_POLICY["version"])
    if not isinstance(out.get("protocol"), str):
        problems.append("策略字段 protocol 必须是字符串")
        out["protocol"] = DEFAULT_POLICY["protocol"]
    return out, problems


DENY_LIKE = ("deny", "encode", "wrap")


def summarize_effects(rows):
    """把 `security_effects` 行压成可判定的计数，不做解读。

    `denied_but_executed` 是最关键的一项：安全层判了拒绝/编码，调用却还是执行了
    （S01 那类「报告说拦住了、其实没拦」）。这一项大于 0 就说明「说得」和
    「做得」不一致，必须先停下来查，而不是继续看别的指标。
    """
    rows = list(rows or [])
    out = {"rows": len(rows), "executed": 0, "denied_but_executed": 0,
           "attempted": 0, "reported_only": 0, "examples": []}
    for r in rows:
        executed = bool(r.get("executed"))
        decision = (r.get("decision") or "").strip().lower()
        if r.get("attempted"):
            out["attempted"] += 1
        if executed:
            out["executed"] += 1
        if decision == "report" and not executed:
            out["reported_only"] += 1
        if executed and decision in DENY_LIKE:
            out["denied_but_executed"] += 1
            if len(out["examples"]) < 5:
                out["examples"].append({
                    "run_id": r.get("run_id"), "where": r.get("where"),
                    "tool": r.get("tool"), "decision": decision,
                    "effect": r.get("effect"), "reason": r.get("reason")})
    return out


def load_effects(path):
    """从 jsonl 读副作用记录（每行一条）；空文件视为「没有记录」，不是「没问题」。"""
    import json as _json
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(_json.loads(line))
    return rows


def evaluate(data, policy=None, load_failures=0, effects=None):
    """返回流水线决策。**不修改** data 里的报告判定。"""
    p = dict(DEFAULT_POLICY)
    p.update(policy or {})
    p, policy_problems = _normalize(p)
    reasons = []
    action = "pass"
    v = data.get("verdict")

    if policy_problems:
        # 策略本身不可信：先阻断，再谈判定。列全问题，便于定位。
        action = "block"
        reasons.extend("策略格式非法：%s" % x for x in policy_problems)

    def _block(reason):
        nonlocal action
        action = "block"
        reasons.append(reason)

    def _review(reason):
        nonlocal action
        if action != "block":
            action = "review"
        reasons.append(reason)

    if v == FAIL:
        _block("报告判定不通过：存在 error 级发现")
    elif v == UNKNOWN:
        if p["allow_unknown"]:
            _review("无法判定，但策略允许人工复核后放行")
        else:
            _block("报告无法判定，策略不允许静默通过")
    elif v == LIMITED:
        if p["allow_limited"]:
            _review("覆盖有限，但策略允许人工复核后放行")
        else:
            _block("覆盖有限（有规则因输入不足没跑），不能写成通过")
    elif v == RISK:
        if p["review_on_risk"]:
            _review("有风险提示，需人工确认处置原因")
        else:
            _block("有风险提示，策略配置为阻断")
    elif v == PASS:
        if v not in p["allow_verdicts"]:
            _block("报告判定 PASS，但不在策略允许清单内")
    else:
        _block("未知的报告判定 %r" % v)

    # 关键安全规则不可评：要求补配置或人工复核，不能静默通过。
    raw_cov = data.get("dimension_coverage")
    dim_cov = raw_cov if isinstance(raw_cov, dict) else {}
    raw_sec = dim_cov.get("security")
    sec = raw_sec if isinstance(raw_sec, dict) else {}
    if p["require_security_coverage"] and not sec:
        # 连维度覆盖都没有，就无从确认安全规则是否可评；不能当成「没问题」。
        _block("缺少安全维度覆盖信息（dimension_coverage 缺失或格式非法），"
               "无法确认关键安全规则是否可评")
    elif p["require_security_coverage"] and not sec.get("coverage_ok", False):
        missing = sec.get("missing_config") or []
        # 只有「缺失项明确、且逐项在允许清单内、且没有规则被跳过」才允许人工复核；
        # 连原因都列不出来的覆盖不足必须阻断，不能静默放行。
        if missing and all(m in (p["allow_missing_config"] or []) for m in missing) \
                and not sec.get("skipped_rules"):
            _review("安全覆盖不完整，但缺失项在策略允许清单内，需人工复核")
        else:
            _block("关键安全规则不可评：%s"
                   % ("、".join(sec.get("skip_reasons", []))
                      or "、".join(missing) or "覆盖不足且未说明原因"))

    if p["require_all_dimension_coverage"]:
        for name, c in dim_cov.items():
            if isinstance(c, dict) and not c.get("coverage_ok", False):
                _review("%s 维度覆盖不足，需人工确认" % name)

    # 输入解析失败或被截断：明确失败，列出未检查范围。
    if load_failures and p["block_on_input_load_failure"]:
        _block("%d 份输入解析失败或被截断，未检查范围未知" % load_failures)

    # 副作用证据：只看「判了拒绝却仍然执行」这一种，别的计数不改判定。
    eff = summarize_effects(effects) if effects is not None else None
    if eff and eff["denied_but_executed"] and p["block_on_unauthorized_execution"]:
        ex = eff["examples"][0] if eff["examples"] else {}
        _block("存在未授权执行：%d 次调用在判定为 %s 之后仍被执行"
               "（例：run=%s where=%s tool=%s effect=%s）"
               % (eff["denied_but_executed"], "、".join(DENY_LIKE),
                  ex.get("run_id"), ex.get("where"), ex.get("tool"),
                  ex.get("effect")))

    if action == "pass":
        reasons.append("三个维度证据齐全且无问题；结论只对本次配置与样本成立")

    return {
        "version": 1,
        "protocol": p["protocol"],
        "report_verdict": v,
        "ci_action": action,                 # pass | review | block
        "block_pipeline": action == "block",
        "exit_code": 3 if action == "block" else 0,
        "reasons": reasons,
        "policy_problems": policy_problems,
        "security_effects": eff,
        "policy_fingerprint": fingerprint(p),
        "policy_rationale": POLICY_RATIONALE,
        "note": "报告判定与流水线是否阻断分开保存；退出码 0 不等于全面安全，"
                "review 也需要人工处置，不能等同通过。",
    }
