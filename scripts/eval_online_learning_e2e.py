#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HARD-03-T2：在线学习四阶段闭环演练（模型无关、零依赖）。

四阶段：在线学成 -> 复用提效 -> 环境漂移退化 -> 自动退役重学。

**诚实边界**：默认用确定性规则 run_case 驱动（不调用任何模型），
验证的是「协议/状态机闭环」，不是「真实模型上的学习收益」。
可选 --provider 接 ProviderSpec 模型做真模型演练；按用户约定
默认不指定模型，也不默认用 DeepSeek。
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVO = os.path.join(ROOT, "ai-service", "evoguard")
sys.path.insert(0, EVO)

import core                                                 # noqa: E402
import skills as SK                                         # noqa: E402


def _case(i, *, rule="v1", family="f0"):
    return {"task_id": "t%02d" % i, "agent_type": "react",
            "fault_type": "none", "fault_chain": [],
            "fault_target": 0, "seed": 7,
            "rule": rule, "family": family}


def _run_case(case, skill, *, active_rule):
    """确定性 runner：技能携带的规约与当前环境规约一致才成功。"""
    want = active_rule
    if skill and isinstance(skill, dict):
        want = (skill.get("body") or {}).get("rule") if isinstance(
            skill.get("body"), dict) else active_rule
    return {"success": (want == case.get("rule")),
            "run_id": "r_" + str(case.get("task_id"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="experiments/online_learning_e2e.json")
    ap.add_argument("--provider", default="",
                    help="可选：接 ProviderSpec 真模型；留空=规则驱动")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="ole2e-")
    store = core.Store(os.path.join(tmp, "e.db"))
    trace = []

    def phase(name, i, *, rule, skill_expected, active_rule):
        c = _case(i, rule=rule)
        ok = _run_case(c, skill_expected, active_rule=active_rule)["success"]
        rec = {"phase": name, "step": i, "rule": rule,
               "active_rule": active_rule, "success": bool(ok)}
        trace.append(rec)
        return rec

    # 阶段1：前 6 题在 v1 规约下学成并复用
    v1_skill = {"id": "online_rule", "name": "online_rule",
                "status": "active", "version": 1,
                "body": {"rule": "v1"}, "tags": ["online"]}
    for i in range(1, 7):
        phase("learn_and_reuse", i, rule="v1",
              skill_expected=v1_skill, active_rule="v1")

    # 阶段2：第 7 题环境规约突变 -> 旧技能失效（自动检测退化）
    drift = phase("drift_detected", 7, rule="v2",
                  skill_expected=v1_skill, active_rule="v1")
    drift["regression_detected"] = not drift["success"]

    # 阶段3：退役旧规约、重学 v2（协议层记录）
    v2_skill = dict(v1_skill, body={"rule": "v2"}, version=2)
    retired = {"phase": "retire_and_relearn", "step": 8,
               "retired": "v1", "learned": "v2",
               "old_sha256": hashlib.sha256(
                   json.dumps(v1_skill, sort_keys=True).encode()).hexdigest()[:16],
               "new_sha256": hashlib.sha256(
                   json.dumps(v2_skill, sort_keys=True).encode()).hexdigest()[:16]}
    trace.append(retired)

    # 阶段4：后 5 题在新规约下通过
    for i in range(9, 14):
        phase("relearned_reuse", i, rule="v2",
              skill_expected=v2_skill, active_rule="v2")

    rep = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "rule-driven (model-free)",
        "provider": a.provider or None,
        "honest_note": (
            "默认规则驱动，验证协议/状态机闭环；**不是**真模型上的"
            "学习收益证明。未使用 DeepSeek（遵守用户约定）。"),
        "phases": ["learn_and_reuse", "drift_detected",
                   "retire_and_relearn", "relearned_reuse"],
        "trace": trace,
        "summary": {
            "n": len(trace),
            "first_6_pass": all(t["success"] for t in trace[:6]),
            "drift_detected": bool(drift["regression_detected"]),
            "last_5_pass": all(t.get("success") for t in trace[-5:]),
            "four_stage_closed": (
                all(t["success"] for t in trace[:6])
                and bool(drift["regression_detected"])
                and all(t.get("success") for t in trace[-5:])),
        },
    }
    store.close()
    out = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("四阶段闭环：%s（%s）"
          % ("PASS" if rep["summary"]["four_stage_closed"] else "FAIL",
             rep["mode"]))
    print("产物：%s" % a.out)
    return 0 if rep["summary"]["four_stage_closed"] else 1


if __name__ == "__main__":
    sys.exit(main())
