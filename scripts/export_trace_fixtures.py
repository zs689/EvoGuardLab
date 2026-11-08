# -*- coding: utf-8 -*-
"""从本项目运行记录导出「标准格式」轨迹夹具。

为什么要这一步：检查器要能证明「真的抓得住问题、且不冤枉干净轨迹」。
自己造 JSON 容易造得比现实干净（正好避开规则的盲区），
所以夹具直接从真实 run 导，再挑类型。

产出（tests/fixtures/）：
    trace_clean.json         正常成功的轨迹（检查器应当零命中）
    trace_hallucination.json 工具输出幻觉（应命中 hallucinated_number）
    trace_swallowed.json     工具报错后仍给答案（应命中 error_swallowed）

跑法（在 EvoGuardLab 目录下，先跑过 real_model_bench）：
    python scripts/export_trace_fixtures.py
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect.loaders import evoguard as eg  # noqa: E402
from evoguard_inspect.model import KIND_CHAT, KIND_TOOL, Step, Trace  # noqa: E402

DB = os.path.join(ROOT, "data", "real_model.db")
OUT = os.path.join(ROOT, "tests", "fixtures")

KIND_MAP = {"model": KIND_CHAT, "tool": KIND_TOOL,
            "plan": "other", "context": "other", "replay": KIND_TOOL, "stop": "other"}


def loads(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def pick(c, where, args=()):
    q = """SELECT u.id FROM runs u JOIN experiments e ON e.id=u.experiment_id WHERE %s
           ORDER BY u.started_at DESC LIMIT 1""" % where
    r = c.execute(q, args).fetchone()
    return r[0] if r else None


def build(c, run_id):
    r = dict(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    e = dict(c.execute("SELECT * FROM experiments WHERE id=?",
                       (r["experiment_id"],)).fetchone())
    t = Trace(trace_id=r["id"], agent=str(e.get("agent_type") or "unknown"),
              task=str(e.get("task_id") or ""), answer=loads(r.get("answer")),
              source="evoguard",
              meta={"mode": r.get("mode"), "fault": e.get("fault_type")})
    for s in c.execute("SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,)):
        s = dict(s)
        kind = KIND_MAP.get(s.get("kind"), "other")
        t.steps.append(Step(
            seq=s["seq"], kind=kind, name=str(s.get("name") or ""),
            tool=(s.get("name") if kind == KIND_TOOL else None),
            args=loads(s.get("input")), result=loads(s.get("output")),
            status=("error" if s.get("error_type") else str(s.get("status") or "ok")),
            error=s.get("error_type"), tokens=int(s.get("tokens") or 0)))
    return t


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not os.path.exists(DB):
        print("[×] 没有 %s，先跑一次 real_model_bench.py" % DB)
        return 1
    os.makedirs(OUT, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    targets = [
        ("trace_clean.json",
         "e.fault_type='none' AND u.success=1 AND u.mode='A'", ()),
        ("trace_hallucination.json",
         "e.fault_type='tool_hallucination' AND u.mode='A'", ()),
        # 负例：工具报错后**重试成功**。这种不应该报 error_swallowed ——
        # 规则必须能区分「吞掉错误」和「正确处理了错误」。
        ("trace_retry_ok.json",
         "e.fault_type='tool_timeout' AND u.mode='B' AND u.success=1 "
         "AND u.answer IS NOT NULL AND u.answer NOT IN ('null','')", ()),
    ]

    made = []
    for name, where, args in targets:
        rid = pick(c, where, args)
        if not rid:
            print("[!] 没有匹配的 run：%s" % where)
            continue
        t = build(c, rid)
        path = os.path.join(OUT, name)
        eg.dump_otel([t], path, agent_name="evoguard-%s" % t.agent)
        made.append((name, t.trace_id, t.agent, t.task, len(t.steps)))

    print("== 导出完成 ==")
    for name, tid, agent, task, n in made:
        print("  %-28s %s  agent=%s task=%s steps=%d" % (name, tid, agent, task, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
