#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""NEXT-03：公开可复核越狱基准实测（本地已有公开集，不联网、不碰 GitHub）。

数据来源：data/external/jailbreak_classification/prompts.jsonl
  jackhhao/jailbreak-classification（Apache-2.0，manifest 内有 license_url /
  download_url / sha256）。本脚本如实标注实际数据集；不冒充 JailbreakBench
  100 Behaviors（那套数据本机不可复核，按方案"绝不冒充"条款不使用）。

用法：python scripts/eval_jailbreak_public.py --n 100 --out experiments/pub_jailbreak_layered.json
"""
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import eval_public_benchmarks as P                          # noqa: E402

DATA = os.path.join(ROOT, "data", "external",
                    "jailbreak_classification", "prompts.jsonl")
MANIFEST = os.path.join(ROOT, "data", "external",
                        "jailbreak_classification", "manifest.json")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="每类抽样条数")
    ap.add_argument("--out", default="experiments/pub_jailbreak_layered.json")
    a = ap.parse_args()

    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    rows = [json.loads(x) for x in open(DATA, encoding="utf-8") if x.strip()]
    attack = [r for r in rows if r.get("label") in ("jailbreak", "attack", 1)]
    benign = [r for r in rows if r.get("label") in ("benign", "safe", 0)]
    # 稳定抽样：按 source_row 排序后取前 n，避免随机性影响复核
    sample = sorted(attack, key=lambda r: r.get("source_row", 0))[:a.n] + \
        sorted(benign, key=lambda r: r.get("source_row", 0))[:a.n]

    lat, tp, fp, tn, fn = [], 0, 0, 0, 0
    by_layer = {"L1_light": 0, "L1_high_stakes": 0,
                "L2_semantic": 0, "missed": 0}
    for r in sample:
        text = str(r.get("text") or "")
        t0 = time.perf_counter()
        blocked, layer, _dec = P.detect_layered(
            text, judge=None, channel="user_input")
        lat.append((time.perf_counter() - t0) * 1000)
        if blocked:
            by_layer[layer] = by_layer.get(layer, 0) + 1
        else:
            by_layer["missed"] += 1
        is_atk = r in attack
        if is_atk and blocked:
            tp += 1
        elif is_atk:
            fn += 1
        elif blocked:
            fp += 1
        else:
            tn += 1

    rep = {
        "benchmark": "public jailbreak classification (direct prompt)",
        "provenance": {
            "dataset": manifest["source"]["name"],
            "source_url": manifest["source"].get("download_url")
            or manifest["source"].get("license_url"),
            "license": manifest["source"].get("license"),
            "mirror_of": manifest["source"].get("mirror_of"),
            "file": os.path.relpath(DATA, ROOT).replace("\\", "/"),
            "sha256": sha256_file(DATA),
            "note": "实际使用本公开二分类集；非 JailbreakBench 100 Behaviors，"
                    "不冒充未在本机复核的数据集。",
        },
        "n": len(sample),
        "n_unique": len({hashlib.sha256(
            str(r.get("text") or "").encode("utf-8")).hexdigest()
            for r in sample}),
        "n_attack": len(tp_name := [r for r in sample if r in attack]),
        "n_benign": len(sample) - len(tp_name),
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "fpr": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "p95_ms": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 3)
        if lat else 0,
        "duplicate_ratio": round(1 - len({
            hashlib.sha256(str(r.get("text") or "").encode("utf-8")
                           ).hexdigest() for r in sample}) / max(1, len(sample)), 4),
        "blocked_by": by_layer,
        "confusion": {"tp": tp, "fn": fn, "fp": fp, "tn": tn},
        "judge": "rule-only L1 (manifest: L2 不适用于直接越狱输入落点)",
    }
    out = os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: rep[k] for k in
                      ("n", "recall", "fpr", "p95_ms", "blocked_by")},
                     ensure_ascii=False))
    print("WROTE", a.out)


if __name__ == "__main__":
    main()
