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


def inspect_files_safe(paths, fmt=None):
    """逐个文件加载，失败的记下来继续 —— 一份坏输入不该让其余轨迹白跑。

    但也不能静默：空文件 / 认不出格式都算解析失败，报告和退出码都要反映。
    """
    traces, failures = [], []
    for p in paths:
        try:
            got = loaders.load_file(p, fmt)
        except Exception as e:
            failures.append({"path": p, "error": "%s: %s" % (type(e).__name__, str(e))})
            continue
        if not got:
            failures.append({"path": p,
                             "error": "解析出 0 条轨迹（空文件或格式不匹配）"})
            continue
        traces.extend(got)
    return traces, failures


def summarize(issues):
    by_sev, by_rule, by_group = {}, {}, {}
    for i in issues:
        by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        by_rule[i["rule_id"]] = by_rule.get(i["rule_id"], 0) + 1
        g = i.get("group") or "other"
        by_group[g] = by_group.get(g, 0) + 1
    return by_sev, by_rule, by_group


def to_json(traces, issues, coverage=None, parse_failures=None):
    by_sev, by_rule, by_group = summarize(issues)
    data = {
        "version": 2,
        "traces": len(traces),
        "trace_ids": [t.trace_id for t in traces],
        "issues": issues,
        "summary": {"by_severity": by_sev, "by_rule": by_rule,
                    "by_group": by_group},
        "parse_failures": list(parse_failures or []),
    }
    if coverage is not None:
        # verdict 单独给一个字段：外部工具靠它判断「没发现问题」和「没查成」。
        data["coverage"] = coverage
        data["verdict"] = coverage.get("verdict")
    return data


GROUP_DESC = {
    "answer": "输出层：答案本身不对",
    "tool": "工具层：调用或结果的使用有问题",
    "flow": "流程层：步骤顺序 / 循环 / 过早收尾",
    "resource": "资源层：token 与耗时",
    "structure": "结构层：多 Agent / 并行 / 错误传播",
}


def to_markdown(traces, issues, path_note="", coverage=None, parse_failures=None):
    by_sev, by_rule, by_group = summarize(issues)
    L = ["# Agent 离线检查报告", ""]
    L.append("> 检查器：EvoGuard inspect（确定性规则，不调用模型）  ")
    L.append("> 输入：%d 条轨迹%s" % (len(traces), ("（" + path_note + "）") if path_note else ""))
    if parse_failures:
        L.append("> **有 %d 个输入没能解析**：%s —— 结论只覆盖解析成功的部分。"
                 % (len(parse_failures), "；".join(
                     "%s（%s）" % (f["path"], f["error"][:80]) for f in parse_failures)))
    L.append("")
    L.append("## 一、总览")
    L.append("")
    L.append("| 严重度 | 条数 |")
    L.append("|---|---|")
    for sev in ("error", "warn", "info"):
        L.append("| %s | %d |" % (sev, by_sev.get(sev, 0)))
    L.append("")
    if by_group:
        L.append("| 分组 | 条数 | 说明 |")
        L.append("|---|---|---|")
        for g in ("answer", "tool", "flow", "resource", "structure"):
            if g in by_group:
                L.append("| %s | %d | %s |" % (g, by_group[g], GROUP_DESC.get(g, "")))
        L.append("")
        L.append("> 排查顺序建议：先看**输出层**（结论错了），再往**工具层/流程层**倒推原因。")
        L.append("")
    if not issues:
        L.append("**没有命中任何规则。** 注意：这只说明「本规则集没发现问题」，")
        L.append("不等于 Agent 没问题 —— 规则集覆盖不到的风险它报不出来。")
        L.append("")
        return "\n".join(L)

    L.append("| 规则 | 分组 | 命中 | 严重度 |")
    L.append("|---|---|---|---|")
    for rid, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        meta = trace_rules.RULES.get(rid, {})
        L.append("| `%s` %s | %s | %d | %s |" % (
            rid, meta.get("title", ""), meta.get("group", "-"), n,
            meta.get("severity", "info")))
    L.append("")
    L.append("## 二、问题明细")
    L.append("")
    issues = sorted(issues, key=lambda i: (SEV_ORDER.get(i["severity"], 9), i["rule_id"]))
    for i in issues:
        L.append("### [%s][%s] %s" % (i["severity"].upper(),
                                      i.get("group", "-"), i["title"]))
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
    from .rules import registry
    L.append("1. 只覆盖 %d 条规则（5 组），规则集之外的问题它看不见。" % len(registry.RULES))
    L.append("2. 规则只看轨迹，**看不到 Agent 的 prompt 与配置** —— 那属于静态检查。")
    L.append("3. `warn` 级别需要人工核实，尤其 `hallucinated_number`（数字可能是算出来的）。")
    L.append("")
    if coverage is not None:
        from . import coverage as cov
        L.append("## 四、规则适用性")
        L.append("")
        L.extend(cov.render(coverage)[2:])     # 复用同一段生成逻辑，标题只留一个
        L.append("")
    return "\n".join(L)


# ---------------- 静态检查报告 ----------------
STATIC_GROUP_DESC = {
    "prompt": "提示词层：没写清楚的边界，模型只能自己猜",
    "tool": "工具定义层：模型选错工具、误用写操作的根源",
    "config": "配置层：超时 / 重试 / 上限 / 输出约束",
}
STATIC_ORDER = ["prompt", "tool", "config"]


def write_static(out_dir, defs, issues, parse_failures=None):
    """静态检查只读定义，所以报告必须把「置信度」写出来 —— 仓库扫描是猜的。"""
    os.makedirs(out_dir, exist_ok=True)
    by_sev, by_rule, by_group = summarize(issues)
    data = {
        "version": 2,
        "agents": [{"name": d.name, "source": d.source, "kind": d.source_kind,
                    "confidence": d.confidence, "tools": len(d.tools),
                    "prompt_chars": len(d.system_prompt), "notes": d.notes}
                   for d in defs],
        "issues": issues,
        "summary": {"by_severity": by_sev, "by_rule": by_rule, "by_group": by_group},
        "parse_failures": list(parse_failures or []),
    }
    j = os.path.join(out_dir, "static.json")
    with open(j, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    L = ["# Agent 静态检查报告（不执行 Agent）", ""]
    L.append("> 检查器：EvoGuard inspect --agent（确定性规则，不调用模型、不运行被检代码）")
    L.append("")
    L.append("## 一、被检对象")
    L.append("")
    L.append("| Agent | 来源 | 类型 | 工具数 | prompt 字数 | 置信度 |")
    L.append("|---|---|---|---|---|---|")
    for d in defs:
        L.append("| %s | `%s` | %s | %d | %d | **%s** |" % (
            d.name, (d.source or "-")[-60:], d.source_kind, len(d.tools),
            len(d.system_prompt), d.confidence))
    L.append("")
    for d in defs:
        if d.notes:
            L.append("- `%s`：" % d.name + "；".join(d.notes))
    if any(d.notes for d in defs):
        L.append("")
    L.append("> **confidence=low 表示定义是从源码里猜出来的** —— prompt 边界和工具清单都可能不全，")
    L.append("> 这类结论只能当线索，不能当事实。")
    L.append("")

    L.append("## 二、总览")
    L.append("")
    L.append("| 严重度 | 条数 |")
    L.append("|---|---|")
    for sev in ("error", "warn", "info"):
        L.append("| %s | %d |" % (sev, by_sev.get(sev, 0)))
    L.append("")
    if by_group:
        L.append("| 分组 | 条数 | 说明 |")
        L.append("|---|---|---|")
        for g in STATIC_ORDER:
            if g in by_group:
                L.append("| %s | %d | %s |" % (g, by_group[g], STATIC_GROUP_DESC.get(g, "")))
        L.append("")

    if not issues:
        L.append("**没有命中任何静态规则。** 这只说明「本规则集没发现问题」，")
        L.append("不等于 Agent 没问题 —— 静态检查看不到运行时行为。")
        L.append("")
    else:
        L.append("## 三、问题明细（按分组）")
        L.append("")
        for g in STATIC_ORDER:
            gi = [i for i in issues if i.get("group") == g]
            if not gi:
                continue
            L.append("### %s · %s" % (g, STATIC_GROUP_DESC.get(g, "")))
            L.append("")
            for i in gi:
                L.append("**[%s] %s**" % (i["severity"].upper(), i["title"]))
                L.append("")
                L.append("- 对象：`%s`（置信度 %s）" % (i["agent"], i.get("confidence", "-")))
                if i.get("evidence"):
                    L.append("- 证据：`%s`" % str(i["evidence"])[:160])
                L.append("- 事实：%s" % i["detail"])
                if i["fix"]:
                    L.append("- 怎么改：%s" % i["fix"])
                if i.get("false_positive"):
                    L.append("- **可能是误报**：%s" % i["false_positive"])
                L.append("")

    L.append("## 四、这份报告不能说明什么")
    L.append("")
    L.append("1. 只看定义，**不知道 Agent 实际会怎么执行** —— 那需要轨迹（`--trace`）。")
    L.append("2. 规则集之外的风险它看不见（本报告只跑了配置里启用的规则）。")
    L.append("3. 从源码提取的定义置信度低，结论需要人工复核。")
    L.append("")
    m = os.path.join(out_dir, "static.md")
    with open(m, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return j, m


def write(out_dir, traces, issues, path_note="", coverage=None, parse_failures=None):
    os.makedirs(out_dir, exist_ok=True)
    j = os.path.join(out_dir, "inspect.json")
    m = os.path.join(out_dir, "inspect.md")
    with open(j, "w", encoding="utf-8") as f:
        json.dump(to_json(traces, issues, coverage, parse_failures), f,
                  ensure_ascii=False, indent=2)
    with open(m, "w", encoding="utf-8") as f:
        f.write(to_markdown(traces, issues, path_note, coverage, parse_failures))
    return j, m
