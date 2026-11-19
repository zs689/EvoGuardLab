# -*- coding: utf-8 -*-
"""按工具声明式的双向护栏（融合 NeMo Guardrails 的 `rails.tool_call/tool_result.per_tool`）。

从 NeMo 那里学到的四件事，这一层负责把它们落地：

1. **同一份配置同时约束「调用前」和「返回后」**。上游是
   `rails.tool_call.per_tool.run_sql: [...]` 与 `rails.tool_result.per_tool.run_sql: [...]`
   —— 同一个工具，两个方向各挂各的检查。本仓库原来只有返回侧
   （`action_scope` 判职责），调用侧散在 `tool_contract` 里，没有一个地方能一眼看出
   「这个工具进和出分别要过什么」。
2. **配置缺项在加载时炸，不在运行时才炸**。上游：`$model=` 写了个没定义的模型，
   配置加载就失败，报错里带 flow 与 tool 名；prompt 缺 `output_parser` 同样加载失败。
   这里对应：扫描器名不存在 / 流程名写错 / 字段作用域用在返回侧 → 加载即报错，
   报错必须带「哪个工具、哪个方向、哪个名字」。
3. **未声明的工具在跑任何检查之前就被挡**。上游 `@tool_call_validation`：
   模型调了一个没声明的工具，先挡住，判官模型根本不会被调用。
4. **判定失败有独立状态**。上游的 `parse_tool_safety_verdict` 只认
   `safe` / `unsafe: ...`，其它一律当「判官输出坏了」抛异常，**不解释成一个安全结论**。
   这里同样：扫描器抛错 → `status="error"`，绝不折算成 safe。

与 `tool_contract` 的分工（刻意不重复）：
  - `tool_contract` 管**结构**：身份、资源、域名、金额、必填字段、别名冲突。
    它比 LLM 判官更严，也更便宜。
  - 这一层管**声明**：哪个工具在哪个方向要过哪些检查。默认只在
    「结构契约覆盖不到」的地方挂检查（例如 `shell.exec` 的自由文本参数），
    返回侧则全量挂职责检查。
  两边都挂同一件事没有增量，只会让延迟翻倍 —— 所以默认配置刻意不重复。
"""
import os

OPEN, BLOCKED = "open", "blocked"

# 判定状态：刻意分开，不许互相顶替
SAFE = "safe"            # 跑完了，没命中
UNSAFE = "unsafe"        # 跑完了，命中
ERROR = "error"          # 检查器自己坏了 —— 不是 safe
NOT_DECLARED = "not_declared"   # 工具没声明 —— 在跑检查之前就挡

FLOWS = ("call", "result")
_ALLOWED_SPEC_KEYS = ("scanner", "fields", "severity")


class RailsConfigError(ValueError):
    """配置本身不成立。必须在加载期抛，不能等到运行时才拦一半。"""


def _builtin_scanners():
    """可用的扫描器名 → 轻量包装。名字与 scan_pipeline 的注册表保持一致。"""
    return {
        "regex": _scan_regex,
        "hidden_ascii": _scan_hidden,
        "action_scope": _scan_action_scope,
        "evidence_grounding": _scan_evidence_grounding,
        "code": _scan_code,
        "schema": _scan_schema,
    }


def _scan_regex(tool, payload, fields=None):
    import security
    text = _pick(payload, fields)
    return [(f["code"], f["severity"]) for f in security.scan(text)]


def _scan_hidden(tool, payload, fields=None):
    import security
    text = _pick(payload, fields)
    out = []
    for f in security.scan(text):
        if f["code"].startswith("hidden_"):
            out.append((f["code"], f["severity"]))
    return out


def _scan_action_scope(tool, payload, fields=None):
    import action_scope
    f = action_scope.check_output_scope(tool, _pick(payload, fields))
    return [(f["code"], f["severity"])] if f else []


def _scan_evidence_grounding(tool, payload, fields=None):
    """输出侧事实核验（WS-05-T2）：句级原子事实 + 证据池数值对齐。

    payload 需带 `evidence_sections`（`{"doc#sec": "正文"}`）；
    没有证据源时不硬判失败，返回 info 级「无可核验证据」——
    宁可提示也不把「没证据」当成「通过」。
    """
    import evidence_gate.gate as G
    # 不能把整个 dict 转文本：那样会把 evidence_sections 的正文混进答案，
    # 篡改数字会被证据原文“洗白”。只取答案字段。
    if fields and isinstance(payload, dict):
        text = _pick(payload, fields)
    elif isinstance(payload, dict):
        text = _as_text(payload.get("answer") or payload.get("text") or payload)
    else:
        text = _as_text(payload)
    sections = payload.get("evidence_sections") if isinstance(payload, dict) else None
    if not sections:
        return [("evidence_no_source", "info")]
    r = G.check(text, sections)
    label = str(r.get("label") or "")
    if label in ("PASS", "pass", "allow", "可核验"):
        return []
    # 只有「有引用但对不上」才是 error；「没引用」是可疑但未必造假，记 warn。
    severe = ("MISMATCH" in label or "NOT_FOUND" in label
              or "数字对不上" in label or "引用不存在" in label)
    return [("evidence_grounding", "error" if severe else "warn")]


def _scan_code(tool, payload, fields=None):
    import code_guard
    # code_guard.scan_code 返回的是 findings **列表**，不是 dict。
    # 旧代码按 r["findings"] 取值会 TypeError（list 不能用 str 下标），
    # 一旦工具真含代码内容就崩 —— 之前无 code 扫描场景所以没暴露。
    findings = code_guard.scan_code(_pick(payload, fields))
    return [(x.get("cwe") or "code_issue",
             "error" if x.get("severity") == "high" else "warn")
            for x in findings]


def _scan_schema(tool, payload, fields=None):
    import tool_contract
    bad = [c for c in tool_contract.validate_args(tool, payload)
           if not c["ok"]]
    return [(c["rule"], "error") for c in bad]


def _pick(payload, fields):
    """`fields=` 作用域：只把指定字段送进检查。

    上游对应 `$argument=query`：少送数据给判官，也省 token。
    这里同样只对**调用侧**有意义，返回侧没有字段结构（配置会拒绝）。
    """
    if not fields:
        return payload if isinstance(payload, str) else _as_text(payload)
    if not isinstance(payload, dict):
        return _as_text(payload)
    return _as_text({k: payload[k] for k in fields if k in payload})


def _as_text(v):
    import json
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


# ---------------- 默认配置 ----------------
# 只有「结构契约覆盖不到」的地方才在调用侧挂检查；返回侧全量挂职责检查。
DEFAULT_RAILS = {
    "kb.search": {"call": [], "result": ["regex", "hidden_ascii", "action_scope"]},
    # INT-01-T2：输出侧事实核验正式启用。单独一个工具名而不是塞进 kb.search，
    # 是因为它需要 payload 带 evidence_sections；塞进普通检索会让每次调用都
    # 报一条 evidence_no_source，把真正的信号淹掉。
    "kb.answer_grounded": {
        "call": [],
        "result": ["regex", "hidden_ascii", "action_scope",
                   {"scanner": "evidence_grounding"}]},
    "kb.search_all": {"call": [], "result": ["regex", "hidden_ascii", "action_scope"]},
    "order.get_order": {"call": [], "result": ["regex", "hidden_ascii", "action_scope"]},
    "order.get_quote_order": {"call": [], "result": ["regex", "hidden_ascii", "action_scope"]},
    "pricing.calc_total": {"call": [], "result": ["regex", "hidden_ascii"]},
    "pricing.quote": {"call": [], "result": ["regex", "hidden_ascii"]},
    "pricing.subtotal": {"call": [], "result": ["regex", "hidden_ascii"]},
    "calc.apply_rules": {"call": [], "result": ["regex", "hidden_ascii"]},
    "crm.tier": {"call": [], "result": ["regex", "hidden_ascii", "action_scope"]},
    "task.make_plan": {"call": [], "result": ["regex", "hidden_ascii"]},
    "order.settle": {"call": [], "result": ["regex", "hidden_ascii"]},
    "http.send": {
        # 自由文本载荷：结构契约只管域名，管不到载荷里是什么
        "call": [{"scanner": "regex", "fields": ["payload"]}],
        "result": ["regex", "hidden_ascii"],
    },
    "shell.exec": {
        # 任意命令：这里必须看，且要按 error 级挡
        "call": [{"scanner": "code", "severity": "error"}],
        "result": ["regex", "hidden_ascii"],
    },
    # AGENT-E2E-01：自主修 Bug 标准工具集。未在此声明的工具会被
    # execution.preflight 直接挡（fail-closed），所以这些必须登记。
    "fs.read": {"call": [], "result": ["regex", "hidden_ascii", "action_scope"]},
    "fs.write": {"call": [], "result": ["regex", "hidden_ascii"]},
    "code.run_in_sandbox": {
        # 待执行代码本身可能藏注入：调用侧做 code 扫描（warn 记录不误伤）
        "call": [{"scanner": "code", "severity": "warn"}],
        "result": ["regex", "hidden_ascii"],
    },
}


def all_known_tools():
    """全仓库登记过的工具名：执行层 + 职责表 + 结构契约。

    三处登记的集合不一定相同（`shell.exec` 只在契约里有），
    所以取并集 —— 少算一个，合法工具就会被当成「未声明」挡掉。
    """
    names = set()
    try:
        import sim
        names |= set(sim.TOOLS)
    except Exception:                     # noqa: BLE001
        pass
    try:
        import action_scope
        names |= set(action_scope.TOOL_SCOPE)
    except Exception:                     # noqa: BLE001
        pass
    try:
        import tool_contract
        names |= set(tool_contract.DEFAULT_CONTRACTS)
    except Exception:                     # noqa: BLE001
        pass
    return names


def load(config=None, *, known_tools=None):
    """校验并返回规范化的配置。**任何缺项都在这里报错**。

    报错必须带三样东西：哪个工具、哪个方向、哪个名字。
    只说「配置有误」等于让人自己猜 —— 上游那点做得对，照抄。
    """
    cfg = DEFAULT_RAILS if config is None else config
    if not isinstance(cfg, dict):
        raise RailsConfigError("rails 配置必须是 {tool: {call: [...], result: [...]}}")
    scanners = _builtin_scanners()
    known = set(known_tools) if known_tools is not None else all_known_tools()
    out = {}
    for tool, flows in cfg.items():
        if not isinstance(tool, str) or not tool:
            raise RailsConfigError("工具名必须是非空字符串：%r" % (tool,))
        if known is not None and tool not in known:
            raise RailsConfigError(
                "rails.tool=%s：没有登记这个工具（已知工具：%s）"
                % (tool, "、".join(sorted(known)[:8]) + "…"))
        if not isinstance(flows, dict):
            raise RailsConfigError("rails.%s 必须是 {call: [...], result: [...]}" % tool)
        for flow in flows:
            if flow not in FLOWS:
                raise RailsConfigError(
                    "rails.%s.%s：方向只能是 %s（NeMo 里对应 tool_call / tool_result）"
                    % (tool, flow, " / ".join(FLOWS)))
        norm = {}
        for flow in FLOWS:
            specs = flows.get(flow) or []
            if not isinstance(specs, (list, tuple)):
                raise RailsConfigError("rails.%s.%s 必须是列表" % (tool, flow))
            norm[flow] = [_norm_spec(tool, flow, s, scanners) for s in specs]
        out[tool] = norm
    return out


def _norm_spec(tool, flow, spec, scanners):
    if isinstance(spec, str):
        spec = {"scanner": spec}
    if not isinstance(spec, dict):
        raise RailsConfigError("rails.%s.%s 的条目必须是字符串或对象：%r"
                               % (tool, flow, spec))
    extra = sorted(set(spec) - set(_ALLOWED_SPEC_KEYS))
    if extra:
        raise RailsConfigError(
            "rails.%s.%s：不认识的键 %s（只允许 %s）"
            % (tool, flow, "、".join(extra), "、".join(_ALLOWED_SPEC_KEYS)))
    name = spec.get("scanner")
    if not name:
        raise RailsConfigError("rails.%s.%s：缺少 scanner" % (tool, flow))
    if name not in scanners:
        raise RailsConfigError(
            "rails.%s.%s：没有这个扫描器 %r（可用：%s）"
            % (tool, flow, name, "、".join(sorted(scanners))))
    if "fields" in spec:
        # 上游：$argument= 只对 tool call 有效，tool result 没有参数结构
        if flow != "call":
            raise RailsConfigError(
                "rails.%s.%s：fields 只能用在 call 方向（返回侧没有参数结构）"
                % (tool, flow))
        if not isinstance(spec["fields"], (list, tuple)):
            raise RailsConfigError("rails.%s.%s：fields 必须是列表" % (tool, flow))
        if spec["scanner"] == "schema":
            raise RailsConfigError(
                "rails.%s.%s：schema 检查必须看完整参数，不能只挑字段"
                % (tool, flow))
    sev = spec.get("severity", "warn")
    if sev not in ("info", "warn", "error"):
        raise RailsConfigError("rails.%s.%s：severity 只能是 info/warn/error"
                               % (tool, flow))
    return {"scanner": name, "fields": list(spec.get("fields") or []),
            "severity": sev}


def _run(tool, flow, payload, specs, scanners):
    """跑一个方向的全部检查。**扫描器抛错不许变成 safe。**"""
    verdict = {"tool": tool, "flow": flow, "status": SAFE,
               "ran": [], "findings": [], "blocked": False,
               "reason": "未命中"}
    for spec in specs:
        name = spec["scanner"]
        verdict["ran"].append(name)
        try:
            hits = scanners[name](tool, payload, spec.get("fields") or [])
        except Exception as e:                    # noqa: BLE001
            # 判官坏了 ≠ 判官说安全。上游遇到无法解析的判定也是抛，不是放行。
            verdict.update({"status": ERROR, "blocked": False,
                            "reason": "检查器 %s 执行失败（%s: %s），结论不成立"
                                      % (name, type(e).__name__, e)})
            return verdict
        for code, sev in hits:
            verdict["findings"].append({"scanner": name, "code": code,
                                        "severity": sev})
            # 状态回答「命中没命中」，blocked 回答「要不要停」。
            # 两件事分开：warn 级会命中但不停，error 级才停。
            verdict["status"] = UNSAFE
            if sev == "error" or (spec.get("severity") or "warn") == "error":
                verdict["blocked"] = True
    if verdict["status"] != SAFE:
        verdict["reason"] = "命中 %d 项%s" % (
            len(verdict["findings"]),
            "（含 error 级，调用方必须停）" if verdict["blocked"] else "（仅提示，不停调用）")
    return verdict


def check_call(rules, tool, args):
    """调用前。未声明的工具在跑任何检查**之前**就挡掉（上游 @tool_call_validation）。"""
    if tool not in rules:
        return {"tool": tool, "flow": "call", "status": NOT_DECLARED,
                "ran": [], "findings": [], "blocked": True,
                "reason": "工具 %s 没有在 rails 里声明，检查未执行即拒绝" % tool}
    return _run(tool, "call", args, rules[tool]["call"], _builtin_scanners())


def check_result(rules, tool, payload):
    """返回后。未声明的工具同样先挡（返回侧不能因为没声明就变成不管）。"""
    if tool not in rules:
        return {"tool": tool, "flow": "result", "status": NOT_DECLARED,
                "ran": [], "findings": [], "blocked": True,
                "reason": "工具 %s 没有在 rails 里声明，检查未执行即拒绝" % tool}
    return _run(tool, "result", payload, rules[tool]["result"],
                _builtin_scanners())


def describe(rules):
    """一眼看清「每个工具进和出分别要过什么」—— 这就是能力表的一行。"""
    return [{"tool": t,
             "call": [s["scanner"] for s in v["call"]] or ["-"],
             "result": [s["scanner"] for s in v["result"]] or ["-"]}
            for t, v in sorted(rules.items())]


_CACHE = {"key": None, "rules": None}


def load_default(known_tools=None):
    """进程内缓存一份默认配置。启动时调用一次即可把配置问题暴露出来。"""
    known_tools = known_tools if known_tools is not None else all_known_tools()
    key = tuple(sorted(known_tools))
    if _CACHE["key"] != key:
        _CACHE.update({"key": key, "rules": load(None, known_tools=known_tools)})
    return _CACHE["rules"]


def strict_mode(env=None):
    """受控模式下「未声明工具 / 检查器坏了」按拒绝处理；历史实验只记录。"""
    env = os.environ if env is None else env
    return (env.get("AGENTLAB_EXEC_MODE") or "").strip().lower() in (
        "controlled", "enforce", "strict")
