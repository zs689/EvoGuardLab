# -*- coding: utf-8 -*-
"""OTel GenAI 语义约定 → 统一 Trace。

为什么选这个格式当第一优先：它是目前唯一被多个框架同时采纳的事实标准
（LangChain / LlamaIndex / OpenAI Agents SDK / LangSmith / Langfuse 都能导出或转换）。
接住它，等于接住大半个生态 —— 比给每个框架写一个适配器现实得多。

支持的 span 属性（2025 版 GenAI semconv）：
    gen_ai.operation.name      "chat" | "execute_tool"
    gen_ai.agent.name          可选，缺省用 resource attribute
    gen_ai.tool.name           工具名
    gen_ai.tool.call.arguments JSON 字符串
    gen_ai.tool.call.result    JSON 字符串
    gen_ai.usage.input_tokens / output_tokens

容错原则：认不出的 span 归为 other，**不丢**（丢掉会让后续规则少证据）。
"""
import json

from ..model import KIND_CHAT, KIND_TOOL, Step, Trace


def _attr(d, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def _unjson(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _spans_of(doc):
    """展开 OTLP JSON：resourceSpans -> scopeSpans -> spans。"""
    for rs in doc.get("resourceSpans") or []:
        res_attrs = {}
        for a in ((rs.get("resource") or {}).get("attributes") or []):
            res_attrs[a.get("key")] = _val(a)
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans") or []:
                yield sp, res_attrs


def _val(attr):
    v = attr.get("value") or {}
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    if "arrayValue" in v:
        return [_val({"value": x}) for x in (v["arrayValue"].get("values") or [])]
    return None


def _attrs(sp):
    out = {}
    for a in sp.get("attributes") or []:
        out[a.get("key")] = _val(a)
    return out


def _is_empty_shell(v):
    """{"parsed": null, "text": ""} 这种外壳不算答案。

    不处理会让「结果未被引用」这类规则误报：外壳是非空 dict，里面却什么都没有。
    """
    if not isinstance(v, dict):
        return False
    if set(v) - {"parsed", "text", "answer", "output", "result", "outputs"}:
        return False
    inner = v.get("parsed")
    if inner is not None:
        return False
    for k in ("text", "answer"):
        val = v.get(k)
        if isinstance(val, str) and val.strip():
            return False
        if isinstance(val, (dict, list)) and val:
            return False
    return True


def unwrap_answer(v):
    """把「模型输出」归一化成答案本体。

    很多框架会把答案包一层：{"parsed": {...}, "text": "..."} 或
    {"outputs": {...}} 或 {"generations": [...]}。不拆开的话，
    规则拿到的就不是答案本身，而是外壳 —— 数字规则会直接失效。
    """
    v = _unjson(v)
    if isinstance(v, dict):
        for k in ("parsed", "answer", "output", "outputs", "result"):
            inner = v.get(k)
            if isinstance(inner, dict):
                return inner
    return None if _is_empty_shell(v) else v


def load(doc):
    """doc 是 OTLP JSON（dict）。返回 Trace 列表，按 traceId 分组。"""
    traces = {}
    for sp, res_attrs in _spans_of(doc):
        a = dict(res_attrs)
        a.update(_attrs(sp))
        tid = sp.get("traceId") or sp.get("spanId") or "unknown"
        t = traces.get(tid)
        if t is None:
            agent = _attr(a, "gen_ai.agent.name", "service.name", default="unknown")
            t = Trace(trace_id=tid, agent=str(agent),
                      task=str(_attr(a, "gen_ai.task", default="")),
                      source="otel")
            traces[tid] = t

        op = str(_attr(a, "gen_ai.operation.name", default=""))
        name = sp.get("name") or ""
        if op == "execute_tool" or "execute_tool" in name:
            kind, tool = KIND_TOOL, str(_attr(a, "gen_ai.tool.name", default=name))
            args = _unjson(_attr(a, "gen_ai.tool.call.arguments", default=None))
            result = _unjson(_attr(a, "gen_ai.tool.call.result", default=None))
        elif op == "chat" or name.startswith("chat"):
            kind, tool = KIND_CHAT, None
            args, result = None, _attr(a, "gen_ai.completion", default=None)
            result = _unjson(result)
        else:
            kind, tool, args, result = "other", None, None, None

        status = str((sp.get("status") or {}).get("code", "")).upper()
        err = None
        # OTLP 的 status.code 有四种写法：0/UNSET、1/OK、2/ERROR、STATUS_CODE_ERROR。
        # 只判 == "ERROR" 会漏掉带前缀的那版（踩过：合成的错误 span 一条都没被识别）。
        if "ERROR" in status or status == "2":
            err = str(_attr(a, "error.type", "exception.message", default="error"))
        start = sp.get("startTimeUnixNano")
        try:
            ts = int(start) / 1e9 if start else None
        except Exception:
            ts = None
        # 耗时：OTLP 的 endTimeUnixNano - startTimeUnixNano。
        # 缺 end 就记 0（不当成「很快」）—— 可运行性统计要能区分「没记录」和「真快」。
        dur_ms = 0
        try:
            end = sp.get("endTimeUnixNano")
            if start and end:
                dur_ms = max(0, int((int(end) - int(start)) / 1e6))
        except Exception:
            dur_ms = 0

        t.steps.append(Step(
            seq=len(t.steps) + 1, kind=kind, name=name, tool=tool,
            args=args, result=result,
            status="error" if err else "ok", error=err,
            tokens=int(_attr(a, "gen_ai.usage.output_tokens",
                             "gen_ai.usage.completion_tokens", default=0) or 0),
            ts=ts, duration_ms=dur_ms,
            raw={"span": sp.get("spanId"), "op": op}))

    for t in traces.values():
        t.steps.sort(key=lambda s: (s.ts if s.ts is not None else 0, s.seq))
        for i, s in enumerate(t.steps, 1):
            s.seq = i
        chats = t.chats()
        if chats:
            t.answer = unwrap_answer(chats[-1].result)
    return list(traces.values())
