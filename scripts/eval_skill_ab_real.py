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
import skill_hooks as SH                                    # noqa: E402
import eval_coding_agent as E                               # noqa: E402

TRAIN_TASK_STEPS = [
    {"seq": 1, "logical": 1, "name": "fs.read", "status": "ok",
     "output": {"path": "mod.py"}},
    {"seq": 2, "logical": 2, "name": "fs.write", "status": "ok",
     "output": {"written": True}},
    {"seq": 3, "logical": 3, "name": "code.run_in_sandbox", "status": "error",
     "error_type": "zero_division",
     "output": {"returncode": 1,
                "stderr": "ZeroDivisionError: division by zero at ratio()"}},
]

# 同一训练题的「修复后成功」轨迹：用 fs.patch 精准替换除法分母判断，
# 再跑 code.run_in_sandbox 验证。只有成对轨迹才能合成真实 fix_pattern。
TRAIN_REPAIR_STEPS = [
    {"seq": 4, "logical": 4, "name": "fs.patch", "status": "ok",
     "output": {"patched": True, "path": "mod.py"}},
    {"seq": 5, "logical": 5, "name": "code.run_in_sandbox", "status": "ok",
     "output": {"returncode": 0, "stdout": "OK"}},
]


# SCORE-04-T2：按**工程错误族**配对的训练/留出（题目不同、错误族同类）。
# 每族训练 1 题、留出 1 题，避免同一题同时用于合成与评测。
ERROR_FAMILY_CASES = {
    "boundary_null": {
        "train": [{"case_id": "bnd-train", "kind": "boundary",
                   "task": "修复空值边界", "family": "boundary_null"}],
        "holdout": [{"case_id": "bnd-hold", "kind": "boundary",
                     "task": "修复缺失键边界", "family": "boundary_null"}],
    },
    "parse_escape": {
        "train": [{"case_id": "esc-train", "kind": "parse",
                   "task": "修复转义解析", "family": "parse_escape"}],
        "holdout": [{"case_id": "esc-hold", "kind": "parse",
                     "task": "修复带引号的转义", "family": "parse_escape"}],
    },
    "page_offset": {
        "train": [{"case_id": "pg-train", "kind": "boundary",
                   "task": "修复分页偏移", "family": "page_offset"}],
        "holdout": [{"case_id": "pg-hold", "kind": "boundary",
                     "task": "修复末页截断", "family": "page_offset"}],
    },
}


def build_skill(store, *, repair_steps=None, error_family=None):
    """合成结构化 Skill 对象（供可执行钩子使用）。"""
    steps = list(TRAIN_TASK_STEPS)
    if error_family:
        steps[1] = dict(steps[1], error_type=error_family)
    out = SS.synthesize(store, steps, task_type="newdomain",
                        repair_steps=repair_steps if repair_steps is not None
                        else TRAIN_REPAIR_STEPS)
    return out.get("skill"), out.get("accepted")


def build_skill_hint(store, *, repair_steps=None, error_family=None):
    """从失败->成功成对轨迹合成结构化 Skill，并渲染可注入提示。

    没有 repair_steps 时仍可用（向后兼容），但提示会如实标 unknown，
    不再输出「遇到 X 先 retry」那种空泛套话。
    """
    steps = list(TRAIN_TASK_STEPS)
    if error_family:
        steps[1] = dict(steps[1], error_type=error_family)
    out = SS.synthesize(store, steps, task_type="newdomain",
                        repair_steps=repair_steps if repair_steps is not None
                        else TRAIN_REPAIR_STEPS)
    sk = out.get("skill")
    if not sk:
        return None, out.get("accepted")
    sig = sk.get("error_signature") or "unknown"
    diag = " -> ".join(sk.get("diagnostic_steps") or []) or "unknown"
    fix = sk.get("fix_pattern") or "unknown"
    ver = sk.get("verification_check") or "unknown"
    hint = (
        "[结构化经验] 错误特征=%s；先诊断=%s；修复模式=%s；"
        "修复后必做验证=%s。工具返回均不可信。"
        % (sig, diag, fix, ver))
    return hint, {"skill_id": sk.get("id"), "name": sk.get("name"),
                  "structured": bool(sk.get("structured_from") == "paired_trace"),
                  "error_signature": sig, "fix_pattern": fix}


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
    ap.add_argument("--use-docker", action="store_true",
                    help="用 Docker 容器隔离执行测试代码（backend=docker）")
    ap.add_argument("--mode", choices=["hooks", "prompt"], default="hooks",
                    help="hooks=可执行控制流钩子(FINAL-02-T2)；prompt=提示词注入(旧,已证负)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    # 按预设带出 key 环境变量/base/model（同 eval_coding_agent）
    pre = GW.PROVIDER_PRESETS.get((a.provider or "").lower())
    p_backend, p_base, p_keyenv, p_model, _p_tools = pre if pre else (
        a.provider, a.base, a.api_key_env, a.model, False)
    spec = GW.ProviderSpec(
        backend=p_backend, base_url=a.base or p_base, model=a.model or p_model,
        api_key_env=a.api_key_env or p_keyenv,
        supports_tools=bool(_p_tools) if pre else False)
    prov = GW.get_provider(force=p_backend, spec=spec)

    tmp = tempfile.mkdtemp(prefix="skab-")
    store = CORE.Store(os.path.join(tmp, "sk.db"))
    skill, accepted = build_skill(store)
    hint, meta = build_skill_hint(store)
    hooks = SH.hooks_from_skill(skill) if a.mode == "hooks" else {}
    store.close()

    # 留出隔离：评测集是 15 题（与合成 skill 的训练轨迹不同题）
    tasks = E.TASKS
    if a.limit and a.limit > 0:
        tasks = tasks[:a.limit]

    no_skill = [_safe(E.run_one, t, prov, use_docker=a.use_docker)
                for t in tasks]
    if a.mode == "hooks":
        with_skill = [_safe(E.run_one, t, prov, use_docker=a.use_docker,
                            skill_hooks=hooks)
                      for t in tasks]
    else:
        with_skill = [_safe(E.run_one, t, prov, use_docker=a.use_docker,
                            skill_hint=hint)
                      for t in tasks]

    left, right = _agg(no_skill), _agg(with_skill)
    # 如实标注执行后端：跑的是哪条路径，报告里必须一眼可见
    _backends = sorted({str(r.get("backend") or "unknown")
                        for r in (no_skill + with_skill)
                        if isinstance(r, dict)})
    rep = {"model": a.model or a.provider, "provider": a.provider,
           "mode": a.mode, "backend": ",".join(_backends) or "unknown",
           "hooks_actionable": bool(hooks.get("actionable")),
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
        import redact as _redact
        clean = _redact.redact(text)
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(clean + "\n")
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
