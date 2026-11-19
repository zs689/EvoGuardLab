#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AGENT-EVAL-02-T1：公开权威基准适配器（模型无关）。

    python scripts/eval_public_benchmarks.py --out experiments/pub_bench.json
    python scripts/eval_public_benchmarks.py --provider ollama --model qwen3:4b --limit 200

数据来源（如实标注）：
  - InjecAgent（公开，2108 条）：attacker_instruction + attacker_tools；
  - AgentDojo 切片（公开，120 条）：goal 为攻击意图。

两个核心指标（避免「一律 deny 伪高分」）：
  - ASR（Attack Success Rate）：**两层都没拦住**的攻击比例 = 真实 executed>0 上界；
      第 1 层：分层检测（L1 light_classifier -> 可选语义判官）；
      第 2 层：工具门禁（未知/危险工具在 execution.preflight 必被拒）。
  - Utility under Attack：良性请求**未被误拦**的比例（攻击下正常任务仍可完成）。

诚实边界：本脚本用「检测 + 门禁」两层判定攻击是否可能落地，不启动真实
业务执行器（InjecAgent 的攻击工具在本系统中未注册，无法真执行）。因此 ASR
是**可利用性上界**，不是真实业务损失率；这一口径写进输出，避免误读。
"""
import argparse
import collections
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, HERE)

import gateway as GW                                        # noqa: E402
import light_classifier as LC                               # noqa: E402
import harmful_request_judge as HJ                          # noqa: E402
import tool_contract as TC                                  # noqa: E402
import execution as EX                                      # noqa: E402
import run_semantic_injection_v3 as V                       # noqa: E402

DATA = os.path.join(ROOT, "data", "external")


def _read_jsonl(path):
    out = []
    if not os.path.isfile(path):
        return out
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def detect_layered(text, *, judge=None):
    """分层检测：返回 (blocked, layer, decision)。

    层 1：`security.scan` 正则 + action_scope（v3 快速路径，毫秒级纯规则）；
    层 2：语义判官（仅在层 1 未命中且显式提供 judge 时调用，模型无关）。
    任一层命中即拦。规则-only 模式不调任何模型，可全量跑。
    """
    try:
        hit, _codes, _ms = V.detect(text, provider=None)
        if hit:
            return True, "L1_light", "block"
    except Exception:                                       # noqa: BLE001
        pass
    if judge is not None:
        try:
            sm = HJ.guard_input_semantic(text, judge=judge)
            if sm.get("decision") == "block":
                return True, "L2_semantic", "block"
        except Exception:                                   # noqa: BLE001
            pass
    return False, "none", "allow"


def tool_gate_blocks(tools):
    """第 2 层：攻击工具在受控门禁下能否放行。

    返回 True 表示**会被门禁拦住**（未知/危险工具）。我们以「工具是否在
    DEFAULT_CONTRACTS 里被登记为可读」为放行判据：未登记 -> 门禁必拒。
    """
    declared = TC.DEFAULT_CONTRACTS
    for t in (tools or []):
        name = t if isinstance(t, str) else (t[0] if t else "")
        name = str(name)
        c = declared.get(name)
        if c is None:
            return True                                     # 未知工具 -> 拒
        if c.get("side_effect") in ("write", "external", "arbitrary"):
            return True                                     # 危险工具 -> 拒
    return False


def _eval_set(name, rows, *, judge, limit=0):
    if limit and limit > 0:
        rows = rows[:limit]
    hits = {"L1_light": 0, "L2_semantic": 0, "tool_gate": 0, "missed": 0}
    lat = []
    for r in rows:
        text = r.get("text") or r.get("attacker_instruction") or r.get("goal") or ""
        t0 = time.perf_counter()
        blocked, layer, _dec = detect_layered(text, judge=judge)
        lat.append((time.perf_counter() - t0) * 1000)
        if blocked:
            hits[layer] += 1
            continue
        # 第 1 层没拦 -> 看第 2 层工具门禁
        if tool_gate_blocks(r.get("attacker_tools")):
            hits["tool_gate"] += 1
        else:
            hits["missed"] += 1
    n = len(rows)
    missed = hits["missed"]
    return {
        "n": n,
        "blocked_by": dict(hits),
        "asr": round(missed / n, 4) if n else None,
        "p95_ms": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 3) if lat else 0,
    }


def _is_benign(r):
    v = r.get("label", 1)
    if isinstance(v, str):
        return v.strip().lower() in ("benign", "0", "safe", "normal")
    return int(v) == 0


def _utility(rows, *, judge):
    """Utility under Attack：良性（负样本）请求未被误拦的比例。"""
    benign = [r for r in rows if _is_benign(r)]
    if not benign:
        return None, 0
    passed = 0
    for r in benign:
        blocked, _layer, _d = detect_layered(r.get("text") or "", judge=judge)
        if not blocked:
            passed += 1
    return round(passed / len(benign), 4), len(benign)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--api-key-env", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    judge = None
    if a.provider:
        spec = GW.ProviderSpec(backend=a.provider, base_url=a.base,
                               model=a.model, api_key_env=a.api_key_env)
        prov = GW.get_provider(force=a.provider, spec=spec)

        def judge(messages):                                # 判官适配：单轮
            text, _t, _m = prov.complete(
                messages if isinstance(messages, list)
                else [{"role": "user", "content": str(messages)}],
                temperature=0.0, max_tokens=200)
            return text or ""

    rep = {"version": 1, "note":
           "ASR 为「检测+门禁两层都未拦」的可利用性上界，非真实业务损失率；"
           "InjecAgent/AgentDojo 的攻击工具在本系统未注册，无法真执行。",
           "judge": (a.provider + "/" + str(a.model)) if a.provider else "rule-only",
           "sets": {}}

    inj = _read_jsonl(os.path.join(DATA, "injecagent", "cases.jsonl"))
    if inj:
        rows = [{"text": r.get("attacker_instruction") or "",
                 "label": 1, "attacker_tools": r.get("attacker_tools"),
                 "slice": str(r.get("category") or "na")} for r in inj]
        rep["sets"]["injecagent_public"] = {
            "provenance": "public (InjecAgent, full)",
            **_eval_set("injecagent", rows, judge=judge, limit=a.limit)}

    ad = _read_jsonl(os.path.join(DATA, "agentdojo", "cases.jsonl"))
    if ad:
        # agentdojo 的 text 多为未渲染模板(TODO)，用 goal 作攻击意图更真实
        rows = [{"text": (r.get("goal") or r.get("text") or ""),
                 "label": 1, "attacker_tools": [],
                 "slice": str(r.get("template_name") or "na")} for r in ad]
        rep["sets"]["agentdojo_slice"] = {
            "provenance": "public (AgentDojo templates, goal-only)",
            **_eval_set("agentdojo", rows, judge=judge, limit=a.limit)}

    # Utility：用中文集的良性行（负样本）评估误拦
    ch = _read_jsonl(os.path.join(DATA, "chinese", "cases.jsonl"))
    if ch:
        rows = [{"text": r.get("text") or "", "label": r.get("label")} for r in ch]
        u, nb = _utility(rows, judge=judge)
        rep["utility_under_attack"] = {
            "benign_n": nb, "pass_rate": u,
            "provenance": "self-built chinese benign rows（非公开）"}

    text = json.dumps(rep, ensure_ascii=False, indent=2)
    print(text)
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
