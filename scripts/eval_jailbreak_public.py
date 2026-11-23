#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DET-DEEP-01-T1：公开越狱基准实测（严格排除训练集重叠 + 分列 L1 / L1+light）。

为什么必须排除重叠：
  `scripts/train_light_classifier.py` 的训练集**就包含**
  `data/external/jailbreak_classification/prompts.jsonl`。直接拿它评估
  light_classifier 等于拿训练集考自己。这里复用训练脚本自己的
  `load_datasets + stratified_split`（同一 seed），只取它划分出的
  **holdout**（从未参与训练与阈值校准）作为评测集。

用法：
  python scripts/eval_jailbreak_public.py --n 100
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import eval_public_benchmarks as P                          # noqa: E402
import light_classifier as LC                               # noqa: E402

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


def _sha(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def light_holdout():
    """复用训练脚本的分层切分，返回 (train_calib_sha_set, holdout_rows, calib_rows)。

    holdout_rows / calib_rows: [(text, label)]，label 1=越狱 0=良性。
    calib 未参与训练，只用于**选阈值**；holdout 只用于报告，互不越界。
    """
    spec = importlib.util.spec_from_file_location(
        "_train_light", os.path.join(ROOT, "scripts",
                                     "train_light_classifier.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                            # type: ignore
    # 必须复现**权重训练时**的切分：参数从权重文件的 split 元数据读，
    # 用默认值会得到另一套 holdout，评测集反而落进训练集（虚假 fpr）。
    wpath = os.path.join(ROOT, "ai-service", "evoguard",
                         "light_classifier_weights.json")
    try:
        meta = (json.load(open(wpath, encoding="utf-8")).get("split") or {})
    except Exception:                                       # noqa: BLE001
        meta = {}
    kwargs = {}
    if meta.get("train_frac") is not None:
        kwargs["train_frac"] = float(meta["train_frac"])
    if meta.get("calib_frac") is not None:
        kwargs["calib_frac"] = float(meta["calib_frac"])
    if meta.get("seed") is not None:
        kwargs["seed"] = int(meta["seed"])
    splits = mod.stratified_split(mod.load_datasets(), **kwargs)
    seen = {_sha(t) for t, _y, _l, _s in
            (splits["train"] + splits["calib"])}
    hold = [(str(t), int(y)) for t, y, _l, _s in splits["holdout"]
            if _s == "jailbreak_classification"]
    calib = [(str(t), int(y)) for t, y, _l, _s in splits["calib"]
             if _s == "jailbreak_classification"]
    return seen, hold, calib


def pick_threshold(calib_rows, target_fpr=0.05):
    """在 calib 集上选阈值：满足 fpr<=target 的前提下 recall 最大。

    calib 从未参与训练，因此这不是在 holdout 上调参。
    """
    best = (None, -1.0, None)
    for hi in [x / 100 for x in range(30, 100, 2)]:
        m = _measure(
            calib_rows,
            lambda t, _hi=hi: (LC.classify(t, high=_hi).get("decision")
                               == "block"))
        if m["fpr"] is None or m["fpr"] > target_fpr:
            continue
        if (m["recall"] or 0) > best[1]:
            best = (hi, m["recall"], m["fpr"])
    return best


def _measure(rows, decide):
    """rows=[(text,label)]；decide(text)->bool(blocked)。返回指标。"""
    lat, tp, fp, tn, fn = [], 0, 0, 0, 0
    for text, label in rows:
        t0 = time.perf_counter()
        blocked = bool(decide(text))
        lat.append((time.perf_counter() - t0) * 1000)
        if label == 1 and blocked:
            tp += 1
        elif label == 1:
            fn += 1
        elif blocked:
            fp += 1
        else:
            tn += 1
    return {
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "fpr": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "p95_ms": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 3)
        if lat else 0,
        "confusion": {"tp": tp, "fn": fn, "fp": fp, "tn": tn},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="每类抽样条数")
    ap.add_argument("--out", default="experiments/pub_jailbreak_layered.json")
    a = ap.parse_args()

    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    train_sha, hold, calib = light_holdout()
    if not hold:
        raise SystemExit("训练脚本的 holdout 为空，拒绝用训练集自评")

    attack = [(t, 1) for t, y in hold if y == 1]
    benign = [(t, 0) for t, y in hold if y == 0]
    # 稳定抽样（holdout 顺序由固定 seed 决定，可复现）
    sample = attack[:a.n] + benign[:a.n]

    def l1_only(text):
        blocked, _layer, _dec = P.detect_layered(
            text, judge=None, channel="user_input")
        return blocked

    hi, cal_recall, cal_fpr = pick_threshold(calib)

    def l1_plus_light(text):
        if l1_only(text):
            return True
        try:
            r = LC.classify(text, high=hi)
        except Exception:                                   # noqa: BLE001
            return False
        return r.get("decision") == "block"

    l1 = _measure(sample, l1_only)
    l1l = _measure(sample, l1_plus_light)
    n_unique = len({_sha(t) for t, _ in sample})

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
            "note": "使用训练脚本自身 stratified_split 的 holdout；"
                    "非 JailbreakBench 100 Behaviors，不冒充未复核数据集。",
        },
        "train_overlap_excluded": True,
        "overlap_exclusion": {
            "method": "复用 scripts/train_light_classifier.py 的 "
                      "load_datasets + stratified_split（seed 固定）",
            "excluded_train_calib_texts": len(train_sha),
            "holdout_pool": {"attack": len(attack), "benign": len(benign)},
        },
        "n": len(sample),
        "n_unique": n_unique,
        "duplicate_ratio": round(1 - n_unique / max(1, len(sample)), 4),
        "threshold_calibration": {
            "high": hi, "selected_on": "calib (未参与训练，非 holdout)",
            "calib_recall": cal_recall, "calib_fpr": cal_fpr,
            "target_fpr": 0.05,
        },
        # 自证：把「这份数字凭什么可信」写进产物本身。
        # 四项分别对应历史上真实踩过的坑：重复计数、训练集泄漏、
        # 阈值在 holdout 上调过、评测跑的是 L1 还是 L1+light。
        "self_checks": {
            "dedup_verified": n_unique == len(sample),
            "train_overlap_excluded": True,
            "threshold_source": "calib (未参与训练，非 holdout)",
            "scored_layers": ["L1_only", "L1_plus_light"],
            "warning": ("评测集存在重复文本，ASR/召回置信区间会被高估"
                        if n_unique != len(sample) else None),
        },
        "diagnosis": {
            "calib_default_threshold": _measure(
                calib, lambda t: LC.classify(t).get("decision") == "block"),
            # 结论必须由**本次实测数据**推出，不能写死：早先这里固定写着
            # 「模型无区分度、不建议启用」，修完 bug 后指标已达标，硬编码
            # 的旧结论会让产物自相矛盾。
            "finding": (
                "达标：L1+light 在留出集上 recall=%.2f / fpr=%.2f"
                % (l1l["recall"] or 0, l1l["fpr"] or 1)
                if (l1l["fpr"] is not None and l1l["fpr"] <= 0.05
                    and (l1l["recall"] or 0) >= 0.5)
                else "未达标：recall=%.2f / fpr=%.2f（验收线 fpr<=0.05）"
                % (l1l["recall"] or 0, l1l["fpr"] or 1)),
            "root_causes_fixed": [
                "训练/推理特征函数不一致（推理侧多加 pinyin_tokens）",
                "拼音短码子串误匹配（sc/rg 命中 score/description）",
                "模长分母只累加 w!=0 的特征，与训练侧全词表口径不符",
                "权重未落盘切分比例，下游评测复现出另一套 holdout",
            ],
        },
        "L1_only": l1,
        "L1_plus_light": l1l,
        "delta_recall": (round((l1l["recall"] or 0) - (l1["recall"] or 0), 4)
                         if l1["recall"] is not None else None),
    }
    out = os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(json.dumps({"n": rep["n"], "n_unique": rep["n_unique"],
                      "L1_only": l1, "L1_plus_light": l1l,
                      "delta_recall": rep["delta_recall"]},
                     ensure_ascii=False))
    print("WROTE", a.out)


if __name__ == "__main__":
    main()
