# -*- coding: utf-8 -*-
"""从公开生产事故里提炼的轨迹规则（6 条）。

和 trace_rules.py 的区别：那边是「通用 hygiene」，这边每条都对应一个真实
事故模式，来源是 2025-2026 年公开报道 / postmortem / 框架失败模式综述。

  self_report_contradiction  Replit 删库后伪造测试结果、谎报不可恢复
  retry_storm                31,000 次重试烧掉 $4,700 / 761 次调用 60 秒
  silent_stall               CrewAI 嵌套路由静默死锁（无结果、无报错、无日志）
  write_timeout_ambiguous    状态变更类工具超时后重试，exactly-once 未解
  retry_on_permanent_error   把 401/参数错误当瞬时故障重试
  hallucination_propagation  多 Agent 里上游编造的值被下游当事实继续用

为什么要单独一个文件：规则要能回答「你凭什么加这条」。每条规则对应一类
有公开案例的失败，而不是拍脑袋想的检查项。案例与规则的完整对照表见
experiments/inspect/agent_failure_cases.md。

原则不变：不调用模型、给证据位置、写清误报、单条崩溃不拖垮报告。
"""
import json

from ..config import DEFAULT
from .registry import meta_of, rule
from .trace_rules import ID_RE

# 「我干完了」的自述词。只认明确的完成/通过措辞，不认「处理了」这种模糊说法，
# 否则正常答案会被误报。
SUCCESS_WORDS = ("完成", "成功了", "已修复", "已处理完", "已提交", "已生成",
                 "已保存", "全部通过", "测试通过", "搞定", "没问题了",
                 "completed", "succeeded", "successfully", "all tests passed",
                 "tests passed", "done and verified", "no issues")
CLAIM_VERIFIED = ("测试通过", "已验证", "验证过", "全部通过", "tests passed",
                  "verified", "all checks passed")
VERIFY_HINTS = ("test", "verify", "check", "validate", "assert", "lint", "pytest",
                "测试", "校验", "验证")
PERMANENT_ERR = ("401", "403", "404", "400", "unauthorized", "forbidden",
                 "permission denied", "invalid api key", "authentication",
                 "validation error", "invalid argument", "not found",
                 "权限", "未授权", "认证失败", "参数错误", "不存在")
TIMEOUT_ERR = ("timeout", "timed out", "deadline exceeded", "超时", "expired")


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


def _ans(trace):
    a = trace.answer
    return (a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)) if a is not None else ""


@rule("self_report_contradiction", "answer", "error",
      "答案自称成功，但轨迹里的证据不支持",
      "Agent 既是执行者又是唯一证人。Replit 事故里它删了库、伪造测试记录、"
      "还谎称无法回滚；任何只信模型自述的系统都会漏掉这类错误。",
      "成功词可能只是客套话；也可能 Agent 确实修复了，但轨迹没记录全。"
      "结论要以基础设施日志和状态为准，不能只凭这条规则定性。")
def rule_self_report_contradiction(trace, cfg=DEFAULT):
    ans = _ans(trace).lower()
    if not ans or not any(w.lower() in ans for w in SUCCESS_WORDS):
        return []
    failed = [s for s in trace.steps if s.error]
    if failed:
        last = failed[-1]
        name = last.tool or last.name
        # 关键：报错后**同工具重试成功**属于正确恢复，这时说「完成」是诚实的。
        # 不加这道判断，就会把健康地处理了故障的 Agent 标成「谎报」——
        # 这类误报会最快毁掉一个检查工具的可信度（用 trace_retry_ok 回归验证）。
        recovered = any(
            t.seq > last.seq and (t.tool or t.name) == name and not t.error
            and len(t.text().strip()) >= 10
            for t in trace.tools())
        if not recovered:
            return [_issue(
                "self_report_contradiction", trace, step=last,
                detail="答案自称完成/通过，但轨迹里有 %d 个失败步骤且未恢复（最后：%s，%s）"
                       % (len(failed), name, str(last.error)[:60]),
                fix="不要采信 Agent 的自述；用工具返回状态和独立日志核对最终结论，"
                    "失败步骤必须在答案里如实披露")]
    # 第二种形态：宣称「测试通过」，但轨迹里根本没有任何验证类调用。
    if any(w in _ans(trace) for w in CLAIM_VERIFIED):
        tools = trace.tools()
        verified = any(any(h in (t.tool or t.name or "").lower() for h in VERIFY_HINTS)
                       for t in tools)
        if not verified:
            return [_issue(
                "self_report_contradiction", trace,
                detail="答案宣称已验证/测试通过，但轨迹里没有一次验证类工具调用",
                fix="把「通过」绑定到真实的验证步骤输出；没有跑过就不要写通过")]
    return []


@rule("retry_storm", "flow", "error",
      "同一个失败被连续重试，没有熔断",
      "公开案例：某 Agent 对一个失败工具重试约 31,000 次，一夜烧掉 $4,700；"
      "另有 60 秒内 761 / 1,384 次调用的空闲超时重试。重试路径没有硬上限时，"
      "一次普通故障会直接变成账单事故。",
      "固定次数的重试本身是正确做法；外部服务持续故障也可能造成连续失败。"
      "本条只提示「连续失败没有触发停止」，是否已做退避/熔断要看配置和监控。")
def rule_retry_storm(trace, cfg=DEFAULT):
    run, worst, at = 0, 0, None
    for s in trace.steps:
        if s.error:
            run += 1
            if run > worst:
                worst, at = run, s
        else:
            run = 0
    limit = max(2, int(cfg.max_repeat or 3))
    if worst >= limit:
        return [_issue(
            "retry_storm", trace, step=at,
            detail="连续 %d 步报错、中间没有一次成功（阈值 %d）" % (worst, limit),
            fix="加运行时强制的熔断：连续失败计数达到上限就停止付费调用，"
                "成功才清零；配合指数退避与单次运行的费用上限")]
    return []


@rule("silent_stall", "flow", "warn",
      "连续多步无有效产出、无报错、不终止",
      "最危险的失败是「什么都没发生」：107 个任务实测里，CrewAI 在工具委派链"
      "IO 映射断裂时既不报错也不重试，循环空转却没有任何表层日志。",
      "轮询/等待类工具返回空是正常语义；流式响应的中间帧也可能是空。"
      "轮询工具应显式标注 polling 语义，本条对它不适用。")
def rule_silent_stall(trace, cfg=DEFAULT):
    empties = ("", "{}", "[]", "null", "none", "{}")
    run, at = 0, None
    for s in trace.tools():
        if s.error:
            run = 0
            continue
        txt = s.text().strip()
        if len(txt) < 10 or txt.lower() in empties:
            run += 1
            at = s
        else:
            run = 0
        if run >= 3:
            return [_issue(
                "silent_stall", trace, step=at,
                detail="连续 %d 个工具步骤没有实质返回，也没报错或收尾" % run,
                fix="给循环加最大空转次数与显式超时；空结果要写日志并向上抛，"
                    "不要让调用链静默挂起")]
    return []


@rule("write_timeout_ambiguous", "structure", "warn",
      "写操作超时后又用同样参数重试",
      "状态变更类工具超时时，调用方不知道服务端到底执行了没有。此时用同参数"
      "再调一次，可能重复扣款 / 重复下单 —— MCP 生态里 exactly-once 语义至今"
      "没有通用解法，只能靠幂等键和查状态来兜。",
      "调用方可能带了幂等键，或第一次确实没执行成功（这时重试是对的）。"
      "本条是提示「需要确认」，不是判定「一定重复了」。")
def rule_write_timeout_ambiguous(trace, cfg=DEFAULT):
    tools = trace.tools()
    for i, s in enumerate(tools):
        err = (s.error or "").lower()
        if not err or not any(k in err for k in TIMEOUT_ERR):
            continue
        name = s.tool or s.name
        if not cfg.is_write(name):
            continue
        for t in tools[i + 1:]:
            if (t.tool or t.name) == name and (t.args or None) == (s.args or None):
                return [_issue(
                    "write_timeout_ambiguous", trace, step=t,
                    detail="%s 第 %d 步超时后，第 %d 步用完全相同的参数再次调用"
                           % (name, s.seq, t.seq),
                    fix="写工具带幂等键（idempotency_key / request_id）；超时后"
                        "先查状态再决定是否重试，不要盲重试")]
    return []


@rule("retry_on_permanent_error", "tool", "warn",
      "把永久性错误当成瞬时故障继续重试",
      "401/403、参数校验失败、404 这类错误重试多少次都不会成功，只会烧钱和"
      "拖长延迟。生产级 Agent 应该在工具包装层把错误分成 transient / permanent "
      "/ input_error，按类型路由，而不是把原始错误字符串丢给模型自己猜。",
      "错误信息措辞可能含糊；也可能重试期间权限被修复、资源被创建出来了。"
      "以错误码为准，不要只看文本。")
def rule_retry_on_permanent_error(trace, cfg=DEFAULT):
    tools = trace.tools()
    for i, s in enumerate(tools):
        err = (s.error or "").lower()
        if not err or not any(k in err for k in PERMANENT_ERR):
            continue
        name = s.tool or s.name
        for t in tools[i + 1:]:
            if (t.tool or t.name) == name:
                return [_issue(
                    "retry_on_permanent_error", trace, step=s,
                    detail="第 %d 步是永久性错误（%s），第 %d 步仍重复调用 %s"
                           % (s.seq, str(s.error)[:60], t.seq, name),
                    fix="在工具层标注 error_type；permanent / input_error 直接"
                        "停止并把错误回传给模型修正参数，只有 transient 才重试")]
    return []


@rule("hallucination_propagation", "structure", "warn",
      "下游步骤用了上游没有出处的值",
      "多 Agent 里一个 Agent 编造的事实会被下一个当 ground truth 继续用，"
      "误差沿链路放大（AutoGen 会话放大、LangGraph/CrewAI 无内建防御）。"
      "这里检查：下游工具参数里出现了一个此前所有工具输出都没给过的标识。",
      "标识可能来自用户输入但任务字段没记全，也可能是合法常量或按规则生成的编号。"
      "只查 ID 形态、不查普通数字，就是为了压低这类误报。")
def rule_hallucination_propagation(trace, cfg=DEFAULT):
    tools = trace.tools()
    if len(tools) < 3:
        return []
    for i, s in enumerate(tools[1:], start=1):
        args_txt = json.dumps(s.args, ensure_ascii=False) if s.args else ""
        ids = set(ID_RE.findall(args_txt))
        if not ids:
            continue
        prior = " ".join(t.text() for t in tools[:i]) + " " + (trace.task or "")
        bad = sorted(x for x in ids if x not in prior)
        if bad:
            return [_issue(
                "hallucination_propagation", trace, step=s,
                detail="%s 的参数引用了 %s，但前面 %d 个工具输出里都没有它"
                       % (s.tool or s.name, ", ".join(bad[:3]), i),
                fix="跨 Agent 传递的值要带来源标记；关键字段在交接处做一次"
                    "存在性校验，别把上游文本直接当结构化事实")]
    return []
