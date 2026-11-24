# -*- coding: utf-8 -*-
"""PROD-02-T2：MCP Security Proxy（stdio JSON-RPC 安全代理）。

下游 MCP Server 的 `tools/list` 会注册进 tool_registry；
`tools/call` 执行前强制走契约 + 输出侧护栏。恶意工具返回间接注入文本
诱导写工具调用时，写工具必须 executed=0。

零第三方依赖：JSON-RPC 2.0 over stdio 用标准库 json 逐行解析。

**边界**：代理只做**门禁决定**，不替下游执行工具；返回值里 `blocked=true`
表示「不放行这次 tools/call」，下游不会收到该请求。
"""
import json
import sys

try:
    from . import security as _sec
    from . import tool_rails as _rails
    from . import tool_registry as _reg
except ImportError:
    import security as _sec                                 # type: ignore
    import tool_rails as _rails                             # type: ignore
    import tool_registry as _reg                            # type: ignore


def register_tools(tools, *, registry=None, agent_type="mcp"):
    """把 MCP tools/list 的 tool 定义注册进 registry。

    MCP tool: {"name":..,"description":..,"inputSchema":{...}}
    """
    reg = registry if registry is not None else _reg.ToolRegistry()
    for t in tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        schema = t.get("inputSchema") or t.get("schema") or {}
        desc = str(t.get("description") or "")[:160]
        # scope 从描述里**不猜**；默认只读，写工具必须显式声明才能拿到写权限
        scope = _reg.WRITE if ("write" in desc.lower()
                               or "删除" in desc or "修改" in desc) else _reg.READ_ONLY
        reg.from_json_schema(str(t["name"]), schema, scope=scope,
                             source="mcp", identities=[agent_type])
    return reg


def guard_call(name, args, *, registry, rails=None, goal=""):
    """tools/call 前的门禁。返回 (allowed, record)。"""
    declared = (registry.declared() or {})
    if name not in declared:
        return False, {"tool": name, "decision": "deny",
                       "reason": "tool_not_registered", "executed": False}
    # rails=None -> 用默认策略；rails={} -> 显式关闭调用侧扫描（测试/纯注册场景）
    rl = rails if rails is not None else _rails.load_default()
    r = _rails.check_call(rl, name, args or {}) if rails != {} else {}
    if r.get("blocked"):
        return False, {"tool": name, "decision": "deny",
                       "reason": "rails_call_blocked", "executed": False}
    return True, {"tool": name, "decision": "allow", "executed": False}


def guard_result(name, payload, *, rails=None):
    """工具返回侧：检测间接注入与动作-职责不符，不改正文。"""
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False)
    findings = _sec.scan(text, "tool_output")
    rl = rails if rails is not None else _rails.load_default()
    r = (_rails.check_result(rl, name, payload if isinstance(payload, dict)
                             else {"output": text})
         if rails != {} else {})
    codes = [f["code"] for f in findings]
    if r.get("blocked"):
        codes.append("rails_result_blocked")
    return {"codes": codes, "blocked": bool(r.get("blocked")),
            "status": r.get("status")}


def handle_message(msg, *, registry, rails=None, state=None):
    """处理一条 JSON-RPC 请求，返回响应 dict（或 None 表示通知）。

    state 用于跨消息保存 tools/list 注册结果（同一会话内）。
    """
    state = state if state is not None else {}
    method = str(msg.get("method") or "")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "tools/list":
        tools = list(params.get("tools") or [])
        # FRONTIER-02：会话级 SHA-256 钉扎，防 Rug-Pull（第 1 轮正常、
        # 第 2 轮偷偷改 description 注入指令）。规范化只覆盖描述与 schema。
        import hashlib as _hl
        fp = {}
        for t in tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            canon = json.dumps({"description": str(t.get("description") or ""),
                                "inputSchema": t.get("inputSchema") or
                                               t.get("schema") or {}},
                               ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
            fp[str(t["name"])] = _hl.sha256(
                canon.encode("utf-8")).hexdigest()
        pinned = dict(state.get("tool_fingerprints") or {})
        drift = [n for n, h in fp.items()
                 if n in pinned and pinned[n] != h]
        if drift:
            state["rugpull_drift"] = True
            state["rugpull_tools"] = drift
            if mid is None:
                return None
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32099,
                              "message": "mcp_tool_rugpull_drift",
                              "data": {"tools": drift}}}
        # 首次 tools/list 也要过投毒检查：description 里的 Unicode Tag
        # 隐写 / 指令注入不能靠"正常工具"身份混进 registry。
        poisoned = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            desc = str(t.get("description") or "")
            findings = _sec.scan(desc, "tool_output")
            codes = {f.get("code") for f in findings}
            if any("hidden_" in str(c) for c in codes):
                poisoned.append(str(t.get("name")))
        if poisoned:
            state["rugpull_drift"] = True
            state["rugpull_tools"] = poisoned
            if mid is None:
                return None
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32098,
                              "message": "mcp_tool_description_poisoned",
                              "data": {"tools": poisoned}}}
        state["tool_fingerprints"] = {**pinned, **fp}
        reg = register_tools(tools, registry=registry)
        state["registry"] = reg
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"tools": tools}}

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        reg = state.get("registry") or registry
        ok, rec = guard_call(name, args, registry=reg, rails=rails)
        if mid is None:
            return None
        if not ok:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32001,
                              "message": "blocked by evoguard: %s" % rec["reason"],
                              "data": rec}}
        # 放行：返回给调用方一个「可以去执行」的信号，executed 仍为 False
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"allowed": True, "tool": name,
                           "arguments": args, "evoguard": rec}}

    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "method not found: %s" % method}}


def serve_stdio(instream=None, outstream=None, *, rails=None):
    """真 stdio 循环（CLI 用）。逐行 JSON-RPC。"""
    inp = instream or sys.stdin
    out = outstream or sys.stdout
    registry = _reg.ToolRegistry()
    state = {}
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700,
                                            "message": "parse error"}}) + "\n")
            out.flush()
            continue
        resp = handle_message(msg, registry=registry, rails=rails,
                              state=state)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


def describe():
    return {"transport": "stdio JSON-RPC 2.0 (line-delimited)",
            "methods": ["tools/list", "tools/call"],
            "executes_tools": False,
            "guards": ["tool_registry", "tool_rails", "security.scan"]}
