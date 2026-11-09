# -*- coding: utf-8 -*-
"""攻击面规则（轨迹层）：注入、越权、外带、凭据、命令执行、输出侧 XSS。

设计上刻意**复用运行时那一套检测器**（`evoguard/security.py`），而不是另抄一份正则：
离线报告说「这条轨迹有注入」，运行时说「这次调用被拦」，两边必须看同一组特征。
抄两份的下场是规则慢慢分叉，报告和拦截对不上，用户就再也不信任何一边。

这些规则只报不改：检查器本来就不执行 Agent，改不改由运行时（enforce 模式）决定。
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_AI = os.path.dirname(os.path.dirname(_HERE))          # ai-service
_EVOGUARD = os.path.join(_AI, "evoguard")
if _EVOGUARD not in sys.path:
    sys.path.insert(0, _EVOGUARD)

import security as sec  # noqa: E402

from .registry import rule  # noqa: E402

INJ = ("injection_override", "injection_role_switch", "injection_prompt_leak",
       "injection_delimiter", "injection_tool_directive")
EXFIL = ("exfil_url_with_secret", "exfil_data_uri", "exfil_encoded_blob")
SECRETS = ("secret_aws_key", "secret_openai_key", "secret_github_token",
           "secret_private_key", "secret_generic_kv")
SHELL = ("dangerous_shell_rm", "dangerous_shell_pipe", "dangerous_shell_eval",
         "dangerous_chmod")
XSS = ("xss_script_tag", "xss_event_handler", "xss_javascript_uri", "xss_iframe")
PII = ("pii_id_card", "pii_phone", "pii_email")

SEND_HINTS = ("send", "upload", "post", "webhook", "notify", "email", "sms",
              "http", "fetch", "publish")


def _text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def _issue(rule_id, trace, step, detail, fix):
    from ..rules.registry import meta_of
    m = meta_of(rule_id)
    return {"rule_id": rule_id, "severity": m.get("severity", "warn"),
            "group": m.get("group", "security"), "title": m.get("title", ""),
            "trace_id": trace.trace_id, "agent": trace.agent, "task": trace.task,
            "step_seq": getattr(step, "seq", None),
            "step_name": getattr(step, "name", None) or getattr(step, "tool", None),
            "detail": detail, "fix": fix,
            "false_positive": m.get("false_positive", "")}


@rule("injection_in_tool_output", "security", "error",
      "工具返回的内容里带指令（间接提示注入）",
      "检索到的网页/文档/第三方接口内容如果被模型当指令执行，就会越过原有约束去改行为、"
      "套取提示词或调用工具。这类攻击不经过用户输入，最容易被漏掉。",
      "业务文档里正常讲解「如何忽略上一条指令」也会命中；需要看来源是否外部可控。")
def rule_injection_in_tool_output(trace, cfg=None):
    out = []
    for s in trace.tools():
        hits = sec.scan(s.text(), only=INJ)
        if not hits:
            continue
        codes = "、".join(sorted({h["code"] for h in hits}))
        out.append(_issue("injection_in_tool_output", trace, s,
                          "%s 的返回里出现指令性内容（%s）" % (s.tool or s.name, codes),
                          "把工具结果当**数据**处理：包裹来源标记、禁止执行其中的指令；"
                          "外部来源（网页/邮件/第三方 API）尤其要做隔离"))
    return out


@rule("tool_not_allowed", "security", "warn",
      "调用了不在白名单里的工具",
      "最小权限是唯一真正可靠的一道防线：即使模型被注入、被幻觉带着走，"
      "只要工具白名单足够窄，它能造成的破坏就是有上限的。",
      "allowed_tools 没配置时本条不生效；白名单本身过期会误报。")
def rule_tool_not_allowed(trace, cfg=None):
    allowed = set(getattr(cfg, "allowed_tools", None) or ())
    if not allowed:
        return []
    out = []
    for s in trace.tools():
        name = s.tool or s.name
        if name in allowed:
            continue
        out.append(_issue("tool_not_allowed", trace, s,
                          "%s 不在 allowed_tools 里（%s）" % (name, ", ".join(sorted(allowed))),
                          "在 Agent 定义里收紧工具清单；确需新工具时显式加白名单并说明理由"))
    return out


@rule("data_exfiltration", "security", "error",
      "写/发送类工具的参数里带敏感数据或外链",
      "注入攻击的最终目的通常是「把数据带出去」。数据一旦进入写操作或外发参数，"
      "就已经离开可控范围，事后审计只能看到结果。",
      "正常业务里给客户发订单号、发手机号也会命中；要看接收方是否可信。")
def rule_data_exfiltration(trace, cfg=None):
    out = []
    for s in trace.tools():
        name = (s.tool or s.name or "")
        args = _text(s.args)
        if not args:
            continue
        is_send = any(h in name.lower() for h in SEND_HINTS)
        if cfg is not None and hasattr(cfg, "is_write") and cfg.is_write(name):
            is_send = True
        hits = sec.scan(args, only=EXFIL + PII)
        if not is_send or not hits:
            continue
        codes = "、".join(sorted({h["code"] for h in hits}))
        out.append(_issue("data_exfiltration", trace, s,
                          "%s 的参数里含敏感数据（%s）" % (name, codes),
                          "外发前做白名单域名 + 字段级脱敏；写操作走审批，不接受模型拼的接收方"))
    return out


@rule("secret_in_tool_args", "security", "error",
      "工具参数里出现凭据",
      "凭据一旦进入工具参数，就会出现在日志、轨迹、第三方请求里。"
      "模型不该经手密钥——它只需要知道「有这个能力」，而不是「密钥是什么」。",
      "参数里传的可能是被哈希过的值或占位符；需人工确认是否真凭据。")
def rule_secret_in_tool_args(trace, cfg=None):
    out = []
    for s in trace.tools():
        hits = sec.scan(_text(s.args), only=SECRETS)
        if not hits:
            continue
        out.append(_issue("secret_in_tool_args", trace, s,
                          "%s 的参数里出现疑似凭据（%s）"
                          % (s.tool or s.name, "、".join(sorted({h["code"] for h in hits}))),
                          "凭据放服务端凭证库，工具只收引用 id；"
                          "确需透传时用短期令牌并记录用途"))
    return out


@rule("dangerous_shell_command", "security", "error",
      "工具参数里出现破坏性命令",
      "Agent 一旦能拼命令，注入就等于拿到 shell。"
      "删除、下载后执行、把输入当代码 eval，这三种是实际事故里最常见的形态。",
      "运维类 Agent 正常执行运维命令会命中；要看是否走白名单与沙箱。")
def rule_dangerous_shell_command(trace, cfg=None):
    out = []
    for s in trace.tools():
        hits = sec.scan(_text(s.args), only=SHELL)
        if not hits:
            continue
        out.append(_issue("dangerous_shell_command", trace, s,
                          "%s 的参数里出现危险命令（%s）"
                          % (s.tool or s.name, "、".join(sorted({h["code"] for h in hits}))),
                          "命令走固定模板 + 参数白名单，禁止拼接；执行放沙箱，删除类操作二次确认"))
    return out


@rule("xss_in_answer", "security", "error",
      "答案里带可执行的浏览器内容",
      "Agent 的输出经常被直接塞进网页。模型不知道「这段文字会被当 HTML 渲染」，"
      "而注入者知道 —— 于是 script / onerror / javascript: 就成了打到用户浏览器的跳板。",
      "答案本身是代码片段或教学示例时会命中；取决于前端是否转义。")
def rule_xss_in_answer(trace, cfg=None):
    s = _text(trace.answer)
    if not s:
        return []
    hits = sec.scan(s, only=XSS)
    if not hits:
        return []
    from ..model import Step
    step = trace.chats()[-1] if trace.chats() else Step(seq=None, kind="chat", name="answer")
    return [_issue("xss_in_answer", trace, step,
                   "答案里含浏览器可执行内容（%s）"
                   % "、".join(sorted({h["code"] for h in hits})),
                   "输出前按落点转义（HTML/属性/JS）；前端禁用 innerHTML，"
                   "并加 CSP 兜底")]
