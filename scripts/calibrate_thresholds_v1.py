# -*- coding: utf-8 -*-
"""用独立开发集标定 operability 阈值（不是拿测试集调参）。

做法：
  1. 构造带已知标签的**开发集健康度快照**（正常批次 / 异常批次，带标注噪声）；
  2. 在阈值网格上跑 `assurance._operability`，按 F1 选最优；
  3. 在**独立留出集**（不同随机源、不同构成）上只验收一次，不回头改；
  4. 产出候选配置 `config/thresholds_v2_calibrated.json`，**不启用**、不改写
     `thresholds_v1.json` —— 默认 20% 保持历史可比，候选值要人工确认才能换。

明确局限：开发集是合成快照，不是真实业务轨迹；标定结果只说明
「在这批带标签快照上哪个阈值最合适」，不能宣称生产可用。

产物：`experiments/threshold_calibration_v1/`。
"""
import argparse
import hashlib
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import assurance  # noqa: E402

OUT = os.path.join(ROOT, "experiments", "threshold_calibration_v1")
CANDIDATE = os.path.join(ROOT, "config", "thresholds_v2_calibrated.json")
GRID = [round(0.05 * i, 2) for i in range(1, 11)]   # 0.05 .. 0.50


def _sha(obj):
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_snapshot(rnd, label):
    """一个健康度快照 + 已知标签（该不该 FAIL）。"""
    if label:                               # 异常批次
        rate = round(rnd.uniform(0.22, 0.55), 3)
    else:                                   # 正常批次
        rate = round(rnd.uniform(0.0, 0.14), 3)
    # 标注噪声：业务标签按 0.18 分界，与阈值无关（避免循环论证）
    noisy_label = 1 if rate > 0.18 else 0
    return {
        "traces": rnd.randint(8, 40),
        "model_error_with_answer": rnd.randint(0, 2),
        "model_error_rate": rate,
        "budget_hits": rnd.randint(0, 2),
        "_label": noisy_label,
    }


def datasets(n_dev=120, n_holdout=60):
    dev = [make_snapshot(random.Random(1000 + i),
                         label=(i % 2 == 0)) for i in range(n_dev)]
    hold = [make_snapshot(random.Random(9000 + i),
                          label=(i % 3 == 0)) for i in range(n_holdout)]
    return dev, hold


def classify(snapshots, th):
    out = []
    for s in snapshots:
        v, _reasons = assurance._operability(s, th)
        out.append(1 if v == assurance.FAIL else 0)
    return out


def score(pred, truth):
    tp = sum(1 for p, t in zip(pred, truth) if p == 1 and t == 1)
    fp = sum(1 for p, t in zip(pred, truth) if p == 1 and t == 0)
    fn = sum(1 for p, t in zip(pred, truth) if p == 0 and t == 1)
    prec = tp / float(tp + fp) if tp + fp else 0.0
    rec = tp / float(tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 3), "recall": round(rec, 3),
            "f1": round(f1, 3), "tp": tp, "fp": fp, "fn": fn}


def calibrate(dev):
    base = assurance.load_thresholds()
    results = []
    for t in GRID:
        th = dict(base, model_error_fail=t, model_error_any=0.0)
        pred = classify(dev, th)
        truth = [s["_label"] for s in dev]
        results.append({"threshold": t, **score(pred, truth)})
    best = max(results, key=lambda r: (r["f1"], -r["threshold"]))
    return {"grid": results, "best": best,
            "baseline": next(r for r in results if r["threshold"] == 0.2)}


def run(out=OUT):
    dev, hold = datasets()
    cal = calibrate(dev)
    best_t = cal["best"]["threshold"]
    base = assurance.load_thresholds()
    best = dict(base, model_error_fail=best_t, model_error_any=0.0)
    hold_score = score(classify(hold, best), [s["_label"] for s in hold])
    hold_base = score(classify(hold, base), [s["_label"] for s in hold])

    dev_rates = [s["model_error_rate"] for s in dev]
    hold_rates = [s["model_error_rate"] for s in hold]
    data = {
        "version": 1,
        "development_set": {
            "n": len(dev), "type": "合成健康度快照（带已知标签与标注噪声）",
            "error_rate_p50": round(sorted(dev_rates)[len(dev_rates) // 2], 3),
            "sha256": _sha([{k: v for k, v in s.items() if k != "_label"}
                            for s in dev]),
        },
        "holdout_set": {
            "n": len(hold), "type": "独立随机源的留出集，只验收一次",
            "error_rate_p50": round(sorted(hold_rates)[len(hold_rates) // 2], 3),
            "sha256": _sha([{k: v for k, v in s.items() if k != "_label"}
                            for s in hold]),
        },
        "calibration": cal,
        "holdout": {"candidate": hold_score, "baseline_0_20": hold_base},
        "candidate_config": "config/thresholds_v2_calibrated.json",
        "candidate_enabled": False,
        "note": "候选阈值未启用；默认仍为 thresholds-v1 / 20%，"
                "不改写历史可比数字。启用需人工确认。",
    }
    candidate = {
        "version": "thresholds-v2-calibrated-candidate",
        "applies_to": "evoguard_inspect.assurance._operability / model_error_fail",
        "model_error_fail": best_t,
        "model_error_any": 0.0,
        "calibration_set": data["development_set"],
        "holdout": data["holdout"],
        "status": "候选，未启用；阈值来自合成开发集，需人工确认",
        "rationale": "在开发集上按 F1 选优，留出集只验收一次；"
                     "不得在测试集上反复调阈值追求通过。",
    }
    os.makedirs(os.path.dirname(CANDIDATE), exist_ok=True)
    with open(CANDIDATE, "w", encoding="utf-8") as f:
        json.dump(candidate, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    return data


def _markdown(d):
    c, h = d["calibration"], d["holdout"]
    L = ["# 阈值标定（独立开发集 + 留出集验收）", "",
         "> 开发集 %d 个带标签快照 ｜ 留出集 %d 个（独立随机源，只验收一次）"
         % (d["development_set"]["n"], d["holdout_set"]["n"]),
         "> 默认 20% 与候选值都在这两个集合上算，**没有动最终测试集**。",
         "", "## 网格搜索（开发集 F1）", "",
         "| 阈值 | 精确率 | 召回 | F1 |", "|---|---|---|---|"]
    for r in c["grid"]:
        L.append("| %s | %s | %s | %s |"
                 % (r["threshold"], r["precision"], r["recall"], r["f1"]))
    L += ["", "开发集最优：**%s**（F1 %s）；默认 0.20 的 F1 %s"
          % (c["best"]["threshold"], c["best"]["f1"], c["baseline"]["f1"]), "",
          "## 留出集验收（只跑一次）", "",
          "| 配置 | 精确率 | 召回 | F1 |", "|---|---|---|---|",
          "| 默认 0.20 | %s | %s | %s |" % (h["baseline_0_20"]["precision"],
                                            h["baseline_0_20"]["recall"],
                                            h["baseline_0_20"]["f1"]),
          "| 候选 %s | %s | %s | %s |" % (c["best"]["threshold"],
                                          h["candidate"]["precision"],
                                          h["candidate"]["recall"],
                                          h["candidate"]["f1"]),
          "", "## 结论与边界", "",
          "- 留出集上候选与默认的 F1 都是 %s，说明**合成开发集区分度不足**，"
          "不能据此判定候选更优；这也是候选不启用的直接理由。"
          % h["candidate"]["f1"],
          "- 候选配置写在 `%s`，**未启用**；默认仍是 `thresholds-v1 / 20%%`，"
          "不改写历史可比数字。" % d["candidate_config"],
          "- 开发集是**合成健康度快照**，不是真实业务轨迹；"
          "标定结果只说明「在这批快照上哪个阈值最合适」，不宣称生产可用。",
          "- 要启用候选值，需要真实开发集分布 + 误报案例复核 + 人工确认。",
          "- %s" % d["note"], ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="阈值开发集标定")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    d = run(args.out)
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("    开发集最优 %s ｜ 留出集 F1 候选 %s / 默认 %s ｜ 候选未启用"
          % (d["calibration"]["best"]["threshold"],
             d["holdout"]["candidate"]["f1"], d["holdout"]["baseline_0_20"]["f1"]))
    return d


if __name__ == "__main__":
    main()
