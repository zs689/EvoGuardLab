# -*- coding: utf-8 -*-
"""轨迹规则库（18 条，分 5 组）。

原则：
  1. 规则只处理「模式」，不写死业务名词 —— 写操作靠配置识别，见 config.py；
  2. 每条必须能给证据位置（第几步、哪个工具）；
  3. 每条必须写「什么时候会误报」，报告会逐条显示；
  4. 单条规则崩溃不能拖垮整份报告。

为什么分五组：用户拿到 20 条警告时需要排查顺序。
输出层 → 工具层 → 流程层 → 资源层 → 结构层，就是从「结论错」倒推到「过程错」。
"""
import json
import re

from ..config import DEFAULT
from .registry import (ALL_RULES, GROUPS, RULES, meta_of, rule,  # noqa: F401
                       select)

# 数字边界：跳过标识符里的数字（"X1" 的 1 不是数值）。
NUM_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")
# 常见 ID 形态：KB-01 / A1001 / ord_7f3a
ID_RE = re.compile(r"\b[A-Z]{1,4}-\d{1,4}\b|\b[A-Z]\d{4}\b|\b[a-z]{2,6}_[0-9a-f]{6,}\b")
PII_PATTERNS = [
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
]


def _issue(rule_id, trace, step=None, detail="", fix=""):
    m = meta_of(rule_id)
    return {
        "rule_id": rule_id,
        "group": m.get("group", ""),
        "title": m.get("title", rule_id),
        "severity": m.get("severity", "info"),
        "trace_id": trace.trace_id,
        "agent": trace.agent,
        "task": trace.task,
        "step_seq": step.seq if step is not None else None,
        "step_name": ((step.tool or step.name) if step is not None else None),
        "detail": detail,
        "fix": fix,
        "false_positive": m.get("false_positive", ""),
    }


def _answer_text(trace):
    a = trace.answer
    if a is None:
        return ""
    return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)


def _tool_text(trace):
    """只有**工具输出**算证据。模型自己的话不能给自己作证（踩过的坑）。"""
    return " ".join(s.text() for s in trace.tools())


# ============================ 输出层 ============================
@rule("empty_answer", "answer", "error",
      "宣称完成但没有产出答案",
      "轨迹结束了，答案却是空的 —— 下游拿到的是空对象，不是结果。",
      "流式输出被截断、或答案写在别的字段里（非 answer/completion）。")
def rule_empty_answer(trace, cfg=DEFAULT):
    txt = _answer_text(trace).strip()
    if trace.answer is None:
        return [_issue("empty_answer", trace, detail="trace 里没有答案",
                       fix="检查 Agent 的收尾分支；空答案不要按成功上报")]
    if txt in ("", "{}", "[]", "null"):
        return [_issue("empty_answer", trace, detail="答案为空：%r" % txt[:40],
                       fix="检查 Agent 的收尾分支；空答案不要按成功上报")]
    return []


@rule("answer_schema_missing", "answer", "warn",
      "答案缺少约定字段",
      "调用方按下游契约取字段，缺了就是运行时 KeyError。",
      "该 Agent 的答案本来就没有固定 schema（自由文本）；未配置 expect_keys 时本条不生效。")
def rule_answer_schema_missing(trace, cfg=DEFAULT):
    if not cfg.expect_keys or not isinstance(trace.answer, dict):
        return []
    missing = [k for k in cfg.expect_keys if k not in trace.answer]
    if not missing:
        return []
    return [_issue("answer_schema_missing", trace,
                   detail="答案缺少字段：%s" % ", ".join(missing),
                   fix="在 Agent 的输出契约里加校验，别让下游兜底")]


@rule("unsupported_entity", "answer", "warn",
      "答案引用了工具没返回过的标识",
      "答案里出现 KB-99 / A9999 这类 ID，但没有任何工具返回过它 —— 典型编造。",
      "该 ID 可能来自 system prompt 或用户输入；本条只比对工具输出，两者都不在池子里。")
def rule_unsupported_entity(trace, cfg=DEFAULT):
    txt = _answer_text(trace)
    if not txt:
        return []
    pool = _tool_text(trace)
    bad = [i for i in ID_RE.findall(txt) if i not in pool]
    if not bad:
        return []
    return [_issue("unsupported_entity", trace,
                   step=trace.chats()[-1] if trace.chats() else None,
                   detail="这些标识不在任何工具输出里：%s" % ", ".join(sorted(set(bad))[:6]),
                   fix="要求 Agent 只能引用工具返回的实体；引用前先校验存在性")]


@rule("hallucinated_number", "answer", "warn",
      "答案里出现工具没返回过的数字",
      "数值没有出处，属于典型的工具输出幻觉。",
      "**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— "
      "所以这条只报警不判错，必须人工核实一次。")
def rule_hallucinated_number(trace, cfg=DEFAULT):
    txt = _answer_text(trace)
    if not txt:
        return []
    # 只拿**工具输出**当证据池：模型自己的话不能当自己的证据（踩过）。
    pool = _tool_text(trace)
    has_calc = any(any(k in (s.tool or "").lower() for k in ("calc", "apply", "compute"))
                   for s in trace.tools())
    missing = [n for n in NUM_RE.findall(txt) if n not in pool]
    if not missing:
        return []
    hint = "本条命中计算类工具，请先人工确认是否算出来的" if has_calc else ""
    return [_issue("hallucinated_number", trace,
                   step=trace.chats()[-1] if trace.chats() else None,
                   detail="这些数字在任何工具输出里都找不到：%s"
                          % ", ".join(sorted(set(missing))[:6]),
                   fix="要求 Agent 只引用工具返回的数值；" + hint)]


@rule("pii_leak", "answer", "error",
      "答案里出现未脱敏的个人信息",
      "手机号 / 身份证 / 邮箱 / 银行卡直接出现在对外输出里。",
      "如果被检 Agent 本身就在处理这些数据（如客服工单），需要按合规要求豁免。")
def rule_pii_leak(trace, cfg=DEFAULT):
    if not cfg.check_pii:
        return []
    txt = _answer_text(trace)
    if not txt:
        return []
    hits = []
    for name, pat in PII_PATTERNS:
        m = pat.search(txt)
        if m:
            hits.append("%s(%s***)" % (name, m.group(0)[:3]))
    if not hits:
        return []
    return [_issue("pii_leak", trace, detail="检出：%s" % ", ".join(hits),
                   fix="输出前做脱敏；日志与答案分开处理")]


# ============================ 工具层 ============================
@rule("error_swallowed", "tool", "error",
      "工具报错后仍然给出了最终答案",
      "工具失败没有触发重试或降级，Agent 直接把残缺上下文当成结论。",
      "该工具本来就允许失败（可选增强），且答案不依赖它。")
def rule_error_swallowed(trace, cfg=DEFAULT):
    tools = trace.tools()
    errs = [s for s in tools if s.status == "error"]
    if not errs or trace.answer is None:
        return []
    retried = []
    for e in errs:
        retried.append(bool([s for s in tools if s.seq > e.seq
                             and (s.tool or s.name) == (e.tool or e.name)]))
    if all(retried):
        return []
    e = errs[-1]
    return [_issue("error_swallowed", trace, step=e,
                   detail="工具 %s 报错(%s)，之后没有重试就给出了答案"
                          % (e.tool or e.name, e.error),
                   fix="失败分支要显式降级或重试；把工具错误写进答案，别静默吞掉")]


@rule("tool_never_called", "tool", "warn",
      "一次工具调用都没有就下了结论",
      "任务看起来需要外部数据，但轨迹里没有任何工具调用 —— 纯靠模型记忆作答。",
      "纯知识问答类 Agent 本来就不需要工具；本条只对「声明了工具」的 Agent 有意义。")
def rule_tool_never_called(trace, cfg=DEFAULT):
    if trace.chats() and not trace.tools() and _answer_text(trace):
        return [_issue("tool_never_called", trace, step=trace.chats()[-1],
                       detail="有 %d 次模型调用、0 次工具调用就给出了答案" % len(trace.chats()),
                       fix="确认该任务是否允许凭记忆作答；需要外部数据就要挂工具")]
    return []


@rule("tool_arg_incomplete", "tool", "warn",
      "工具参数是空的或缺少关键项",
      "调用发出去了，但参数为空 —— 工具要么报错，要么返回错的数据。",
      "有些工具确实允许零参数（如 list_all）；这类工具需要单独豁免。")
def rule_tool_arg_incomplete(trace, cfg=DEFAULT):
    out = []
    for s in trace.tools():
        if s.args in (None, {}, [], ""):
            out.append(_issue("tool_arg_incomplete", trace, step=s,
                              detail="调用 %s 时参数为空" % (s.tool or s.name),
                              fix="在工具 schema 层做必填校验；空参数直接拒绝"))
    return out


@rule("tool_result_unused", "tool", "warn",
      "工具返回了实质内容，但后续完全没引用",
      "白花了一次调用（钱和时间），而且说明 Agent 可能没看证据。",
      "结果可能只用于「确认存在」；短的确认型返回（{\"ok\":true}）本条不判。")
def rule_tool_result_unused(trace, cfg=DEFAULT):
    """只对「产出了值却没被引用」报，不对「返回很短」报。

    第一版拿整个 JSON 的 token 做交集，11 条轨迹全命中 —— 因为 JSON 的 key
    （items / docs / ok）本来就不会出现在答案里。这种规则没有区分度，只能制造噪音。
    """
    ans = _answer_text(trace)
    if not ans:
        return []
    tools = trace.tools()
    out = []
    for i, s in enumerate(tools):
        txt = s.text()
        if len(txt) < 20:
            continue          # {"ok": true} 这类确认型返回没有判断价值
        # 只看「值」：数字、ID、中文短语；JSON 的 key 不算证据
        vals = set(NUM_RE.findall(txt)) | set(ID_RE.findall(txt)) \
            | set(re.findall(r"[\u4e00-\u9fa5]{3,}", txt))
        if not vals:
            continue
        later = ans + " " + " ".join(
            json.dumps(t.args, ensure_ascii=False) for t in tools[i + 1:] if t.args)
        if not any(v in later for v in vals):
            out.append(_issue("tool_result_unused", trace, step=s,
                              detail="%s 返回的 %d 个值（如 %s）在答案与后续调用里都没出现"
                                     % (s.tool or s.name, len(vals),
                                        next(iter(sorted(vals)))[:20]),
                              fix="确认这次调用是否必要；如果结果该影响答案，说明 Agent 漏读了"))
    return out


@rule("dependency_mismatch", "tool", "warn",
      "下游工具的参数不是上游返回的值",
      "B 的参数本应来自 A 的输出，但两者对不上 —— 常见于「取错对象」和参数编造。",
      "参数可能是模型构造的（拼接字符串、换算单位），不一定是直接拷贝。")
def rule_dependency_mismatch(trace, cfg=DEFAULT):
    out = []
    tools = trace.tools()
    # 合法来源不止「上一个工具」：用户问题里的词、模型 prompt 里的约束，
    # 都可以直接变成下游参数（搜索词最典型）。漏掉这一层，
    # 每个带检索的 Agent 都会被误报一次。
    oracle = " ".join([str(getattr(trace, "task", "") or "")]
                      + [c.text() for c in trace.chats()]
                      + [json.dumps(c.args, ensure_ascii=False)
                         for c in trace.chats() if c.args])
    for i, s in enumerate(tools):
        if not isinstance(s.args, dict) or not s.args:
            continue
        before = " ".join(t.text() for t in tools[:i]) + " " + oracle
        if not before.strip():
            continue
        for k, v in s.args.items():
            if not isinstance(v, str) or len(v) < 4:
                continue
            if v not in before:
                out.append(_issue("dependency_mismatch", trace, step=s,
                                  detail="%s 的参数 %s=%r 在工具输出、用户问题与模型输入里都找不到"
                                         % (s.tool or s.name, k, v[:40]),
                                  fix="参数要从上游结果或用户输入里取；构造型参数请在 Agent 里显式记录来源"))
                break
    return out


@rule("tool_name_unknown", "tool", "warn",
      "调用了工具清单里不存在的工具",
      "典型「工具幻觉」：模型编了一个听起来合理的工具名。",
      "未配置 allowed_tools 时本条不生效；清单本身也可能过期。")
def rule_tool_name_unknown(trace, cfg=DEFAULT):
    if not cfg.allowed_tools:
        return []
    allowed = set(cfg.allowed_tools)
    bad = [s for s in trace.tools() if (s.tool or s.name) not in allowed]
    if not bad:
        return []
    s = bad[0]
    return [_issue("tool_name_unknown", trace, step=s,
                   detail="%s 不在允许的工具清单里（共 %d 次）" % (s.tool or s.name, len(bad)),
                   fix="在工具注册层做白名单；未知工具直接拒绝")]


# ============================ 流程层 ============================
@rule("write_before_read", "flow", "warn",
      "写操作发生在读取之前",
      "没有先读当前状态就写，容易覆盖或重复扣款。",
      "写操作本身幂等、或入参已包含全部前置信息。")
def rule_write_before_read(trace, cfg=DEFAULT):
    tools = trace.tools()
    first_write = next((s for s in tools if cfg.is_write(s.tool or s.name)), None)
    if first_write is None:
        return []
    reads = [s for s in tools if s.seq < first_write.seq
             and not cfg.is_write(s.tool or s.name)]
    if reads:
        return []
    return [_issue("write_before_read", trace, step=first_write,
                   detail="第一次调用就是写操作 %s，之前没有任何读取"
                          % (first_write.tool or first_write.name),
                   fix="写之前先读一次当前状态，或确认入参已含前置信息")]


@rule("loop_detected", "flow", "warn",
      "同一工具同参数被反复调用",
      "典型的成本滥用：同样的调用做三遍以上，token 和时间都在烧。",
      "该工具用于轮询等待（如查任务状态），重复是设计如此。")
def rule_loop_detected(trace, cfg=DEFAULT):
    seen = {}
    for s in trace.tools():
        key = ((s.tool or s.name),
               json.dumps(s.args, ensure_ascii=False, sort_keys=True))
        seen.setdefault(key, []).append(s)
    out = []
    for (name, _k), steps in seen.items():
        if len(steps) >= cfg.max_repeat:
            out.append(_issue("loop_detected", trace, step=steps[-1],
                              detail="%s 用同一组参数被调用 %d 次（阈值 %d）"
                                     % (name, len(steps), cfg.max_repeat),
                              fix="加去重/缓存；轮询类工具要显式标 polling 语义"))
    return out


@rule("cycle_detected", "flow", "warn",
      "工具调用陷入 A→B→A→B 循环",
      "两条以上的工具交替出现两轮，通常是 Agent 在同一个坑里来回试。",
      "工作流本身就是交替的（读→写→读→写），属于正常设计。")
def rule_cycle_detected(trace, cfg=DEFAULT):
    names = [s.tool or s.name for s in trace.tools()]
    tools = trace.tools()
    for n in range(2, len(names) // 2 + 1):
        for i in range(len(names) - 2 * n + 1):
            a, b = names[i:i + n], names[i + n:i + 2 * n]
            if a == b and len(set(a)) > 1:
                return [_issue("cycle_detected", trace, step=tools[i + 2 * n - 1],
                               detail="检测到循环：%s 连续重复两轮" % " → ".join(a),
                               fix="给循环加最大次数与退出条件；把「已经试过」写进上下文")]
    return []


@rule("premature_finish", "flow", "error",
      "没有任何有效工具结果就下定论",
      "模型在没有拿到任何工具数据的情况下给出了结论 —— 概率最高的幻觉来源。",
      "任务允许零工具（纯知识问答）；或工具结果确实为空且 Agent 如实说了「查不到」。")
def rule_premature_finish(trace, cfg=DEFAULT):
    if not trace.answer:
        return []
    useful = [s for s in trace.tools()
              if s.status == "ok" and s.text() not in ("", "{}", "[]", "null")]
    if useful:
        return []
    return [_issue("premature_finish", trace,
                   step=trace.chats()[-1] if trace.chats() else None,
                   detail="答案已产出，但全程没有一个成功的工具结果",
                   fix="拿不到数据时应当明说「查不到」而不是给结论")]


# ============================ 资源层 ============================
@rule("token_blowup", "resource", "warn",
      "单条轨迹 token 超预算",
      "长轨迹本身是成本问题；也常常意味着 Agent 在原地打转。",
      "任务本来就长（多轮对话、长文档总结）；预算需要按业务调整。")
def rule_token_blowup(trace, cfg=DEFAULT):
    total = sum(s.tokens for s in trace.steps)
    if total <= cfg.token_budget:
        return []
    return [_issue("token_blowup", trace,
                   detail="总 token %d，预算 %d" % (total, cfg.token_budget),
                   fix="压缩上下文、减少无效轮次；把预算写进 Agent 配置")]


@rule("slow_step", "resource", "info",
      "单步耗时异常",
      "某一步明显慢，可能是外部依赖抖动，也可能是重试在打转。",
      "输入本身很大（长文档、大模型首 token 慢）；阈值需要按业务调整。")
def rule_slow_step(trace, cfg=DEFAULT):
    out = []
    ts = [(s.seq, s.ts, s) for s in trace.steps if s.ts is not None]
    for i in range(1, len(ts)):
        prev_seq, prev_ts, _ = ts[i - 1]
        seq, t, s = ts[i]
        ms = (t - prev_ts) * 1000
        if ms > cfg.slow_step_ms:
            out.append(_issue("slow_step", trace, step=s,
                              detail="%s 距上一步约 %d ms（阈值 %d）"
                                     % (s.tool or s.name, int(ms), cfg.slow_step_ms),
                              fix="加超时与熔断；确认是不是重试在打转"))
    return out


# ============================ 结构层 ============================
@rule("model_error_ignored", "structure", "error",
      "模型调用报错后仍产出了最终答案",
      "模型那一步失败了，答案要么是兜底值、要么来自缓存 —— 不能当成本次推理的结果。",
      "框架会在模型失败时回退到规则/模板，且业务接受这种降级（需要显式标注）。")
def rule_model_error_ignored(trace, cfg=DEFAULT):
    errs = [s for s in trace.chats() if s.status == "error"]
    if not errs or not trace.answer:
        return []
    return [_issue("model_error_ignored", trace, step=errs[-1],
                   detail="模型调用报错(%s)，之后仍给出了答案" % errs[-1].error,
                   fix="模型失败要显式降级并标注；不要把兜底值当推理结果上报")]


@rule("parallel_write_conflict", "structure", "error",
      "相邻两步都是写操作",
      "两个写操作紧挨着发生，中间没有读取或确认 —— 并发写冲突/重复提交的典型形态。",
      "写操作之间本来就不需要读（如批量创建）；也可能是设计如此的流水线。")
def rule_parallel_write_conflict(trace, cfg=DEFAULT):
    tools = trace.tools()
    for a, b in zip(tools, tools[1:]):
        if b.seq - a.seq > 1:
            continue
        if cfg.is_write(a.tool or a.name) and cfg.is_write(b.tool or b.name):
            return [_issue("parallel_write_conflict", trace, step=b,
                           detail="连续两次写操作：%s → %s"
                                  % (a.tool or a.name, b.tool or b.name),
                           fix="写操作之间插入状态读取或幂等键；确认是否需要串行化")]
    return []


# ============================ 执行 ============================
def check(trace, cfg=None, rules=None):
    cfg = cfg or DEFAULT
    issues = []
    for fn in (rules or ALL_RULES):
        try:
            issues.extend(fn(trace, cfg) or [])
        except Exception as e:      # 单条规则崩了不能拖垮整份报告
            issues.append({
                "rule_id": "rule_crashed", "group": "structure",
                "title": "规则执行异常", "severity": "info",
                "trace_id": trace.trace_id, "agent": trace.agent, "task": trace.task,
                "step_seq": None, "step_name": None,
                "detail": "%s: %s" % (getattr(fn, "__name__", fn), e),
                "fix": "", "false_positive": "这是检查器自己的 bug，不是被检 Agent 的问题",
            })
    return issues


def check_all(traces, cfg=None, rules=None):
    out = []
    for t in traces:
        out.extend(check(t, cfg, rules))
    return out
