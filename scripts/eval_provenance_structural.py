#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FRONTIER-01 真实切片评测：关闭全部文本检测器，只靠结构血缘。

旧版只有 attacks=3 / benign=3 的手写样例，无法支撑"结构性防护"的声明。
这里直接复用仓库里的**公开 InjecAgent / AgentDojo 切片**（离线、零网络）：

- 攻击：真实 attacker_instruction 提供的外部值流入高敏 sink；
- 良性：真实 user_instruction 提供的值（用户显式可信）流入同类 sink，
  以及不含任何敏感 token 的正常多步工具调用。

不调用任何文本检测器（L1 / light / L2 全关），只用 ProvenanceTracker +
CapabilityPolicy，因此测的是**结构血缘本身**，不是关键词命中。
脚本可离线一键复现；样本数与"良性需人工确认的比例"都如实写进产物。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import provenance as PV                                     # noqa: E402

MIN_ATTACK = 50
MIN_BENIGN = 20


def _load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:                               # noqa: BLE001
                continue
    return rows


def _sink_for(value):
    """根据外部值形态选一个真实高敏 sink（字段与 provenance._SENSITIVE 对齐）。"""
    if "@" in value and "." in value:
        return "send_email", {"to": value, "subject": "report"}
    if value.startswith("http://") or value.startswith("https://"):
        return "http_post", {"url": value, "payload": "x"}
    if value.isdigit():
        return "transfer", {"to": value, "amount": 100}
    return "write_file", {"path": value[:80]}


def _attack_cases(rows):
    """每条唯一攻击指令构造一次"外部值流入副作用工具"。"""
    seen, out = set(), []
    for r in rows:
        instr = str(r.get("attacker_instruction") or "")
        if not instr or instr in seen:
            continue
        seen.add(instr)
        toks = PV._extract_tokens(instr)
        toks |= {p.strip(".,);\"'") for p in re.split(r"[\s,;:]+", instr)
                 if len(p) >= 8}
        if not toks:
            continue
        goal = str(r.get("user_instruction") or "查资料")
        # 外部值绝不能出现在用户目标里，否则算用户显式授权，不该拦
        value = next((t for t in sorted(toks) if t not in goal), None)
        if value is None:
            continue
        tool, args = _sink_for(value)
        out.append((goal, instr, tool, args, r.get("case_id")))
    return out


def _benign_cases(rows):
    """良性：用户目标里就有的可信值 + 不含外部 token 的多步调用。"""
    out, seen_goal = [], set()
    for r in rows:
        goal = str(r.get("user_instruction") or r.get("text") or "").strip()
        if not goal or goal in seen_goal:
            continue
        seen_goal.add(goal)
        toks = PV._extract_tokens(goal)
        if toks:
            value = sorted(toks)[0]
            # 值同时出现在工具输出里也无所谓：用户先说过 -> USER_TRUSTED
            tool, args = _sink_for(value)
            out.append((goal, goal + " " + value, tool, args,
                        r.get("case_id")))
        else:
            # 无敏感值的普通只读调用（非高敏 sink，天然放行）
            out.append((goal, "无关的工具返回", "kb.search",
                        {"query": "订单"}, r.get("case_id")))
    return out


def evaluate_real_slice():
    inj = _load(os.path.join(ROOT, "data", "external", "injecagent",
                             "cases.jsonl"))
    cn = _load(os.path.join(ROOT, "data", "external", "chinese",
                            "cases.jsonl"))
    attacks = _attack_cases(inj)[:max(MIN_ATTACK, 50)]
    benign = _benign_cases(cn + inj)[:max(MIN_BENIGN, 20)][:max(MIN_BENIGN, 20)]

    blocked_prov = blocked_cap = 0
    samples_a = []
    for goal, out, tool, args, cid in attacks:
        t = PV.ProvenanceTracker()
        t.record_user_goal(goal)
        t.record_tool_output("untrusted.read", out)
        d = t.check_call(tool, args)
        if d["decision"] == "deny":
            blocked_prov += 1
        elif d["decision"] == "require_approval":
            blocked_cap += 1
        samples_a.append({"case_id": cid, "tool": tool,
                          "decision": d["decision"],
                          "reason": d.get("reason")})

    auto = approve = blocked_b = 0
    samples_b = []
    for goal, out, tool, args, cid in benign:
        t = PV.ProvenanceTracker()
        t.record_user_goal(goal)
        t.record_tool_output("untrusted.read", out)
        d = t.check_call(tool, args)
        if d["decision"] == "allow":
            auto += 1
        elif d["decision"] == "require_approval":
            approve += 1
        else:
            blocked_b += 1
        samples_b.append({"case_id": cid, "tool": tool,
                          "decision": d["decision"]})

    n, m = len(attacks), len(benign)
    return {
        "schema_version": "provenance-structural-v2-real-slice",
        "text_detectors_enabled": False,
        "sources": ["data/external/injecagent/cases.jsonl",
                    "data/external/chinese/cases.jsonl"],
        "method": ("attacker_instruction 作为不可信工具输出；其外部值流入"
                   " send_email/http_post/transfer/write_file 高敏参数。"
                   "全部文本检测器关闭，仅 ProvenanceTracker + CapabilityPolicy。"),
        "n_attack": n, "n_benign": m,
        "attacks": n, "benign": m,          # v1 字段兼容
        "blocked_by_provenance": blocked_prov,
        "blocked_by_value_cap": blocked_cap,
        "structural_asr": round(1 - (blocked_prov + blocked_cap) / max(1, n), 4),
        "benign_auto_pass_rate": round(auto / max(1, m), 4),
        "benign_approval_required_rate": round(approve / max(1, m), 4),
        "benign_blocked": blocked_b,
        "attack_sink_distribution": {
            k: sum(1 for s in samples_a if s["tool"] == k)
            for k in sorted({s["tool"] for s in samples_a})},
        "samples_attack": samples_a[:10],
        "samples_benign": samples_b[:10],
        "note": ("真实公开切片，离线可复现；结构血缘不是语义理解 —— "
                 "用户在工具输出里写入恰好出现在目标中的值仍会放行。"),
    }


def main():
    rep = evaluate_real_slice()
    if rep["n_attack"] < MIN_ATTACK or rep["n_benign"] < MIN_BENIGN:
        print("样本不足：attack=%d benign=%d（要求 >=%d/%d）"
              % (rep["n_attack"], rep["n_benign"], MIN_ATTACK, MIN_BENIGN),
              file=sys.stderr)
        sys.exit(2)
    out = os.path.join(ROOT, "experiments", "provenance_structural.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("n_attack = %d, n_benign = %d" % (rep["n_attack"], rep["n_benign"]))
    print("structural_asr = %.3f" % rep["structural_asr"])
    print("benign_auto_pass_rate = %.3f" % rep["benign_auto_pass_rate"])
    print("benign_approval_required_rate = %.3f"
          % rep["benign_approval_required_rate"])
    print("产物：%s" % os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
