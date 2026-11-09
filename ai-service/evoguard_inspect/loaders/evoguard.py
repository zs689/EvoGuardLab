# -*- coding: utf-8 -*-
"""把本项目的 SQLite 运行记录导成标准格式 —— 两种用途：

1. **自用**：EvoGuard 自己也成了「被检查的 Agent」，检查器不吃自己的私有格式；
2. **造夹具**：测试和演示需要「已知错误的轨迹」，用这个导出一份 OTLP JSON，
   人为注入错误，看检查器能不能抓出来。不然没法证明规则真的有效。

导出成 OTel 而不是自定义格式，是为了让检查器走的代码路径和检查别人时**完全一样**。
"""
import json

from ..model import KIND_CHAT, KIND_TOOL, Step, Trace

KIND_MAP = {"model": KIND_CHAT, "tool": KIND_TOOL,
            "plan": "other", "context": "other", "replay": KIND_TOOL, "stop": "other"}


def from_store(store, run_id=None):
    """从 core.Store 读 run（可指定），返回 Trace 列表。"""
    with store.conn() as c:
        return _read(c, run_id)


def from_conn(conn, run_id=None):
    """从裸 sqlite3 连接读（不依赖 core.Store，供 --trace xxx.db 直接用）。"""
    return _read(conn, run_id)


def _read(c, run_id=None):
    traces = []
    if True:
        if run_id:
            runs = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchall()
        else:
            # 不设上限：静默截断会让人以为「全查过了」。
            # 库太大时用 run_id 指定单条，或先用 SQL 筛出子集再导。
            runs = c.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        for r in runs:
            r = dict(r)
            exp = c.execute("SELECT * FROM experiments WHERE id=?",
                            (r["experiment_id"],)).fetchone()
            exp = dict(exp) if exp else {}
            steps = c.execute("SELECT * FROM steps WHERE run_id=? ORDER BY seq",
                              (r["id"],)).fetchall()
            t = Trace(trace_id=r["id"],
                      agent=str(exp.get("agent_type") or "unknown"),
                      task=str(exp.get("task_id") or ""),
                      answer=_clean_answer(r.get("answer")),
                      source="evoguard",
                      meta={"mode": r.get("mode"), "status": r.get("status"),
                            "fault": exp.get("fault_type")})
            for s in steps:
                s = dict(s)
                kind = KIND_MAP.get(s.get("kind"), "other")
                t.steps.append(Step(
                    seq=s["seq"], kind=kind, name=str(s.get("name") or ""),
                    tool=(s.get("name") if kind == KIND_TOOL else None),
                    args=_loads(s.get("input")), result=_loads(s.get("output")),
                    status=("error" if s.get("error_type") else str(s.get("status") or "ok")),
                    error=s.get("error_type"), tokens=int(s.get("tokens") or 0),
                    ts=s.get("ts"), duration_ms=int(s.get("duration_ms") or 0)))
            traces.append(t)
    return traces


def _clean_answer(v):
    """把「空壳答案」归一成 None。

    运行库里的 answer 常是 {"parsed": null, "text": ""} 这种外壳，
    直接当答案会让「结果未被引用」这类规则误报（外壳非空，但里面什么都没有）。
    """
    a = _loads(v)
    # 必须**真的存在** parsed/text 键才算外壳。
    # 否则 {"order_id": "A1001", "total": 504.0} 这种正常答案会被当成空壳丢掉
    # —— 实测导致 32 条正常轨迹全被判「没有答案」。
    if isinstance(a, dict) and "parsed" in a:
        if a.get("parsed") is None and not str(a.get("text") or "").strip():
            return None
    return a


def _loads(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def _empty_shell(v):
    if not isinstance(v, dict):
        return False
    if set(v) - {"parsed", "text", "answer", "output", "result", "outputs"}:
        return False
    return v.get("parsed") is None and not str(v.get("text") or "").strip()


def to_otel(traces, agent_name=None):
    """统一 Trace 列表 → OTLP JSON（resourceSpans 结构）。"""
    out_spans = []
    for t in traces:
        for s in t.steps:
            attrs = [{"key": "gen_ai.agent.name", "value": {"stringValue": t.agent}},
                     {"key": "gen_ai.task", "value": {"stringValue": t.task}}]
            if s.kind == KIND_TOOL:
                attrs += [
                    {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                    {"key": "gen_ai.tool.name", "value": {"stringValue": s.tool or s.name}},
                    {"key": "gen_ai.tool.call.arguments",
                     "value": {"stringValue": json.dumps(s.args, ensure_ascii=False)}},
                    {"key": "gen_ai.tool.call.result",
                     "value": {"stringValue": json.dumps(s.result, ensure_ascii=False)}},
                ]
                name = "execute_tool %s" % (s.tool or s.name)
            elif s.kind == KIND_CHAT:
                # 模型步的结果可能是空壳（{"parsed": null, "text": ""}）：
                # mock 后端不产出文本，真正的答案在 trace.answer 里。
                # 导出时若照抄空壳，检查器就永远看不到答案 —— 会把每条正常轨迹
                # 都判成「宣称完成但没有答案」（实测 32/32 误报）。
                completion = s.result
                if _empty_shell(completion) and t.answer is not None:
                    completion = t.answer
                attrs += [
                    {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": s.tokens}},
                    {"key": "gen_ai.completion",
                     "value": {"stringValue": json.dumps(completion, ensure_ascii=False)}},
                ]
                name = "chat %s" % (t.agent or "agent")
            else:
                attrs += [{"key": "gen_ai.operation.name", "value": {"stringValue": "evoguard." + s.name}}]
                name = s.name or "step"
            if s.error:
                attrs.append({"key": "error.type", "value": {"stringValue": str(s.error)}})
            out_spans.append({
                "traceId": t.trace_id,
                "spanId": "%s-%02d" % (t.trace_id[-8:], s.seq),
                "name": name,
                "startTimeUnixNano": str((s.seq or 0) * 1000000),
                "attributes": attrs,
                "status": {"code": "STATUS_CODE_ERROR" if s.error else "STATUS_CODE_OK"},
            })
    return {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": agent_name or "evoguard"}}]},
        "scopeSpans": [{"scope": {"name": "evoguard.export"}, "spans": out_spans}],
    }]}


def dump_otel(traces, path, agent_name=None):
    doc = to_otel(traces, agent_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return path
