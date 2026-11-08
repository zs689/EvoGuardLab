# -*- coding: utf-8 -*-
"""检查器入口：把「一个或多个轨迹文件」变成「一份问题清单」。

设计上只做三件事：加载 → 跑规则 → 出报告。
不做的事：不执行 Agent、不调用模型、不修改被检对象。
「不调用模型」是刻意的 —— 检查器自己也得是确定性的，否则它凭什么判别人。
"""
import json
import os

from . import loaders
from .rules import trace_rules

SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


def inspect_files(paths, fmt=None):
    traces = []
    for p in paths:
        traces.extend(loaders.load_file(p, fmt))
    return traces


def summarize(issues):
    by_sev, by_rule = {}, {}
    for i in issues:
        by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        by_rule[i["rule_id"]] = by_rule.get(i["rule_id"], 0) + 1
    return by_sev, by_rule


def to_json(traces, issues):
    by_sev, by_rule = summarize(issues)
    return {
        "version": 1,
        "traces": len(traces),
        "trace_ids": [t.trace_id for t in traces],
        "issues": issues,
        "summary": {"by_severity": by_sev, "by_rule": by_rule},
    }


def to_markdown(traces, issues, path_note=""):
    by_sev, by_rule = summarize(issues)
    L = ["# Agent 离线检查报告", ""]
    L.append("> 检查器：EvoGuard inspect（确定性规则，不调用模型）  ")
    L.append("> 输入：%d 条轨迹%s" % (len(traces), ("（" + path_note + "）") if path_note else ""))
    L.append("")
    L.append("## 一、总览")
    L.append("")
    L.append("| 严重度 | 条数 |")
    L.append("|---|---|")
    for sev in ("error", "warn", "info"):
        L.append("| %s | %d |" % (sev, by_sev.get(sev, 0)))
    L.append("")
    if not issues:
        L.append("**没有命中任何规则。** 注意：这只说明「本规则集没发现问题」，")
        L.append("不等于 Agent 没问题 —— 规则集覆盖不到的风险它报不出来。")
        L.append("")
        return "\n".join(L)

    L.append("| 规则 | 命中 | 严重度 |")
    L.append("|---|---|---|")
    for rid, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        meta = trace_rules.RULES.get(rid, {})
        L.append("| `%s` %s | %d | %s |" % (rid, meta.get("title", ""), n,
                                            meta.get("severity", "info")))
    L.append("")
    L.append("## 二、问题明细")
    L.append("")
    issues = sorted(issues, key=lambda i: (SEV_ORDER.get(i["severity"], 9), i["rule_id"]))
    for i in issues:
        L.append("### [%s] %s" % (i["severity"].upper(), i["title"]))
        L.append("")
        L.append("- 轨迹 `%s` ｜ agent `%s` ｜ task `%s`"
                 % (i["trace_id"], i["agent"], i["task"] or "-"))
        if i["step_seq"] is not None:
            L.append("- 证据位置：第 **%s** 步 `%s`" % (i["step_seq"], i["step_name"]))
        L.append("- 事实：%s" % i["detail"])
        if i["fix"]:
            L.append("- 建议：%s" % i["fix"])
        if i.get("false_positive"):
            L.append("- **可能是误报**：%s" % i["false_positive"])
        L.append("")
    L.append("## 三、这份报告不能说明什么")
    L.append("")
    L.append("1. 只覆盖 %d 条规则，规则集之外的问题它看不见。" % len(trace_rules.ALL_RULES))
    L.append("2. 规则只看轨迹，**看不到 Agent 的 prompt 与配置** —— 那属于静态检查。")
    L.append("3. `warn` 级别需要人工核实，尤其 `hallucinated_number`（数字可能是算出来的）。")
    L.append("")
    return "\n".join(L)


def write(out_dir, traces, issues, path_note=""):
    os.makedirs(out_dir, exist_ok=True)
    j = os.path.join(out_dir, "inspect.json")
    m = os.path.join(out_dir, "inspect.md")
    with open(j, "w", encoding="utf-8") as f:
        json.dump(to_json(traces, issues), f, ensure_ascii=False, indent=2)
    with open(m, "w", encoding="utf-8") as f:
        f.write(to_markdown(traces, issues, path_note))
    return j, m
