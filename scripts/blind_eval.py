#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""INT-03-T2：独立盲测入口（v3 之外的外部/留出数据）。

    python scripts/blind_eval.py --out experiments/blind_eval/result.json
    python scripts/blind_eval.py --judge ollama --limit 200

**数据来源如实标注**：
- `injecagent`：公开数据集（InjecAgent），真外部；
- `chinese` / `chinese_jailbreak`：项目自建中文样本（`source: self`），
  **不是**外部公开语料 —— 结果只能说明「在自建集上的表现」。
- `agentdojo`：公开基准，格式为任务/工具定义，不是逐条文本，本脚本不混算。

用途：v3 的 8 条线是在模板化载荷上过的；这里用独立来源数据复核，
避免「只在自己的模板上有效」的自我循环。
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

import gateway as _gw                                       # noqa: E402
import run_semantic_injection_v3 as V                       # noqa: E402

DATA = os.path.join(ROOT, "data", "external")


def _read_jsonl(path):
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def load_sets():
    sets = {}
    p = os.path.join(DATA, "chinese", "cases.jsonl")
    if os.path.isfile(p):
        rows = _read_jsonl(p)
        sets["chinese_selfbuilt"] = {
            "provenance": "self-built (source:self), NOT public corpus",
            "rows": [{"text": r.get("text") or "",
                      "label": 1 if r.get("label") == "attack" else 0,
                      "slice": str(r.get("technique") or "na")} for r in rows]}
    p = os.path.join(DATA, "chinese_jailbreak", "cases.jsonl")
    if os.path.isfile(p):
        rows = _read_jsonl(p)
        sets["chinese_jailbreak_selfbuilt"] = {
            "provenance": "self-built jailbreak set, NOT public corpus",
            "rows": [{"text": r.get("text") or "",
                      "label": 1 if r.get("label") == "attack" else 0,
                      "slice": str(r.get("technique") or "na")} for r in rows]}
    p = os.path.join(DATA, "injecagent", "cases.jsonl")
    if os.path.isfile(p):
        rows = _read_jsonl(p)
        sets["injecagent_public"] = {
            "provenance": "public dataset (InjecAgent)",
            "rows": [{"text": (r.get("attacker_instruction") or ""),
                      "label": 1,
                      "slice": str(r.get("category") or "na")} for r in rows]}
    return sets


def _rate(hits, labels, positive=True):
    sel = [i for i, y in enumerate(labels) if bool(y) == positive]
    if not sel:
        return None
    return round(sum(1 for i in sel if hits[i]) / float(len(sel)), 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--judge", default="none", choices=["none", "ollama"])
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    provider = _gw.get_provider("ollama") if a.judge == "ollama" else None
    sets = load_sets()
    report = {"version": 1,
              "detector": ("L1+window" if provider is None
                           else "L1+window+semantic_judge(ollama)"),
              "note": ("v3 的 8 条线在模板化载荷上通过；本盲测用独立来源数据复核。"
                       "自建集不等于外部公开语料，结论需按 provenance 分别看。"),
              "sets": {}}
    for name, spec in sets.items():
        rows = spec["rows"][:a.limit] if a.limit else spec["rows"]
        t0 = time.perf_counter()
        hits, labels, lat = [], [], []
        for r in rows:
            h, _codes, ms = V.detect(r["text"], provider=provider)
            hits.append(h)
            labels.append(r["label"])
            lat.append(ms)
        n = len(rows)
        pos = sum(labels)
        rec = _rate(hits, labels, True)
        fp = _rate(hits, labels, False)
        by_slice = collections.defaultdict(lambda: [0, 0])
        for h, y, r in zip(hits, labels, rows):
            by_slice[r["slice"]][1] += 1
            by_slice[r["slice"]][0] += int(h)
        report["sets"][name] = {
            "provenance": spec["provenance"],
            "n": n, "positives": pos, "negatives": n - pos,
            "recall": rec, "fp_rate": fp,
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "p95_ms": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 3) if lat else 0,
            "by_slice": {k: "%d/%d" % (v[0], v[1])
                         for k, v in sorted(by_slice.items())[:12]},
        }
        print("%-30s n=%-5d recall=%s fp=%s"
              % (name, n, report["sets"][name]["recall"],
                 report["sets"][name]["fp_rate"]))
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
