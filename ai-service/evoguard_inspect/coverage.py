# -*- coding: utf-8 -*-
"""规则适用性统计：一条规则没命中，到底是「查过没问题」还是「根本没查」？

这件事比多写几条规则重要。零命中太容易被读成「检查通过」，
而实际上可能是一半规则因为缺 token 用量、缺时间戳、缺工具返回而压根没跑。
报告必须把「可执行规则数 / 选中规则数」和跳过原因写出来，
否则用户拿到的是一张写着 0 的体检单，却不知道有几项没做。

设计取舍：输入条件集中声明在这张表里，而不是让 25 条规则各自返回「我跳过了」。
理由有两个：
  1. 规则函数保持纯函数，签名不变，已公布的 25 条规则不用重写；
  2. 条件集中一处才能横向核对 —— 加规则时漏写条件，测试会直接报出来。
代价是表与规则实现可能漂移，所以 tests 里有一条断言：表里的键必须都是已注册规则。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .rules import registry  # noqa: E402

# 轨迹侧条件
ANSWER = "answer"          # 有答案文本
TOOLS = "tools"            # 至少一次工具调用
TOOLS_MULTI = "tools_multi"  # 至少两种不同工具（跨工具比对类规则）
TOOL_TEXT = "tool_text"     # 至少一条工具有实质文本返回
CHATS = "chats"            # 至少一次模型调用
TOKENS = "tokens"          # 至少有一步记录了 token 用量
TS = "ts"                  # 至少有一步记录了时间戳
# 配置侧条件（用户没配就无从检查）
CFG_EXPECT_KEYS = "config:expect_keys"
CFG_ALLOWED_TOOLS = "config:allowed_tools"
CFG_PII = "config:pii"

REASON = {
    ANSWER: "轨迹没有答案文本",
    TOOLS: "轨迹没有工具调用",
    TOOLS_MULTI: "只有单一工具，跨工具比对无从进行",
    TOOL_TEXT: "工具没有实质返回内容",
    CHATS: "轨迹没有模型调用步骤",
    TOKENS: "轨迹没记录 token 用量",
    TS: "轨迹没记录时间戳",
    CFG_EXPECT_KEYS: "配置未声明 expect_keys",
    CFG_ALLOWED_TOOLS: "配置未声明 allowed_tools",
    CFG_PII: "配置已关闭 check_pii",
}

# 每条规则需要的输入。没列出的按「无条件可执行」处理 —— 宁可高估覆盖率，不低估。
TRACE_REQUIRES = {
    "empty_answer": (),
    "answer_schema_missing": (CFG_EXPECT_KEYS,),
    "unsupported_entity": (ANSWER, TOOLS),
    "hallucinated_number": (ANSWER, TOOL_TEXT),
    "pii_leak": (ANSWER, CFG_PII),
    "error_swallowed": (TOOLS, ANSWER),
    "tool_never_called": (CHATS,),
    "tool_arg_incomplete": (TOOLS,),
    "tool_result_unused": (TOOLS, ANSWER),
    "dependency_mismatch": (TOOLS,),
    "tool_name_unknown": (TOOLS, CFG_ALLOWED_TOOLS),
    "write_before_read": (TOOLS,),
    "loop_detected": (TOOLS,),
    "cycle_detected": (TOOLS_MULTI,),
    "premature_finish": (ANSWER, TOOLS),
    "token_blowup": (TOKENS,),
    "slow_step": (TS,),
    "model_error_ignored": (CHATS, ANSWER),
    "parallel_write_conflict": (TOOLS_MULTI,),
    "self_report_contradiction": (ANSWER, TOOLS),
    "retry_storm": (TOOLS,),
    "silent_stall": (TOOLS,),
    "write_timeout_ambiguous": (TOOLS,),
    "retry_on_permanent_error": (TOOLS,),
    "hallucination_propagation": (TOOLS_MULTI,),
}


def _text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        import json
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def trace_facts(trace):
    """这条轨迹能提供哪些输入。只读，不改轨迹。"""
    tools = trace.tools()
    names = {(s.tool or s.name) for s in tools if (s.tool or s.name)}
    return {
        ANSWER: bool(_text(trace.answer).strip()),
        TOOLS: bool(tools),
        TOOLS_MULTI: len(names) >= 2,
        TOOL_TEXT: any(len(s.text().strip()) >= 10 for s in tools),
        CHATS: bool(trace.chats()),
        TOKENS: any(getattr(s, "tokens", None) for s in trace.steps),
        TS: any(getattr(s, "ts", None) is not None for s in trace.steps),
    }


def config_facts(cfg):
    return {
        CFG_EXPECT_KEYS: bool(getattr(cfg, "expect_keys", None)),
        CFG_ALLOWED_TOOLS: bool(getattr(cfg, "allowed_tools", None)),
        CFG_PII: bool(getattr(cfg, "check_pii", True)),
    }


def missing(facts, requires):
    return [r for r in requires if not facts.get(r, False)]


def analyze(traces, issues, cfg, selected_ids=None):
    """算出覆盖率。selected_ids 为 None 时按全部已注册规则算。

    返回结构直接给报告和 CLI 用：
      rules: 每条规则一行（是否可执行、可执行轨迹数、命中数、跳过原因）
      runnable / total: 可执行规则数与选中数
      traces_with: 数据集层面有多少条轨迹提供了某类输入
      verdict: PASS / FINDINGS / COVERAGE_LIMITED / EMPTY_INPUT
    """
    ids = list(selected_ids) if selected_ids is not None else list(registry.RULES)
    tfacts = [trace_facts(t) for t in traces]
    cfacts = config_facts(cfg)
    hits = {}
    for i in issues:
        hits[i["rule_id"]] = hits.get(i["rule_id"], 0) + 1

    rows, runnable, table_warnings = [], 0, []
    for rid in ids:
        meta = registry.RULES.get(rid, {})
        reqs = TRACE_REQUIRES.get(rid, ())
        per_trace = 0
        reasons = set()
        for facts in tfacts:
            merged = dict(facts)
            merged.update(cfacts)          # 轨迹事实 + 配置事实合成一份再判
            miss = missing(merged, reqs)
            if miss:
                reasons.update(miss)
            else:
                per_trace += 1
        ok = per_trace > 0
        if not ok and hits.get(rid, 0) > 0:
            # 规则命中了却没通过「输入条件」判断 —— 说明这张条件表写错了。
            # 以实际行为为准（它显然跑了），同时把偏差记下来，别静默。
            ok = True
            per_trace = 1
            reasons.clear()
            table_warnings.append(
                "%s 命中了 %d 次，但条件表认为它不可执行（%s）—— 条件表需要修正"
                % (rid, hits[rid], "、".join(sorted(REASON.get(r, r) for r in reqs))))
        if ok:
            runnable += 1
        rows.append({
            "rule_id": rid, "group": meta.get("group"), "severity": meta.get("severity"),
            "requires": list(reqs), "applicable_traces": per_trace,
            "hits": hits.get(rid, 0), "runnable": ok,
            "skipped_because": [] if ok else sorted(REASON.get(r, r) for r in reasons),
        })

    total = len(ids)
    if not traces:
        verdict = "EMPTY_INPUT"
    elif not issues and runnable < total:
        verdict = "COVERAGE_LIMITED"   # 零命中但没查全 —— 不能写成检查通过
    elif issues:
        verdict = "FINDINGS"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "total_rules": total,
        "runnable_rules": runnable,
        "skipped_rules": total - runnable,
        "runnable_ratio": round(runnable / total, 3) if total else None,
        "traces": len(traces),
        "traces_with": {k: sum(1 for f in tfacts if f.get(k)) for k in
                        (ANSWER, TOOLS, TOOLS_MULTI, TOOL_TEXT, CHATS, TOKENS, TS)},
        "config": {k: v for k, v in cfacts.items()},
        "table_warnings": table_warnings,
        "rules": rows,
    }


VERDICT_TEXT = {
    "PASS": "通过：选中的规则都具备输入条件，且没有命中",
    "FINDINGS": "有发现：见下方明细",
    "COVERAGE_LIMITED": "覆盖有限：零命中，但有规则因输入不足没有执行 —— 不能读成「检查通过」",
    "EMPTY_INPUT": "没有可检查的轨迹",
}


# ---------------- 静态检查侧的覆盖率 ----------------
# 静态检查的「输入不足」与轨迹侧不同：这里缺的是定义里的字段，
# 以及 confidence —— 仓库启发式提取只能算 low，缺失型规则在 low 上不成立。
ST_PROMPT = "prompt"          # 定义里有 system prompt
ST_TOOLS = "tools"            # 定义里有工具
ST_HIGH = "high"              # confidence=high（显式定义文件，不是源码猜的）
ST_CFG_RETRY = "config:retry"  # 配置里声明了重试
ST_WRITE_TOOLS = "write_tools"  # 至少一个写操作工具

STATIC_REASON = {
    ST_PROMPT: "定义里没有 system prompt",
    ST_TOOLS: "定义里没有工具",
    ST_HIGH: "定义来自源码启发式提取（confidence=low），缺失型规则在它上面不成立",
    ST_CFG_RETRY: "配置里没声明重试次数",
    ST_WRITE_TOOLS: "定义里没有写操作工具",
}

STATIC_REQUIRES = {
    "prompt_missing": (ST_HIGH,),
    "prompt_no_refusal_boundary": (ST_HIGH, ST_PROMPT),
    "prompt_no_tool_policy": (ST_HIGH, ST_TOOLS, ST_PROMPT),
    "prompt_secret_leak": (ST_PROMPT,),
    "prompt_pii": (ST_PROMPT,),
    "prompt_no_untrusted_boundary": (ST_PROMPT, ST_TOOLS),
    "tool_write_no_approval": (ST_HIGH, ST_WRITE_TOOLS),
    "tool_desc_missing": (ST_HIGH, ST_TOOLS),
    "tool_desc_no_side_effect": (ST_HIGH, ST_WRITE_TOOLS),
    "tool_arg_untyped": (ST_HIGH, ST_TOOLS),
    "tool_too_many": (ST_HIGH, ST_TOOLS),
    "tool_name_collision": (ST_HIGH, ST_TOOLS),
    "unsafe_shell_tool": (ST_TOOLS,),
    "no_timeout": (ST_HIGH,),
    "no_retry_policy": (ST_HIGH,),
    "no_max_iterations": (ST_HIGH,),
    "no_output_schema": (ST_HIGH,),
    "retry_without_backoff": (ST_CFG_RETRY,),
    "no_run_budget": (ST_HIGH,),
    "write_tool_no_idempotency": (ST_WRITE_TOOLS,),
}


def _def_facts(defn, cfg):
    tools = list(getattr(defn, "tools", []) or [])
    return {
        ST_PROMPT: bool((getattr(defn, "system_prompt", "") or "").strip()),
        ST_TOOLS: bool(tools),
        ST_HIGH: getattr(defn, "confidence", "low") == "high",
        ST_CFG_RETRY: bool((getattr(defn, "config", {}) or {}).get("retry")
                           or (getattr(defn, "config", {}) or {}).get("max_retries")),
        ST_WRITE_TOOLS: any(cfg.is_write(t.name) for t in tools),
    }


def analyze_static(defs, issues, cfg, selected_ids=None):
    """静态检查的覆盖率。判定口径与轨迹侧一致，缺的输入条件不同。"""
    from .static import rules as SR
    ids = list(selected_ids) if selected_ids is not None else list(SR.RULES)
    dfacts = [_def_facts(d, cfg) for d in defs]
    hits = {}
    for i in issues:
        hits[i["rule_id"]] = hits.get(i["rule_id"], 0) + 1

    rows, runnable, table_warnings = [], 0, []
    for rid in ids:
        meta = SR.RULES.get(rid, {})
        reqs = STATIC_REQUIRES.get(rid, ())
        ok_defs, reasons = 0, set()
        for facts in dfacts:
            miss = missing(facts, reqs)
            if miss:
                reasons.update(miss)
            else:
                ok_defs += 1
        ok = ok_defs > 0
        if not ok and hits.get(rid, 0) > 0:
            ok, ok_defs = True, 1
            reasons.clear()
            table_warnings.append(
                "%s 命中了 %d 次，但条件表认为它不可执行 —— 条件表需要修正"
                % (rid, hits[rid]))
        if ok:
            runnable += 1
        rows.append({
            "rule_id": rid, "group": meta.get("group"), "severity": meta.get("severity"),
            "requires": list(reqs), "applicable_defs": ok_defs,
            "hits": hits.get(rid, 0), "runnable": ok,
            "skipped_because": [] if ok else sorted(STATIC_REASON.get(r, r) for r in reasons),
        })

    total = len(ids)
    if not defs:
        verdict = "EMPTY_INPUT"
    elif not issues and runnable < total:
        verdict = "COVERAGE_LIMITED"
    elif issues:
        verdict = "FINDINGS"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict, "total_rules": total, "runnable_rules": runnable,
        "skipped_rules": total - runnable,
        "runnable_ratio": round(runnable / total, 3) if total else None,
        "defs": len(defs),
        "defs_with": {k: sum(1 for f in dfacts if f.get(k)) for k in
                      (ST_PROMPT, ST_TOOLS, ST_HIGH, ST_WRITE_TOOLS)},
        "table_warnings": table_warnings,
        "rules": rows,
    }


def render_static(coverage):
    L = ["## 规则适用性", "",
         VERDICT_TEXT.get(coverage["verdict"], coverage["verdict"]), "",
         "- 选中规则 %d 条，可执行 %d 条，跳过 %d 条（可执行率 %s）"
         % (coverage["total_rules"], coverage["runnable_rules"],
            coverage["skipped_rules"], coverage["runnable_ratio"]),
         "- 定义 %d 个；其中有 prompt %d、有工具 %d、高置信度 %d、有写操作工具 %d"
         % (coverage["defs"], coverage["defs_with"][ST_PROMPT],
            coverage["defs_with"][ST_TOOLS], coverage["defs_with"][ST_HIGH],
            coverage["defs_with"][ST_WRITE_TOOLS]),
         "",
         "> 源码启发式提取的定义一律 confidence=low，缺失型规则（没超时 / 没重试 / 没轮次上限）",
         "> 在它上面不成立 —— 源码里没读到，不等于运行时没配。"]
    skipped = [r for r in coverage["rules"] if not r["runnable"]]
    if skipped:
        L += ["", "| 跳过的规则 | 分组 | 原因 |", "|---|---|---|"]
        for r in skipped:
            L.append("| %s | %s | %s |" % (r["rule_id"], r["group"],
                                           "；".join(r["skipped_because"]) or "输入缺失"))
    if coverage.get("table_warnings"):
        L += ["", "条件表与实际行为不一致（需修正）："]
        L += ["- %s" % w for w in coverage["table_warnings"]]
    return L


def render(coverage):
    """Markdown 片段：覆盖率 + 跳过原因。不写「通过」二字除非真的全跑了。"""
    L = ["## 规则适用性", "",
         VERDICT_TEXT.get(coverage["verdict"], coverage["verdict"]), "",
         "- 选中规则 %d 条，可执行 %d 条，跳过 %d 条（可执行率 %s）"
         % (coverage["total_rules"], coverage["runnable_rules"],
            coverage["skipped_rules"],
            coverage["runnable_ratio"] if coverage["runnable_ratio"] is not None else "N/A"),
         "- 轨迹 %d 条；其中有答案 %d、有工具调用 %d、有 ≥2 种工具 %d、"
         "有工具实质返回 %d、有模型调用 %d、有 token 用量 %d、有时间戳 %d"
         % (coverage["traces"], coverage["traces_with"][ANSWER],
            coverage["traces_with"][TOOLS], coverage["traces_with"][TOOLS_MULTI],
            coverage["traces_with"][TOOL_TEXT], coverage["traces_with"][CHATS],
            coverage["traces_with"][TOKENS], coverage["traces_with"][TS])]
    skipped = [r for r in coverage["rules"] if not r["runnable"]]
    if skipped:
        L += ["", "| 跳过的规则 | 分组 | 原因 |", "|---|---|---|"]
        for r in skipped:
            L.append("| %s | %s | %s |" % (r["rule_id"], r["group"],
                                           "；".join(r["skipped_because"]) or "输入缺失"))
    partial = [r for r in coverage["rules"]
               if r["runnable"] and r["applicable_traces"] < coverage["traces"]]
    if partial:
        L += ["", "部分轨迹缺输入的规则（命中数只覆盖可执行的那部分）："
              + "、".join("%s(%d/%d)" % (r["rule_id"], r["applicable_traces"],
                                        coverage["traces"]) for r in partial)]
    if coverage.get("table_warnings"):
        L += ["", "条件表与实际行为不一致（统计口径问题，需修正）："]
        L += ["- %s" % w for w in coverage["table_warnings"]]
    return L
