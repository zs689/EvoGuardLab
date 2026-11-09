# -*- coding: utf-8 -*-
"""标注集评测：把「检出 / 误报 / 不可判定」算清楚。

口径（必须先看，否则数字会被误读）：
  - **确诊（error 级）**：`failed` 轨迹里至少出现一条 error 级发现 —— 只有这一档能直接判失败。
  - **提示（warn 级及以上）**：出现 warn 或 error。很多规则按设计就是 warn（例如数字无出处），
    只用 error 口径算召回会把它们全算成漏报，所以要分两档报。
  - **误报**：`clean` 或 `recovered` 轨迹里出现 error 级发现。
    `recovered` 单独算一档，因为「已经恢复的故障」被报成错误是最典型的误伤。
  - **不可判定**：这条轨迹上没有任何 error 级规则具备输入条件（缺 token/时间戳/白名单…）。
    这类轨迹不进分母 —— 拿它算「漏报」是冤枉规则，算「正确」是自欺。
  - 分母为 0 时比率给 `None`，不给 0。0 和「没算」是两件事。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from . import coverage  # noqa: E402
from .rules import registry  # noqa: E402


def _ratio(num, den):
    return None if not den else round(float(num) / den, 3)


def is_flagged(issues, level="error"):
    order = {"info": 1, "warn": 2, "error": 3}
    want = order[level]
    return any(order.get(i.get("severity"), 0) >= want for i in issues)


def applicable_error_rules(trace, cfg):
    """这条轨迹上有多少条 error 级规则真的具备输入条件。"""
    facts = coverage.trace_facts(trace)
    facts.update(coverage.config_facts(cfg))
    n = 0
    for rid, meta in registry.RULES.items():
        if meta.get("severity") != "error":
            continue
        if not coverage.missing(facts, coverage.TRACE_REQUIRES.get(rid, ())):
            n += 1
    return n


def make_record(key, label, trace, issues, cfg):
    """单条轨迹的评测记录。"""
    return {
        "key": key,
        "label": label,
        "flagged_error": is_flagged(issues, "error"),
        "flagged_warn": is_flagged(issues, "warn"),
        "error_count": sum(1 for i in issues if i.get("severity") == "error"),
        "warn_count": sum(1 for i in issues if i.get("severity") == "warn"),
        "rules": sorted({i["rule_id"] for i in issues}),
        "applicable_error_rules": applicable_error_rules(trace, cfg),
    }


def summarize(records):
    """按类别汇总。返回的每个比率都可能是 None（分母为 0）。"""
    judged = [r for r in records if r["applicable_error_rules"] > 0]
    unjudgeable = [r["key"] for r in records if r["applicable_error_rules"] == 0]

    failed = [r for r in judged if r["label"] == "failed"]
    clean = [r for r in judged if r["label"] == "clean"]
    recovered = [r for r in judged if r["label"] == "recovered"]
    good = clean + recovered

    tp = sum(1 for r in failed if r["flagged_error"])
    fp = sum(1 for r in good if r["flagged_error"])
    fn = len(failed) - tp
    tn = len(good) - fp

    per_rule = {}
    for r in failed:
        for rid in r["rules"]:
            per_rule[rid] = per_rule.get(rid, 0) + 1

    # 两档口径分开算：
    #   confirm（error 级）= 可以据此阻断/判失败的结论
    #   alert（warn 级及以上）= 提示到了、需要人看一眼
    # 只报一档会误导：很多规则按设计就是 warn，用 error 口径算召回会把它们全算成漏报。
    tp_alert = sum(1 for r in failed if r["flagged_warn"])
    fp_alert = sum(1 for r in good if r["flagged_warn"])
    missed = [{"key": r["key"], "rules": r["rules"]}
              for r in failed if not r["flagged_error"]]

    return {
        "n_total": len(records),
        "n_judged": len(judged),
        "n_unjudgeable": len(unjudgeable),
        "unjudgeable_keys": unjudgeable,
        "by_label": {
            "failed": {"n": len(failed), "flagged": tp,
                       "recall": _ratio(tp, len(failed))},
            "clean": {"n": len(clean), "flagged": sum(1 for r in clean if r["flagged_error"]),
                      "false_alarm_rate": _ratio(sum(1 for r in clean if r["flagged_error"]),
                                                 len(clean))},
            "recovered": {"n": len(recovered),
                          "flagged": sum(1 for r in recovered if r["flagged_error"]),
                          "false_alarm_rate": _ratio(
                              sum(1 for r in recovered if r["flagged_error"]),
                              len(recovered))},
        },
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "confusion_alert": {"tp": tp_alert, "fp": fp_alert,
                            "fn": len(failed) - tp_alert,
                            "tn": len(good) - fp_alert},
        "recall_alert": _ratio(tp_alert, len(failed)),
        "precision_alert": _ratio(tp_alert, tp_alert + fp_alert),
        "missed_error": missed,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f1": _f1(_ratio(tp, tp + fp), _ratio(tp, tp + fn)),
        "warn_only_on_good": sum(1 for r in good if r["flagged_warn"]),
        "rules_hit_on_failed": dict(sorted(per_rule.items(), key=lambda kv: -kv[1])),
    }


def _f1(p, r):
    if p is None or r is None or (p + r) == 0:
        return None
    return round(2 * p * r / (p + r), 3)
