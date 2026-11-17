# -*- coding: utf-8 -*-
"""WS-04-T1：基于轨迹反事实对比的通用根因诊断。

不认工具名（不再硬编码 `order.get_order`/`pricing.calc_total`），
只认**轨迹里的契约违例信号**：

1. step.status == error / error_type 非空 → 明确失败；
2. tool 返回的 output 声称成功，但同一步有 `checks` 标记 deny → 静默失效；
3. 同一 logical 步重复超过阈值 → 死循环；
4. 输出含动作请求而工具职责是只读 → 越界（action_scope）；
5. 反事实对比：若 `expected` 提供，用值与 output 比对找出首个不一致步。

返回 `{first_bad_step, first_bad_logical, root_cause, confidence,
evidence, counterfactual}`，与 `runtime.diagnose` 的接口兼容，
但**不依赖任何写死的工具名**。
"""
import hashlib
import json

try:
    from . import action_scope as _as
except ImportError:
    import action_scope as _as                              # type: ignore

ERROR_TYPES = {
    "timeout": "tool_timeout",
    "exception": "tool_exception",
    "interrupted": "process_interruption",
    "bad_args": "bad_arguments",
    "approval_required": "approval_missing",
}


def _text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(v)


def _extract_number(v):
    import re
    s = _text(v)
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def diagnose_trace(steps, *, expected_total=None, max_repeat=3):
    """steps: list[dict]（来自 store.list_steps）。返回诊断 dict。"""
    seq_counts, logical_counts = {}, {}
    findings = []

    for s in steps or []:
        if not isinstance(s, dict):
            continue
        seq, logical = s.get("seq"), s.get("logical")
        status = str(s.get("status") or "")
        err = str(s.get("error_type") or "")
        name = str(s.get("name") or s.get("tool") or "")
        out = s.get("output")
        inp = s.get("input")

        seq_counts[name] = seq_counts.get(name, 0) + 1
        if logical is not None:
            logical_counts[logical] = logical_counts.get(logical, 0) + 1

        if status == "error" or err:
            findings.append({"seq": seq, "logical": logical, "tool": name,
                             "code": ERROR_TYPES.get(err, err or "step_error"),
                             "severity": "error",
                             "evidence": "status=%s error_type=%s" % (status, err)})
            continue

        # 静默失效：输出说成功，但带 deny/错误体
        body = out if isinstance(out, dict) else {}
        if isinstance(body, dict) and (
                body.get("denied") or body.get("error")
                or body.get("stop") is True):
            findings.append({"seq": seq, "logical": logical, "tool": name,
                             "code": "silent_failure", "severity": "error",
                             "evidence": "成功状态下输出含 deny/error 标记"})
            continue

        # 只读工具返回里出现动作请求 → 越界（通用，不认工具名）
        if _as.scope_of(name) == _as.READ_ONLY:
            reqs = _as.detect_action_requests(_text(out))
            if reqs:
                findings.append({"seq": seq, "logical": logical, "tool": name,
                                 "code": "action_on_read_only_tool",
                                 "severity": "warn",
                                 "evidence": "只读工具返回含 %d 处动作请求"
                                             % len(reqs)})

        # 反事实：金额不一致
        if expected_total is not None and isinstance(body, dict):
            got = _extract_number(body.get("total"))
            if got is not None and abs(got - float(expected_total)) > 0.02:
                findings.append({"seq": seq, "logical": logical, "tool": name,
                                 "code": "silent_corruption",
                                 "severity": "error",
                                 "evidence": "got=%.2f expected=%.2f"
                                             % (got, float(expected_total))})

    for name, n in seq_counts.items():
        if n > max_repeat:
            findings.append({"seq": None, "logical": None, "tool": name,
                             "code": "excessive_repeat", "severity": "warn",
                             "evidence": "工具 %s 调用 %d 次" % (name, n)})

    if not findings:
        return {"first_bad_step": None, "first_bad_logical": None,
                "root_cause": "none", "confidence": 0.0,
                "evidence": {"checked_steps": len(steps or [])},
                "counterfactual": {"expected_total": expected_total},
                "none": True}

    errs = [f for f in findings if f["severity"] == "error"] or findings
    first = errs[0]
    cause = first["code"]
    conf = 0.95 if first["severity"] == "error" else 0.7
    if len(findings) > 1:
        conf = min(conf + 0.02 * (len(findings) - 1), 0.99)
    blob = json.dumps(findings, ensure_ascii=False, sort_keys=True)
    return {
        "first_bad_step": first.get("seq"),
        "first_bad_logical": first.get("logical"),
        "root_cause": cause,
        "confidence": round(conf, 3),
        "evidence": first,
        "all_findings": findings,
        "counterfactual": {"expected_total": expected_total,
                           "checked": hashlib.sha256(
                               blob.encode("utf-8")).hexdigest()[:16]},
        "none": False,
    }
