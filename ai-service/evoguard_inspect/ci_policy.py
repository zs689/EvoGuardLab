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


def evaluate(data, policy=None, load_failures=0):
    """返回流水线决策。**不修改** data 里的报告判定。"""
    p = dict(DEFAULT_POLICY)
    p.update(policy or {})
    reasons = []
    action = "pass"
    v = data.get("verdict")

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
    dim_cov = data.get("dimension_coverage", {})
    sec = dim_cov.get("security", {})
    if p["require_security_coverage"] and sec and not sec.get("coverage_ok", False):
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
            if c and not c.get("coverage_ok", False):
                _review("%s 维度覆盖不足，需人工确认" % name)

    # 输入解析失败或被截断：明确失败，列出未检查范围。
    if load_failures and p["block_on_input_load_failure"]:
        _block("%d 份输入解析失败或被截断，未检查范围未知" % load_failures)

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
        "policy_fingerprint": fingerprint(p),
        "policy_rationale": POLICY_RATIONALE,
        "note": "报告判定与流水线是否阻断分开保存；退出码 0 不等于全面安全，"
                "review 也需要人工处置，不能等同通过。",
    }
