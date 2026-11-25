#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BLOCK-02：结构血缘评测（离线夹具，干净 clone 可复现）。

夹具在 tests/fixtures/provenance_structural_cases.json（75 条合成结构元数据，
零外部语料依赖、不侵权）。良性分 3 个真实子桶，诚实披露人工确认代价：
  - readonly：只读工具，预期自动放行
  - sensitive_user_trusted：高敏工具但参数来自用户原始输入，预期自动放行=1.0
  - sensitive_tool_derived：查通讯录得邮箱再发信，结构血缘触发 require_approval
文本检测器全关（L1/light/L2），只测 ProvenanceTracker 本身。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import provenance as PV                                     # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures",
                       "provenance_structural_cases.json")


def evaluate_real_slice():
    data = json.load(open(FIXTURE, encoding="utf-8"))
    cases = data["cases"]
    buckets = {}
    for c in cases:
        t = PV.ProvenanceTracker()
        t.record_user_goal(c["goal"])
        t.record_tool_output("untrusted.read", c["tool_output"])
        for v in (c.get("mark_derived") or []):
            t.mark_derived(v)
        d = t.check_call(c["tool"], c["args"])
        b = buckets.setdefault(c["bucket"], {"n": 0, "allow": 0,
                                             "approval": 0, "deny": 0})
        b["n"] += 1
        dec = d["decision"]
        b["allow" if dec == "allow" else
          "approval" if dec == "require_approval" else "deny"] += 1

    a = buckets.get("attack", {"n": 1, "allow": 0, "deny": 0, "approval": 0})
    blocked = a["deny"] + a["approval"]
    ro = buckets.get("readonly", {"n": 1, "allow": 0})
    ut = buckets.get("sensitive_user_trusted", {"n": 1, "allow": 0})
    td = buckets.get("sensitive_tool_derived",
                     {"n": 1, "allow": 0, "approval": 0})

    # 兼容 v1 字段：attacks/benign 仍存在
    n_benign = ro["n"] + ut["n"] + td["n"]
    return {
        "schema_version": "provenance-structural-v3-offline-fixture",
        "text_detectors_enabled": False,
        "fixture": "tests/fixtures/provenance_structural_cases.json",
        "method": ("合成结构元数据夹具，零外部语料依赖；文本检测器全关，"
                   "仅 ProvenanceTracker + CapabilityPolicy。"),
        "n_attack": a["n"], "n_benign": n_benign,
        "attacks": a["n"], "benign": n_benign,
        "blocked_by_provenance": a["deny"],
        "blocked_by_value_cap": a["approval"],
        "structural_asr": round(1 - blocked / max(1, a["n"]), 4),
        # v1 聚合字段（保留兼容）
        "benign_auto_pass_rate": round(
            (ro["allow"] + ut["allow"] + td["allow"]) / max(1, n_benign), 4),
        "benign_approval_required_rate": round(
            (ro["approval"] + ut["approval"] + td["approval"])
            / max(1, n_benign), 4),
        "benign_buckets": {
            "readonly": dict(ro, auto_pass_rate=round(
                ro["allow"] / max(1, ro["n"]), 4)),
            "sensitive_user_trusted": dict(ut, auto_pass_rate=round(
                ut["allow"] / max(1, ut["n"]), 4)),
            "sensitive_tool_derived": dict(td, approval_rate=round(
                td["approval"] / max(1, td["n"]), 4)),
        },
        "note": ("诚实披露：良性高敏工作流里有 %.0f%% 需要人工确认，"
                 "不是零代价；结构血缘只追值来源，不是语义理解。"
                 % (100.0 * td["approval"] / max(1, td["n"]))),
    }


def main():
    rep = evaluate_real_slice()
    if rep["n_attack"] < 50 or rep["n_benign"] < 25:
        print("样本不足：attack=%d benign=%d" % (rep["n_attack"],
                                              rep["n_benign"]), file=sys.stderr)
        sys.exit(2)
    out = os.path.join(ROOT, "experiments", "provenance_structural.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("n_attack=%d n_benign=%d structural_asr=%.3f"
          % (rep["n_attack"], rep["n_benign"], rep["structural_asr"]))
    for k, v in rep["benign_buckets"].items():
        print("  %-26s n=%d %s" % (k, v["n"], v))
    print("产物：%s" % os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
