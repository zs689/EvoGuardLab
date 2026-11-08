# -*- coding: utf-8 -*-
"""LangSmith 导出（runs 列表）→ 统一 Trace。

LangSmith 的导出是扁平 runs 数组：每个 run 有 run_type（llm / tool / chain）、
inputs / outputs、parent_run_id。这里只取 llm 和 tool 两类，
chain 只用来确定 trace 归属。

为什么还要它：很多团队没有 OTel，但有 LangSmith。两个 loader 加起来，
覆盖面比只做 OTel 大一圈，而规则层不用改。
"""
import json

from ..model import KIND_CHAT, KIND_TOOL, Step, Trace


def _out(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _ts(r, *names):
    for n in names:
        v = r.get(n)
        if v:
            return v
    return None


def load(doc):
    """doc：{"runs": [...]} 或直接是 list。"""
    runs = doc.get("runs") if isinstance(doc, dict) else doc
    if not isinstance(runs, list):
        return []

    groups = {}
    for r in runs:
        tid = r.get("trace_id") or r.get("session_id") or r.get("id") or "unknown"
        groups.setdefault(tid, []).append(r)

    traces = []
    for tid, rs in groups.items():
        rs = sorted(rs, key=lambda x: str(_ts(x, "start_time", "startTime") or ""))
        name = rs[0].get("name") or "unknown"
        t = Trace(trace_id=tid, agent=str(name), task="", source="langsmith")
        for i, r in enumerate(rs, 1):
            rtype = (r.get("run_type") or "").lower()
            if rtype == "llm":
                kind, tool = KIND_CHAT, None
                result = _out(r.get("outputs"))
                args = _out(r.get("inputs"))
            elif rtype == "tool":
                kind, tool = KIND_TOOL, str(r.get("name") or "tool")
                result = _out(r.get("outputs"))
                args = _out(r.get("inputs"))
            else:
                continue
            err = r.get("error")
            t.steps.append(Step(
                seq=i, kind=kind, name=str(r.get("name") or ""), tool=tool,
                args=args, result=result,
                status="error" if err else "ok", error=str(err) if err else None,
                tokens=int((r.get("total_tokens") or 0) or 0),
                ts=None, raw={"id": r.get("id")}))
        chats = t.chats()
        if chats:
            last = chats[-1].result
            if isinstance(last, dict):
                # LangSmith 的 llm outputs 常是 {"outputs": {...}} 或 {"generations": [...]}
                t.answer = last.get("outputs", last)
        if t.steps:
            traces.append(t)
    return traces
