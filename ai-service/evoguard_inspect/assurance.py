# -*- coding: utf-8 -*-
"""保障视图：把「可靠」「防护」「可运行」三件事合成一份报告。

为什么需要这一层：轨迹规则（31 条）和静态规则（20 条）各查一摊，
跑完得到一长串发现，但没人回答那个真正的问题 ——
**这个 Agent 现在能不能放心用，哪一维没测、哪一维测了但有问题。**

刻意不给 0–100 分。把「3 条 warn + 1 个测不了的维度」压成一个分数，
分数看着专业，但没人知道 82 分该不该上线，而且一旦有了分数，
所有人都会开始优化分数而不是优化系统。这里给的是三个维度的**判定 + 依据 + 缺口**。

三个维度：
  可靠 reliability   – 结论对不对、流程有没有乱（answer/tool/flow/resource/structure）
  防护 security      – 有没有被注入/越权/外带/凭据泄漏/危险命令/XSS（security 组）
  可运行 operability – 模型与工具是否健康、有没有预算超限、有没有「静默降级」
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from . import coverage as cov  # noqa: E402

RELIABILITY_GROUPS = ("answer", "tool", "flow", "resource", "structure")
SECURITY_GROUPS = ("security",)

PASS, RISK, FAIL, LIMITED, UNKNOWN = "PASS", "RISK", "FAIL", "LIMITED", "UNKNOWN"

VERDICT_CN = {
    PASS: "通过",
    RISK: "有风险（需人工确认）",
    FAIL: "不通过",
    LIMITED: "覆盖有限（不能算通过）",
    UNKNOWN: "无法判定",
}


def _steps(traces, kind):
    out = []
    for t in traces:
        for s in t.steps:
            if s.kind == kind:
                out.append(s)
    return out


def health(traces):
    """可运行性指标。只从轨迹算，不看 expected_answer。"""
    chats = _steps(traces, "chat")
    tools = _steps(traces, "tool")
    model_err = [s for s in chats if s.status not in ("ok", None)]
    tool_err = [s for s in tools if s.status not in ("ok", None)]
    # 预算触发：错误类型或原始字段里出现 budget（E_BUDGET_EXCEEDED）
    budget = [st for t in traces for st in t.steps
              if "budget" in " ".join([
                  str(getattr(st, "error", "") or ""),
                  str(getattr(st, "raw", "") or "")]).lower()]
    durations = [getattr(s, "duration_ms", 0) for s in tools
                 if getattr(s, "duration_ms", 0)]
    durations.sort()
    p95 = durations[int(len(durations) * 0.95)] if durations else None

    # 「模型报错但仍有答案」：对外看起来成功，答案却可能不是模型给的。
    # 这里刻意**不**直接叫「静默降级」——在故障注入的数据集里这是预期现象
    # （我们故意让模型步骤报错来测恢复），只有真实运行里它才是降级。
    # 名字如实，判定才不会冤枉人。
    with_answer = 0
    for t in traces:
        has_model_err = any(s.status not in ("ok", None) for s in t.chats())
        answered = bool(_text(t.answer).strip())
        if has_model_err and answered:
            with_answer += 1

    injected = sum(1 for t in traces
                   if str((t.meta or {}).get("fault") or "none") not in ("none", ""))

    return {
        "traces": len(traces),
        "injected_traces": injected,
        "model_steps": len(chats),
        "model_error_steps": len(model_err),
        "model_error_rate": round(len(model_err) / len(chats), 3) if chats else None,
        "tool_steps": len(tools),
        "tool_error_steps": len(tool_err),
        "tool_error_rate": round(len(tool_err) / len(tools), 3) if tools else None,
        "budget_hits": len(budget),
        "tool_p95_ms": p95,
        "model_error_with_answer": with_answer,
    }


def _text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def _verdict(issues, coverage_ok, empty):
    if empty:
        return UNKNOWN
    if any(i["severity"] == "error" for i in issues):
        return FAIL
    if issues:
        return RISK
    if not coverage_ok:
        return LIMITED          # 零命中但有规则没跑 —— 不能算通过
    return PASS


def _operability(hl):
    """可运行性判定。阈值写死在这里，改之前先想清楚为什么。"""
    reasons = []
    verdict = PASS
    if hl["traces"] == 0:
        return UNKNOWN, ["没有轨迹，无法判断运行健康"]
    if hl["model_error_with_answer"]:
        reasons.append("%d 条轨迹在模型报错后仍给出了答案"
                       "（故障注入数据里属预期；真实数据里要查是不是降级）"
                       % hl["model_error_with_answer"])
        verdict = RISK
    rate = hl["model_error_rate"]
    if rate is not None:
        if rate > 0.2:
            reasons.append("模型步骤报错率 %.0f%%（>20%%）" % (rate * 100))
            verdict = FAIL
        elif rate > 0:
            reasons.append("模型步骤报错率 %.0f%%" % (rate * 100))
            if verdict != FAIL:
                verdict = RISK
    if hl["budget_hits"]:
        reasons.append("%d 步触发预算上限" % hl["budget_hits"])
        if verdict != FAIL:
            verdict = RISK
    if not reasons:
        reasons.append("没有模型/工具报错，也没有静默降级")
    return verdict, reasons


def build(traces, issues, cfg=None, coverage=None, static_defs=None,
          static_issues=None):
    """合成三维修正后的判定。issues 是轨迹规则的全部发现。"""
    rel = [i for i in issues if (i.get("group") or "") in RELIABILITY_GROUPS]
    sec = [i for i in issues if (i.get("group") or "") in SECURITY_GROUPS]
    hl = health(traces)
    cov_ok = True
    if coverage is not None:
        cov_ok = coverage.get("runnable_rules", 0) == coverage.get("total_rules", 0)
    empty = not traces

    rel_v = _verdict(rel, cov_ok, empty)
    sec_v = _verdict(sec, cov_ok, empty)
    op_v, op_reasons = _operability(hl)

    worst = [rel_v, sec_v, op_v]
    if FAIL in worst:
        overall = FAIL
    elif UNKNOWN in worst:
        overall = UNKNOWN
    elif LIMITED in worst:
        overall = LIMITED
    elif RISK in worst:
        overall = RISK
    else:
        overall = PASS

    return {
        "verdict": overall,
        "dimensions": {
            "reliability": {
                "verdict": rel_v, "findings": len(rel),
                "by_severity": _by_sev(rel),
                "by_rule": _by_rule(rel),
                "measured": "结论正确性、工具使用、流程、资源、结构（31 条规则中的 25 条）",
                "not_measured": "语义正确性（规则只看证据是否自洽，不判断业务上对不对）；"
                                "提示词层面的约束（属于静态检查）",
            },
            "security": {
                "verdict": sec_v, "findings": len(sec),
                "by_severity": _by_sev(sec),
                "by_rule": _by_rule(sec),
                "measured": "间接注入、越权工具、数据外带、凭据出现在参数、危险命令、输出侧 XSS",
                "not_measured": "检测基于特征，改写/编码/多语言混写可绕过；"
                                "工具白名单拒绝与恢复期写操作审批在 enforce 下真执行，"
                                "但通用沙箱/网络隔离仍未实现；action 仅为检测建议，"
                                "不等于拦截成功",
            },
            "operability": {
                "verdict": op_v, "reasons": op_reasons, "metrics": hl,
                "measured": "模型/工具报错率、预算触发、工具耗时 p95、报错后仍有答案的轨迹数",
                "not_measured": "真实服务可用性（SLA）、并发与限流、成本曲线",
            },
        },
        "coverage": coverage,
        "static": None if static_defs is None else {
            "defs": len(static_defs),
            "issues": len(static_issues or []),
            "by_severity": _by_sev(static_issues or []),
        },
        "traces": len(traces),
        "note": "判定是「基于现有证据」的结论，不是概率保证。"
                "LIMITED 表示有规则因输入不足没执行，不能读成通过。",
    }


def _by_sev(issues):
    out = {}
    for i in issues:
        out[i["severity"]] = out.get(i["severity"], 0) + 1
    return out


def _by_rule(issues):
    out = {}
    for i in issues:
        out[i["rule_id"]] = out.get(i["rule_id"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def render(data):
    """Markdown 片段。三节固定顺序：结论 → 三维 → 缺口。"""
    L = ["# AI 保障报告（可靠 / 防护 / 可运行）", "",
         "> 确定性检查，不调用模型、不执行被检 Agent。输入：%d 条轨迹%s。"
         % (data["traces"], _inject_note(data)),
         "",
         "## 一、总判定", "",
         "| 判定 | 含义 |", "|---|---|",
         "| **%s** | %s |" % (VERDICT_CN[data["verdict"]], _overall_hint(data["verdict"])),
         ""]
    L += ["| 维度 | 判定 | 发现数 | 一句依据 |", "|---|---|---|---|"]
    for key, label in (("reliability", "可靠"), ("security", "防护"),
                       ("operability", "可运行")):
        d = data["dimensions"][key]
        if key == "operability":
            basis = "；".join(d["reasons"][:2])
            n = d["metrics"]["model_error_steps"] + d["metrics"]["tool_error_steps"]
        else:
            basis = "、".join(list(d["by_rule"])[:3]) or "无命中"
            n = d["findings"]
        L.append("| %s | **%s** | %d | %s |"
                 % (label, VERDICT_CN[d["verdict"]], n, basis))
    L.append("")
    for key, label in (("reliability", "二、可靠"), ("security", "三、防护"),
                       ("operability", "四、可运行")):
        d = data["dimensions"][key]
        L += ["## %s" % label, ""]
        L.append("- 判定：**%s**" % VERDICT_CN[d["verdict"]])
        L.append("- 测了什么：%s" % d["measured"])
        L.append("- **没测什么**：%s" % d["not_measured"])
        if key == "operability":
            m = d["metrics"]
            L.append("- 指标：模型步骤 %d（报错 %d，%s）｜工具步骤 %d（报错 %d，%s）"
                     "｜预算触发 %d｜工具 p95 %s｜模型报错但有答案 %d 条"
                     % (m["model_steps"], m["model_error_steps"],
                        _pct(m["model_error_rate"]), m["tool_steps"],
                        m["tool_error_steps"], _pct(m["tool_error_rate"]),
                        m["budget_hits"],
                        "N/A" if m["tool_p95_ms"] is None else "%dms" % m["tool_p95_ms"],
                        m["model_error_with_answer"]))
            if d["reasons"]:
                L.append("- 依据：" + "；".join(d["reasons"]))
        else:
            if d["by_rule"]:
                L.append("- 命中：" + "、".join("%s×%d" % (k, v)
                                               for k, v in d["by_rule"].items()))
            else:
                L.append("- 命中：无")
        L.append("")
    c = data.get("coverage")
    if c:
        L += ["## 五、覆盖与缺口", "",
              "- 规则可执行率：%d/%d" % (c["runnable_rules"], c["total_rules"]),
              "- 轨迹输入：有答案 %d、有工具调用 %d、有 token 用量 %d、有时间戳 %d"
              % (c["traces_with"].get("answer", 0), c["traces_with"].get("tools", 0),
                 c["traces_with"].get("tokens", 0), c["traces_with"].get("ts", 0))]
        skipped = [r["rule_id"] for r in c["rules"] if not r["runnable"]]
        if skipped:
            L.append("- 因输入不足跳过的规则：%s" % "、".join(skipped))
        L.append("")
    if data.get("static"):
        s = data["static"]
        L += ["## 六、静态定义（另行提供）", "",
              "- 定义 %d 个，静态发现 %d 个（%s）"
              % (s["defs"], s["issues"],
                 "、".join("%s %d" % (k, v) for k, v in s["by_severity"].items()) or "无"),
              ""]
    L += ["## 七、这份报告的边界", "",
          "1. %s" % data["note"],
          "2. 可靠 ≠ 业务正确：规则只判断「结论有没有证据支撑」，不判断业务上该不该这么做。",
          "3. 防护 ≠ 安全：特征检测可被绕过；最小权限拒绝与写操作审批在 enforce 下"
          "真执行，通用沙箱/网络隔离仍未实现；且检测建议（action）不等于拦截成功，"
          "要看真实执行点的 effect。",
          "4. 可运行 ≠ 高可用：这里看的是轨迹里的报错与降级，不是线上 SLA。",
          ""]
    return "\n".join(L)


def _inject_note(data):
    """数据画像：多少条是故障注入的。不写这一句，失败率会被当成线上失败率读。"""
    m = data["dimensions"]["operability"]["metrics"]
    n = m.get("injected_traces") or 0
    if not n:
        return ""
    return "，其中 %d 条带注入故障（%.0f%%）—— 这批数据上的失败率**不等于**线上失败率" % (
        n, 100.0 * n / max(1, data["traces"]))


def _overall_hint(v):
    return {
        PASS: "三个维度都没有发现问题",
        RISK: "有需要人工确认的发现，不阻断但别直接上线",
        FAIL: "存在高危发现，建议修复后再上",
        LIMITED: "有维度没测全，不能读成通过",
        UNKNOWN: "输入不足，无法给出结论",
    }[v]


def _pct(v):
    return "N/A" if v is None else "%.0f%%" % (v * 100)
