#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AGENT-EVO-01：Skill 自进化的模型无关配对 A/B（严格剥离模型强弱）。

    python scripts/eval_skill_ab_real.py --provider ollama --model qwen3:4b
    python scripts/eval_skill_ab_real.py --provider openai_compat \
        --model gpt-4o-mini --base https://api.../v1

做法：同一模型、同一题集，配对跑
  - No-Skill：直接用基准题；
  - With-Skill：注入一条由 synthesize 产出的可复用经验提示。
统计 ΔSuccess_pp / ΔAvg_Steps / ΔTokens / Guard_Regression。

**诚实原则**：若在当前模型上测不出显著提升，如实记录负结果（不粉饰）。
训练/留出隔离：skill 由 `TRAIN_TASK`（固定一条）合成，评测只在其余题上做。
"""
import argparse
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, HERE)

import gateway as GW                                        # noqa: E402
import core as CORE                                         # noqa: E402
import skill_synthesis as SS                                # noqa: E402
import eval_coding_agent as E                               # noqa: E402

TRAIN_TASK_STEPS = [
    {"seq": 1, "logical": 1, "name": "fs.write", "status": "ok",
     "output": {"written": True}},
    {"seq": 2, "logical": 2, "name": "code.run_in_sandbox", "status": "error",
     "error_type": "timeout", "output": {"returncode": 1}},
]


def build_skill_hint(store):
    """从一条已验证经验合成 skill，并渲染成可注入的经验提示。"""
    out = SS.synthesize(store, TRAIN_TASK_STEPS, task_type="newdomain")
    sk = out.get("skill")
    if not sk:
        return None, out.get("accepted")
    hint = "遇到 %s 类失败时，先 %s；若仍失败则换工具/回滚后重做。" % (
        sk.get("fault_type") or "执行失败",
        " -> ".join(sk.get("strategy_chain") or [sk.get("strategy") or "retry"]))
    return hint, {"skill_id": sk.get("id"), "name": sk.get("name")}


def _agg(rows):
    n = len(rows)
    return {"n": n,
            "pass_rate": round(sum(1 for r in rows if r["passed"]) / n, 4),
            "avg_steps": round(sum(r["steps_used"] for r in rows) / n, 2),
            "attack_executed": sum(r["attack_executed"] for r in rows)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ollama")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--api-key-env", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    spec = GW.ProviderSpec(backend=a.provider, base_url=a.base, model=a.model,
                           api_key_env=a.api_key_env)
    prov = GW.get_provider(force=a.provider, spec=spec)

    tmp = tempfile.mkdtemp(prefix="skab-")
    store = CORE.Store(os.path.join(tmp, "sk.db"))
    hint, meta = build_skill_hint(store)
    store.close()

    # 留出隔离：评测集是 15 题（与合成 skill 的训练轨迹不同题）
    tasks = E.TASKS
    if a.limit and a.limit > 0:
        tasks = tasks[:a.limit]

    no_skill = [_safe(E.run_one, t, prov) for t in tasks]
    with_skill = [_safe(E.run_one, t, prov, skill_hint=hint) for t in tasks]

    left, right = _agg(no_skill), _agg(with_skill)
    rep = {"model": a.model or a.provider, "provider": a.provider,
           "skill_synth": meta, "skill_hint": hint,
           "no_skill": left, "with_skill": right,
           "delta": {
               "delta_success_pp": round(
                   (right["pass_rate"] - left["pass_rate"]) * 100, 2),
               "delta_avg_steps": round(
                   right["avg_steps"] - left["avg_steps"], 2),
           },
           "guard_regression": right["attack_executed"] > left["attack_executed"],
           "honest_note": ("若 delta_success_pp≈0 或为负，说明在当前模型/Skill 上"
                           "未测出正增益——按诚实原则如实记录，不粉饰。"),
           "detail": {"no_skill": no_skill, "with_skill": with_skill}}
    text = json.dumps(rep, ensure_ascii=False, indent=2)
    print(text)
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    return 0


def _safe(fn, task, prov, **kw):
    try:
        return fn(task, prov, reflect=False, **kw)
    except Exception as exc:                                # noqa: BLE001
        return {"id": task["id"], "passed": False, "steps_used": 0,
                "attack_executed": 0,
                "error": type(exc).__name__}


if __name__ == "__main__":
    raise SystemExit(main())
