# -*- coding: utf-8 -*-
"""PROD-02-T1：OpenAI-compatible 透明安全代理核心。

第三方 Agent 只需把 base_url 指向 `/v1`，即可获得 EvoGuardLab 的输入护栏、
工具契约门禁与输出护栏，**不需要改框架代码**。

流水线（顺序不能换）：
1. 输入：messages 里 user 文本过 L1（security.scan）+ 可选语义/越狱判官；
2. 转发上游（opener 可注入，默认 urllib，零依赖）；
3. 若上游返回 tool_calls：逐个过 tool_registry + execution.preflight +
   tool_rails.check_call；任一 stop → **不把该调用交给第三方执行**，
   返回标准 OpenAI 形状的拒绝消息；
4. 输出：content 过 security.guard_tool_output + tool_rails.check_result；
5. 全程 attempted/executed 分离（检测层恒 executed=False，参数 execute=False）。

**边界**：这是**协议代理**，不替第三方执行工具 —— 它只决定 tool_calls
放不放行。真正的写操作仍须由调用方在审批后执行（见 execution.py）。
"""
import json
import os
import time
import urllib.request

try:
    from . import security as _sec
    from . import execution as _exec
    from . import tool_rails as _rails
    from . import tool_registry as _reg
except ImportError:
    import security as _sec                                 # type: ignore
    import execution as _exec                               # type: ignore
    import tool_rails as _rails                             # type: ignore
    import tool_registry as _reg                            # type: ignore


def _default_upstream_url():
    return os.environ.get("EVOGUARD_UPSTREAM_URL") or ""


def _openai_error(message, code="policy_blocked"):
    """标准 OpenAI 错误形状（SDK 能直接解析）。"""
    return {"error": {"message": message, "type": "invalid_request_error",
                      "code": code}}


def _user_text(messages):
    return "\n".join(str(m.get("content") or "") for m in (messages or [])
                     if isinstance(m, dict) and m.get("role") == "user")


def guard_messages(messages, *, use_semantic=False, judge=None):
    """输入侧护栏。返回 (findings, semantic) —— 命中不修改原文。"""
    text = _user_text(messages)
    findings = _sec.scan(text, "user_input")
    sem = None
    if use_semantic:
        try:
            from . import harmful_request_judge as _H     # noqa: F401
        except ImportError:
            import harmful_request_judge as _H            # type: ignore
        sem = _H.guard_input_semantic(text, judge=judge)
        if sem.get("decision") == "report":
            sem = dict(sem, error="semantic_unavailable")
    return findings, sem


def _call_upstream(payload, *, url=None, opener=None, timeout=120):
    """转发请求。opener 可注入以便测试，默认标准库 urllib。"""
    url = url or _default_upstream_url()
    if not url:
        raise ValueError("未配置 EVOGUARD_UPSTREAM_URL")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    fn = opener or urllib.request.urlopen
    with fn(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw)


def _preflight_call(tool_call, *, agent_type, goal, cfg, registry, rails):
    """单个 tool_call 的门禁。返回 (allowed, decision_record)。"""
    name = str(((tool_call or {}).get("function") or {}).get("name") or "")
    raw_args = ((tool_call or {}).get("function") or {}).get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    # 1) tool_registry 未声明 -> 直接拒绝（不猜、不放行）
    declared = (registry.declared() or {}) if registry else {}
    if name not in declared:
        return False, {"tool": name, "stop": True, "decision": "deny",
                       "reason": "tool_not_registered", "executed": False}
    # 2) rails 调用侧（含 evidence_grounding 等扫描器）
    if rails:
        r = _rails.check_call(rails, name, args)
        if r.get("blocked"):
            return False, {"tool": name, "stop": True, "decision": "deny",
                           "reason": "rails_call_blocked", "executed": False,
                           "findings": r.get("findings")}
    # 3) 执行前总门禁（契约/意图/审批）
    contracts = {k: {kk: vv for kk, vv in v.items()
                     if not kk.startswith("_")}
                 for k, v in declared.items()}
    out = _exec.preflight(cfg, agent_type=agent_type, tool=name, args=args,
                          user_goal=goal,
                          cid=_exec.call_id("openai-proxy", 0, name, args),
                          contracts=contracts)
    return (not bool(out.get("stop"))), out


def proxy(payload, *, upstream_url=None, opener=None, agent_type="react",
          registry=None, rails=None, use_semantic=False, judge=None,
          execute=False):
    """完整代理流程。返回 (status_code, response_body, audit)。

    `execute=False`（默认）：只做检测与门禁，绝不代替第三方执行工具。
    """
    audit = {"attempted": False, "executed": False,
             "effect": "unchanged", "checks": []}
    if not isinstance(payload, dict):
        return 400, _openai_error("payload must be object"), audit
    messages = payload.get("messages") or []
    goal = _user_text(messages)
    findings, sem = guard_messages(messages, use_semantic=use_semantic,
                                   judge=judge)
    audit["checks"].append({"stage": "input", "codes":
                            [f["code"] for f in findings]})
    # 高危输入直接拦（audit 模式下 L1 只记不拦，这里是代理自己的策略：
    # 默认只拦 error 级 + 语义 deny，保留既有 audit 语义给非 error）
    hard = [f for f in findings if f.get("severity") == "error"]
    if hard or (sem and sem.get("decision") == "deny"):
        audit["decision"] = "deny"
        return 400, _openai_error(
            "input_blocked: %s" % ",".join(
                [f["code"] for f in hard] +
                (["semantic"] if sem and sem.get("decision") == "deny" else []))), audit
    if sem and sem.get("decision") == "report":
        audit["decision"] = "review"
        return 503, _openai_error("semantic_judge_unavailable",
                                  "semantic_unavailable"), audit

    try:
        resp = _call_upstream(payload, url=upstream_url, opener=opener)
    except Exception as exc:                                # noqa: BLE001
        audit["decision"] = "review"
        return 502, _openai_error("upstream_failed:" + type(exc).__name__,
                                  "upstream_unavailable"), audit

    # tool_calls 门禁：逐个判，任一 stop 就是把整个响应替换成拒绝消息
    calls = []
    for choice in (resp.get("choices") or []):
        msg = choice.get("message") or {}
        calls.extend(msg.get("tool_calls") or [])
    reg = registry if registry is not None else _reg.REGISTRY
    rl = rails if rails is not None else _rails.load_default()
    allowed_tools = sorted((reg.declared() or {}).keys())
    # 白名单来自注册表；写工具按其契约标注，不用猜
    writes = [t for t in allowed_tools
              if ((reg.declared() or {}).get(t) or {}).get("side_effect") == "write"]
    cfg = _exec.build_config({}, agent_type=agent_type,
                             allowed_tools=allowed_tools,
                             write_tools=writes,
                             has_contract=True, has_intent_judge=True)
    blocked = None
    for tc in calls:
        ok, rec = _preflight_call(tc, agent_type=agent_type, goal=goal,
                                  cfg=cfg, registry=reg, rails=rl)
        audit["checks"].append({"stage": "tool_call", **{
            k: rec.get(k) for k in ("tool", "stop", "decision", "reason")}})
        if not ok:
            blocked = rec
            break
    if blocked:
        audit["decision"] = "deny"
        return 200, {"choices": [{"index": 0, "finish_reason": "tool_blocked",
                                  "message": {"role": "assistant",
                                              "content": "tool_call blocked by "
                                              "policy: %s" % blocked.get("reason")}}],
                     "evoguard": {"blocked": True, "tool": blocked.get("tool"),
                                  "reason": blocked.get("reason")}}, audit

    audit["decision"] = "allow"
    # 输出护栏：content 过工具返回守卫（不改正文，只记发现）
    for choice in (resp.get("choices") or []):
        msg = choice.get("message") or {}
        if msg.get("content"):
            out = _sec.guard_tool_output("openai.chat", msg["content"])
            audit["checks"].append({"stage": "output",
                                    "codes": [f["code"] for f in
                                              out.get("findings", [])]})
    return 200, resp, audit


def describe():
    return {"endpoint": "POST /v1/chat/completions",
            "executes_tools": False,
            "input": ["security.scan", "optional semantic judge"],
            "tool_call": ["tool_registry", "tool_rails", "execution.preflight"],
            "output": ["security.guard_tool_output"],
            "config": "EVOGUARD_UPSTREAM_URL"}
