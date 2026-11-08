# -*- coding: utf-8 -*-
"""轨迹规则：只看「已经跑完的一次执行」，不执行 Agent，也不猜。

每条规则都必须满足三条：
  1. 能给**证据位置**（哪一步、哪个字段）；
  2. 误报场景写清楚（写在 FALSE_POSITIVE_NOTE 里，报告会显示）；
  3. 不改数据，只报告。

为什么强调误报：这类工具最大的死法是"报一堆你看不懂的警告"，用户关掉。
所以宁可少报，也要每一条都能人工核实。
"""
import json
import re

WRITE_HINTS = ("settle", "write", "delete", "update", "create", "refund",
               "submit", "send", "pay", "transfer")
# 数字边界：跳过标识符里的数字（"X1" 的 1 不是数值，报了就是噪音）。
# 中文字符不算字母，所以「1 个工作日」里的 1 仍然会被抓 —— 那才是要核的。
NUM_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")

RULES = {
    "empty_answer": {
        "title": "宣称完成但没有产出答案",
        "severity": "error",
        "why": "轨迹成功结束，答案却是空的 —— 下游拿到的是空对象，不是结果。",
        "false_positive": "流式输出被截断、或答案写在别的字段里（非 answer/completion）。",
    },
    "error_swallowed": {
        "title": "工具报错后仍然给出了最终答案",
        "severity": "error",
        "why": "工具失败没有触发重试或降级，Agent 直接把残缺上下文当成了结论。",
        "false_positive": "该工具本来就允许失败（可选增强），且答案不依赖它。",
    },
    "hallucinated_number": {
        "title": "答案里出现工具没返回过的数字",
        "severity": "warn",
        "why": "数值没有出处，属于典型的工具输出幻觉。",
        "false_positive": "**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— "
                          "所以这条只报警不判错，必须人工核实一次。",
    },
    "write_before_read": {
        "title": "写操作发生在读取之前",
        "severity": "warn",
        "why": "没有先读取当前状态就写，容易覆盖或重复扣款。",
        "false_positive": "写操作本身是幂等的、或入参已包含全部前置信息。",
    },
    "repeated_tool_call": {
        "title": "同一工具同参数被反复调用",
        "severity": "warn",
        "why": "典型的成本滥用：同样的调用做三遍以上，token 和时间都在烧。",
        "false_positive": "该工具用于轮询等待（如查任务状态），重复是设计如此。",
    },
}


def _issue(rule_id, trace, step=None, detail="", fix=""):
    meta = RULES[rule_id]
    return {
        "rule_id": rule_id,
        "title": meta["title"],
        "severity": meta["severity"],
        "trace_id": trace.trace_id,
        "agent": trace.agent,
        "task": trace.task,
        "step_seq": step.seq if step is not None else None,
        "step_name": (step.tool or step.name) if step is not None else None,
        "detail": detail,
        "fix": fix,
        "false_positive": meta["false_positive"],
    }


def _answer_text(trace):
    a = trace.answer
    if a is None:
        return ""
    if isinstance(a, str):
        return a
    return json.dumps(a, ensure_ascii=False)


def rule_empty_answer(trace):
    if trace.answer is None:
        return [_issue("empty_answer", trace, detail="trace 里没有答案",
                       fix="检查 Agent 的收尾分支；空答案不要按成功上报")]
    txt = _answer_text(trace).strip()
    if txt in ("", "{}", "[]", "null"):
        return [_issue("empty_answer", trace, detail="答案为空：%r" % txt[:40],
                       fix="检查 Agent 的收尾分支；空答案不要按成功上报")]
    return []


def rule_error_swallowed(trace):
    tools = trace.tools()
    errs = [s for s in tools if s.status == "error"]
    if not errs or trace.answer is None:
        return []
    # 报错之后有没有「再试一次同一个工具」
    after = []
    for e in errs:
        later = [s for s in tools if s.seq > e.seq and (s.tool or s.name) == (e.tool or e.name)]
        after.append(bool(later))
    if all(after):
        return []
    e = errs[-1]
    return [_issue("error_swallowed", trace, step=e,
                   detail="工具 %s 报错(%s)，之后没有重试就给出了答案"
                          % (e.tool or e.name, e.error),
                   fix="失败分支要显式降级或重试；把工具错误写进答案，别静默吞掉")]


def rule_hallucinated_number(trace):
    txt = _answer_text(trace)
    if not txt:
        return []
    # 只拿**工具输出**当证据池。模型自己的话不能当自己的证据 ——
    # 把 chat 也放进来，答案里的数字必然"出现在池子里"，规则永远不命中（踩过）。
    pool = " ".join(s.text() for s in trace.tools())
    # 有计算类工具时，答案数字很可能是算出来的 —— 降级为提示，避免大面积误报
    has_calc = any(any(k in (s.tool or "").lower() for k in ("calc", "apply", "compute"))
                   for s in trace.tools())
    missing = [n for n in NUM_RE.findall(txt) if n not in pool]
    if not missing:
        return []
    return [_issue("hallucinated_number", trace,
                   step=trace.chats()[-1] if trace.chats() else None,
                   detail="这些数字在任何工具输出里都找不到：%s" % ", ".join(missing[:6]),
                   fix="要求 Agent 只引用工具返回的数值；"
                       + ("本条命中计算类工具，请先人工确认是否算出来的" if has_calc else ""))]


def rule_write_before_read(trace):
    tools = trace.tools()
    first_write = None
    for s in tools:
        if any(h in (s.tool or s.name).lower() for h in WRITE_HINTS):
            first_write = s
            break
    if first_write is None:
        return []
    reads = [s for s in tools if s.seq < first_write.seq
             and not any(h in (s.tool or s.name).lower() for h in WRITE_HINTS)]
    if reads:
        return []
    return [_issue("write_before_read", trace, step=first_write,
                   detail="第一次调用就是写操作 %s，之前没有任何读取" % (first_write.tool or first_write.name),
                   fix="写之前先读一次当前状态，或确认入参已含前置信息")]


def rule_repeated_tool_call(trace):
    out, seen = [], {}
    for s in trace.tools():
        key = ((s.tool or s.name), json.dumps(s.args, ensure_ascii=False, sort_keys=True))
        seen.setdefault(key, []).append(s)
    for key, steps in seen.items():
        if len(steps) >= 3:
            out.append(_issue("repeated_tool_call", trace, step=steps[-1],
                              detail="%s 用同一组参数被调用 %d 次" % (key[0], len(steps)),
                              fix="加去重/缓存；轮询类工具要显式标 polling 语义"))
    return out


ALL_RULES = [rule_empty_answer, rule_error_swallowed, rule_hallucinated_number,
             rule_write_before_read, rule_repeated_tool_call]


def check(trace):
    issues = []
    for fn in ALL_RULES:
        try:
            issues.extend(fn(trace) or [])
        except Exception as e:      # 单条规则崩了不能拖垮整份报告
            issues.append({
                "rule_id": "rule_crashed", "title": "规则执行异常",
                "severity": "info", "trace_id": trace.trace_id,
                "agent": trace.agent, "task": trace.task,
                "step_seq": None, "step_name": None,
                "detail": "%s: %s" % (fn.__name__, e), "fix": "",
                "false_positive": "这是检查器自己的 bug，不是被检 Agent 的问题",
            })
    return issues


def check_all(traces):
    out = []
    for t in traces:
        out.extend(check(t))
    return out
