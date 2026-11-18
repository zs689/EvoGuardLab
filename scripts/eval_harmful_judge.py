#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""直接越狱判官评测：**开发集调 prompt，留出集只跑一次**。

    python scripts/eval_harmful_judge.py --out experiments/harmful_request_judge/result.json

设计（防自欺）：
- 中文越狱集 62 条全跑（31 attack / 31 benign）—— 这是已知最难的集；
- 英文 jb_classification 用固定 seed 从**未看过**的区间抽 60 条留出；
- 开发阶段只看过 jb_classification 的前 6 条（#0000-#0005），
  `--holdout-start 100` 保证不重叠；
- 报 recall / FPR / 不一致数 / 判官错误数，一个不放宽；
- 不写耗时进产物（两次重跑要能逐字节一致）。
"""
import argparse
import io
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import harmful_request_judge as HJ                          # noqa: E402

DATA = os.path.join(ROOT, "data", "external")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _eval(judge, rows, label_fn, name, limit=0):
    if limit:
        rows = rows[:limit]
    out = {"set": name, "n": len(rows), "rows": [],
           "tp": 0, "fn": 0, "fp": 0, "tn": 0, "errors": 0}
    t0 = time.time()
    for r in rows:
        y = label_fn(r)
        v = judge.judge(r["text"])
        pred = bool(v.get("harmful"))
        err = bool(v.get("error"))
        out["errors"] += int(err)
        # 判官错误不算命中也不算漏：单列 errors，避免「假装没判」被记成通过
        if not err:
            if y and pred:
                out["tp"] += 1
            elif y and not pred:
                out["fn"] += 1
            elif not y and pred:
                out["fp"] += 1
            else:
                out["tn"] += 1
        out["rows"].append({"case_id": r.get("case_id") or r.get("technique"),
                            "label": "attack" if y else "benign",
                            "predicted": pred, "error": v.get("error"),
                            "reason": v.get("reason")})
    pos, neg = out["tp"] + out["fn"], out["fp"] + out["tn"]
    out["recall"] = round(out["tp"] / pos, 4) if pos else None
    out["fpr"] = round(out["fp"] / neg, 4) if neg else None
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--holdout-start", type=int, default=100)
    ap.add_argument("--holdout-n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20261118)
    a = ap.parse_args()

    judge = HJ.HarmfulRequestJudge()

    zh = _read(os.path.join(DATA, "chinese_jailbreak", "cases.jsonl"))
    en_all = _read(os.path.join(DATA, "jailbreak_classification",
                                "prompts.jsonl"))
    rng = random.Random(a.seed)
    # 留出：从没看过的区间抽，平衡正负
    en_hold = [r for r in en_all[a.holdout_start:]
               if r.get("label") in ("jailbreak", "benign")]
    en_att = [r for r in en_hold if r["label"] == "jailbreak"]
    en_ben = [r for r in en_hold if r["label"] == "benign"]
    rng.shuffle(en_att); rng.shuffle(en_ben)
    half = a.holdout_n // 2
    holdout = en_att[:half] + en_ben[:half]
    rng.shuffle(holdout)

    reports = []
    reports.append(_eval(judge, zh, lambda r: r.get("label") == "attack",
                         "chinese_jailbreak (known-hard, all 62)"))
    print("中文越狱集:", reports[-1]["recall"], "FPR", reports[-1]["fpr"],
          flush=True)
    reports.append(_eval(judge, holdout,
                         lambda r: r.get("label") == "jailbreak",
                         "jb_classification HOLDOUT (unseen)"))
    print("英文留出集:", reports[-1]["recall"], "FPR", reports[-1]["fpr"],
          flush=True)

    # 与旧判官对照：注入判官在这些样本上的表现（已有盲测数字，不重跑模型）
    rep = {"version": 1,
           "judge": HJ.describe(),
           "protocol": {"holdout_start": a.holdout_start,
                        "holdout_n": len(holdout), "seed": a.seed,
                        "note": "prompt 仅按中文集前 6 条 + 英文前 6 条调整；"
                                "留出集从 index>=100 抽，未参与调参"},
           "sets": {r["set"]: r for r in reports}}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
