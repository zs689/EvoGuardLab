# -*- coding: utf-8 -*-
"""外部轨迹 / 评测集适配器：把别的框架的 trace 转成本项目能审的格式。

外部审查 REF-ORG-001 指出：`EvoGuardLab` 没有对接 `github/` 里那些参考
实现（LangSmith、OpenAI function calling、AgentDojo 风格）的 Adapter，
所以「跨框架对照」只能各写一套脚本。

这里做三件事：
1. `normalize(obj)` —— 认几种常见轨迹格式，统一成
   `{"steps":[{seq,kind,name,input,output,status}], "source":..., "warnings":[]}`；
2. `to_guard_input(steps)` —— 抽出所有**工具返回文本**，直接喂给
   `agent_watch.GuardGateway.inspect_tool_output` 做间接注入扫描；
3. `to_probe_request(steps)` —— 抽出「一次请求的 prompt + 工具输出」，
   供 `agent_watch.runtime_probe` 复用。

**不猜格式**：认不出的字段进 `warnings`，不静默丢弃；未知来源明确标
`source="unknown"`，不假装认出来了。
"""
import json


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def _step(seq, kind, name, inp, out, status="ok"):
    return {"seq": seq, "kind": kind, "name": name,
            "input": inp, "output": out, "status": status}


def _from_langsmith(run, steps, depth=0):
    """LangSmith run 树：run_type=llm|tool|chain，子节点在 child_runs。"""
    rt = str(run.get("run_type") or run.get("type") or "chain")
    name = str(run.get("name") or rt)
    kind = {"llm": "llm", "tool": "tool", "chain": "plan"}.get(rt, "plan")
    steps.append(_step(len(steps) + 1, kind, name,
                       run.get("inputs"), run.get("outputs"),
                       "error" if run.get("error") else "ok"))
    for child in _as_list(run.get("child_runs") or run.get("children")):
        if isinstance(child, dict) and depth < 20:
            _from_langsmith(child, steps, depth + 1)


def _from_openai(messages, steps):
    """OpenAI 风格：assistant.tool_calls + role=tool 的返回。"""
    calls = {}
    for m in _as_list(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in _as_list(m.get("tool_calls")):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                cid = tc.get("id") or fn.get("name")
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                steps.append(_step(len(steps) + 1, "llm", "assistant",
                                   None, _text(m.get("content"))))
                calls[cid] = {"tool": fn.get("name"), "args": args}
                steps.append(_step(len(steps) + 1, "plan", fn.get("name"),
                                   args, None))
        elif role == "tool":
            cid = m.get("tool_call_id")
            meta = calls.get(cid) or {}
            steps.append(_step(len(steps) + 1, "tool",
                               str(meta.get("tool") or m.get("name") or "tool"),
                               meta.get("args"), m.get("content")))
        elif role in ("user", "system"):
            steps.append(_step(len(steps) + 1, "plan", role,
                               m.get("content"), None))


def normalize(obj):
    """任意轨迹 → 统一结构。认不出就如实标 warnings。"""
    warnings = []
    steps = []

    if isinstance(obj, dict) and isinstance(obj.get("steps"), list):
        for i, s in enumerate(obj["steps"], 1):
            if not isinstance(s, dict):
                warnings.append("steps[%d] 不是对象，已跳过" % i)
                continue
            steps.append(_step(i, s.get("kind") or "tool",
                               s.get("name") or s.get("tool") or "",
                               s.get("input"), s.get("output"),
                               s.get("status") or "ok"))
        source = "evoguard"
    elif isinstance(obj, dict) and isinstance(obj.get("messages"), list):
        _from_openai(obj["messages"], steps)
        source = "openai"
    elif isinstance(obj, dict) and (
            obj.get("run_type") or obj.get("child_runs") or obj.get("type")):
        _from_langsmith(obj, steps)
        source = "langsmith"
    elif isinstance(obj, list):
        for item in obj:
            sub = normalize(item)
            steps.extend(dict(s, seq=len(steps) + 1) for s in sub["steps"])
            warnings.extend(sub["warnings"])
        source = "list"
    else:
        source = "unknown"
        warnings.append("无法识别的轨迹格式：顶层类型 %s"
                        % type(obj).__name__)
    return {"version": 1, "source": source, "steps": steps,
            "warnings": warnings}


def to_guard_input(normalized):
    """抽出所有工具返回文本，供 GuardGateway.inspect_tool_output 扫描。"""
    out = []
    for s in normalized.get("steps") or []:
        if s.get("kind") == "tool":
            txt = _text(s.get("output"))
            if txt:
                out.append({"tool": s.get("name") or "", "text": txt})
    return out


def to_probe_request(normalized):
    """抽出一次 probe 用的 request：第一个用户/规划输入作 prompt。"""
    prompt = ""
    for s in normalized.get("steps") or []:
        if s.get("kind") == "plan" and s.get("name") in ("user", "system"):
            prompt = _text(s.get("input"))
            break
    if not prompt:
        for s in normalized.get("steps") or []:
            if s.get("input"):
                prompt = _text(s["input"])
                break
    return {"prompt": prompt,
            "tool_outputs": [x["text"] for x in to_guard_input(normalized)],
            "context": {"source": normalized.get("source")}}


def scan_trace(obj, gateway=None):
    """一步到位：任意轨迹 → 间接注入扫描结果。

    返回 {"source","steps","hits":[{tool,allowed,codes}],"warnings"}。
    """
    try:
        from . import agent_watch as _aw
    except ImportError:
        import agent_watch as _aw                          # type: ignore
    g = gateway or _aw.GuardGateway(enforce=True)
    norm = normalize(obj)
    hits = []
    for item in to_guard_input(norm):
        d = g.inspect_tool_output(item["tool"], item["text"])
        hits.append({"tool": item["tool"], "allowed": bool(d.allowed),
                     "codes": [f["code"] for f in d.findings]})
    return {"source": norm["source"], "steps": len(norm["steps"]),
            "hits": hits, "warnings": norm["warnings"],
            "blocked": sum(1 for h in hits if not h["allowed"])}
