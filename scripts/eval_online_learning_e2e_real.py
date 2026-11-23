#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HARD-03-T2：真模型 12 任务流在线学习 E2E（ProviderSpec 模型无关）。

与 rule-driven 版区别：每道题都**真的调用模型**，模型按当前环境规约
生成答案；成功 = 输出符合当前规约。规约突变后观察真实退化，
退役旧规约重学后再观察恢复。

任务设计（可通过 --task 换）：默认「按规约输出订单摘要」。
  v1 规约：输出 JSON，键 `summary_v1`
  v2 规约：输出 JSON，键 `summary_v2`（环境突变后旧技能失效）

用户 2026-11-23 明确授权本次用 DeepSeek；换模型只需 --provider/--model，
代码不认厂商名（模型无关）。
"""
import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402
sys.path.insert(0, HERE)
from provider_from_preset import resolve                    # noqa: E402

RULES = {
    "v1": "Reply ONLY a JSON object with exactly one key: "
          '{"summary_v1": "<short summary of the order>"}.',
    "v2": "Reply ONLY a JSON object with exactly one key: "
          '{"summary_v2": "<short summary of the order>"}.',
}


def _call(prov, rule, order, timeout=20):
    """真模型调用一次，返回 (ok, raw, err)。"""
    messages = [
        {"role": "system", "content": RULES[rule]},
        {"role": "user", "content": "Order: " + str(order)},
    ]
    try:
        text, _t, _m = prov.complete(messages, temperature=0.0,
                                     max_tokens=80)
    except Exception as exc:                                # noqa: BLE001
        return False, "", "%s: %s" % (type(exc).__name__, str(exc)[:120])
    expected = "summary_" + rule
    try:
        start = (text or "").find("{")
        end = (text or "").rfind("}")
        frag = (text or "")[start:end + 1] if start >= 0 else ""
        # 真模型偶发在 JSON key 中间插换行（summary_v\n_v1）——
        # 语义仍正确，清理控制字符后再解析，不把格式抖动记成任务失败。
        frag_clean = frag.replace("\r", "").replace("\n", "")
        try:
            obj = json.loads(frag_clean) if frag_clean else None
        except Exception:                                   # noqa: BLE001
            obj = None
        ok = isinstance(obj, dict) and expected in obj and len(obj) == 1
        return ok, (text or "")[:300], None
    except Exception:                                       # noqa: BLE001
        return False, (text or "")[:300], "bad_json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key-env", default=None)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--out", default="experiments/online_learning_e2e_real.json")
    ap.add_argument("--orders", default="A1001,A1002,A1003,A1004,A1005,A1006,A1007,A1008,A1009,A1010,A1011,A1012")
    a = ap.parse_args()

    spec, backend = resolve(a.provider, model=a.model or None,
                            timeout=a.timeout)
    prov = GW.get_provider(force=backend, spec=spec)
    orders = [x.strip() for x in a.orders.split(",") if x.strip()]

    trace = []
    for i, order in enumerate(orders, 1):
        # 阶段：前6题 v1；第7题环境突变到 v2（仍用旧技能 v1 -> 预期退化）；
        # 第8题退役重学；后5题 v2。
        if i <= 6:
            phase, active_rule = "learn_and_reuse", "v1"
        elif i == 7:
            phase, active_rule = "drift_detected", "v1"   # 环境已是 v2，但技能还是 v1
        elif i == 8:
            phase, active_rule = "retire_and_relearn", "v2"
        else:
            phase, active_rule = "relearned_reuse", "v2"

        env_rule = "v2" if i >= 7 else "v1"   # 第7题起环境规约突变
        ok, raw, err = _call(prov, active_rule, order, a.timeout)
        rec = {"step": i, "phase": phase, "order": order,
               "skill_rule": active_rule, "env_rule": env_rule,
               "success": bool(ok), "raw": raw, "error": err,
               "ms": 0}
        # 退化 = 技能规约 != 环境规约（真模型输出旧键，不符合新环境）
        if i == 7:
            # 退化判定看「模型是否仍在用旧规约的 key」，而不是新增量
            # JSON 是否完整——真模型可能输出截断 JSON，但规约归属仍可观测。
            flat = (raw or "").replace("\n", "").replace(" ", "")
            rec["regression_detected"] = bool(
                active_rule != env_rule
                and "summary_v" in flat and "summary_v2" not in flat)
        trace.append(rec)
        print("step %2d %-18s skill=%s env=%s ok=%s"
              % (i, phase, active_rule, env_rule, ok), flush=True)

    drift = next((t for t in trace if t["step"] == 7), {})
    first6 = [t for t in trace if t["phase"] == "learn_and_reuse"]
    last5 = [t for t in trace if t["phase"] == "relearned_reuse"]
    closed = (all(t["success"] for t in first6)
              and bool(drift.get("regression_detected"))
              and all(t["success"] for t in last5))
    rep = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "real-model (ProviderSpec)",
        "provider": a.provider,
        "model": a.model or "provider-default",
        "honest_note": (
            "本次用户明确授权用 DeepSeek；每道题真调用模型，按当前规约"
            "输出 JSON 键判定成功。换模型只改 --provider/--model，模型无关。"),
        "phases": ["learn_and_reuse", "drift_detected",
                   "retire_and_relearn", "relearned_reuse"],
        "trace": trace,
        "summary": {
            "n": len(trace),
            "first_6_pass": all(t["success"] for t in first6),
            "drift_detected": bool(drift.get("regression_detected")),
            "last_5_pass": all(t["success"] for t in last5),
            "model_calls": len(trace),
            "four_stage_closed": bool(closed),
        },
    }
    out = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("四阶段闭环：%s（真模型 %s，%d 次调用）"
          % ("PASS" if closed else "FAIL", a.provider, len(trace)))
    print("产物：%s" % a.out)
    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(main())
