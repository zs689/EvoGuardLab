#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PROD-03-T1/T2：训练轻量双语注入分类器（**纯 numpy，零第三方推理依赖**）。

为什么不用下载的模型：
- 本机现存 DeBERTa(英文) 在中文盲测 recall 0.47 / FPR 0.34（实测），不可用；
- HuggingFace 两个源 SSL 都断；ModelScope 中文模型 409MB 且许可不明；
- 手上已有 2473 条标注数据，足以训练 char n-gram + LR 的轻量模型，
  推理只依赖 numpy（可选隔离 venv）或纯 Python（导出后）。

**防自欺**：严格按行号前后 70/30 切分，留出集**不参与任何训练/调参**；
训练只报留出集数字。导出权重 JSON 供运行时加载，核心库不 import 训练依赖。
"""
import argparse
import io
import json
import math
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

DATA = os.path.join(ROOT, "data", "external")
NGRAMS = (2, 3, 4)
MAX_FEATURES = 12000


def _read(path, label_fn):
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            out.append((r.get("text") or "", label_fn(r)))
    return out


def load_datasets():
    """(text, y)：y=1 注入/越狱，0 正常。按固定顺序拼接，便于复现切分。"""
    rows = []
    rows += _read(os.path.join(DATA, "chinese", "cases.jsonl"),
                  lambda r: 1 if r.get("label") == "attack" else 0)
    rows += _read(os.path.join(DATA, "chinese_jailbreak", "cases.jsonl"),
                  lambda r: 1 if r.get("label") == "attack" else 0)
    rows += _read(os.path.join(DATA, "jailbreak_classification", "prompts.jsonl"),
                  lambda r: 1 if r.get("label") == "jailbreak" else 0)
    return rows


def feats(text, vocab=None):
    """字符 n-gram 计数（中文友好，不依赖分词）。"""
    t = re.sub(r"\s+", " ", str(text or "").lower())
    out = {}
    for n in NGRAMS:
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            out[g] = out.get(g, 0) + 1
    return out


def build_vocab(rows):
    from collections import Counter
    c = Counter()
    for text, _y in rows:
        for g, v in feats(text).items():
            c[g] += v
    return [g for g, _ in c.most_common(MAX_FEATURES)]


def vec(text, vocab):
    v = feats(text)
    x = [float(v.get(g, 0)) for g in vocab]
    n = math.sqrt(sum(a * a for a in x)) or 1.0
    return [a / n for a in x]


def train(rows, vocab, epochs=12, lr=0.35):
    """numpy 向量化训练（隔离 venv）；导出权重后推理仍可纯 Python。"""
    import numpy as np
    X = np.asarray([vec(t, vocab) for t, _ in rows], dtype=np.float32)
    y = np.asarray([float(v) for _, v in rows], dtype=np.float32)
    w = np.zeros(len(vocab), dtype=np.float32)
    b = 0.0
    for ep in range(epochs):
        # 小批量 SGD（batch=64）：比逐条快一个数量级，收敛等价
        for i in range(0, len(X), 64):
            xb, yb = X[i:i + 64], y[i:i + 64]
            z = xb @ w + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            d = p - yb
            w -= lr * (xb.T @ d) / len(yb)
            b -= lr * float(d.mean())
    return w.tolist(), float(b)


def predict(text, vocab, w, b):
    x = vec(text, vocab)
    z = b + sum(wi * xi for wi, xi in zip(w, x) if xi)
    return 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))


def evaluate(rows, vocab, w, b):
    tp = fp = tn = fn = 0
    for text, y in rows:
        p = predict(text, vocab, w, b) >= 0.5
        if y and p: tp += 1
        elif y: fn += 1
        elif p: fp += 1
        else: tn += 1
    return {"n": len(rows), "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "recall": round(tp / max(1, tp + fn), 4),
            "fpr": round(fp / max(1, fp + tn), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--split", type=float, default=0.7)
    a = ap.parse_args()

    rows = load_datasets()
    # 每个数据源内部按行顺序切，保证留出集是「没见过的后半段」
    # 这里简单按全局索引切（数据已按源分组，后半段覆盖所有源）
    k = int(len(rows) * a.split)
    train_rows, hold_rows = rows[:k], rows[k:]
    t0 = time.time()
    vocab = build_vocab(train_rows)
    w, b = train(train_rows, vocab)
    # 阈值在**训练集内部**校准（用训练集的预测分布选使 F1 最大的阈值），
    # 绝不看留出集 —— 否则就是拿测试集调参。
    train_scores = [(predict(t, vocab, w, b), y) for t, y in train_rows]
    # 约束式校准：在**训练集上** FPR <= 0.05 的前提下最大化 recall。
    # 不能按 F1 选 —— 那会把 FPR 冲到 0.09（实测），超出方案硬门槛。
    best_thr, best_recall = 0.5, -1.0
    train_neg = sum(1 for _, y in train_scores if not y)
    for i in range(10, 95):
        thr = i / 100.0
        tp = sum(1 for p0, y in train_scores if y and p0 >= thr)
        fp = sum(1 for p0, y in train_scores if not y and p0 >= thr)
        fn = sum(1 for p0, y in train_scores if y and p0 < thr)
        if fp / max(1, train_neg) > 0.05:
            continue
        rec = tp / max(1, tp + fn)
        if rec > best_recall:
            best_recall, best_thr = rec, thr
    def evaluate_thr(rows, thr):
        tp = fp = tn = fn = 0
        for text, y in rows:
            p = predict(text, vocab, w, b) >= thr
            if y and p: tp += 1
            elif y: fn += 1
            elif p: fp += 1
            else: tn += 1
        return {"n": len(rows), "tp": tp, "fn": fn, "fp": fp, "tn": tn,
                "recall": round(tp / max(1, tp + fn), 4),
                "fpr": round(fp / max(1, fp + tn), 4)}
    rep = {
        "model": "char n-gram(2-4) TF-normalized + logistic regression",
        "calibrated_threshold": best_thr,
        "train_recall_at_threshold": round(best_recall, 4),
        "train_n": len(train_rows), "holdout_n": len(hold_rows),
        "vocab": len(vocab),
        "holdout": evaluate_thr(hold_rows, best_thr),
        "train": evaluate_thr(train_rows, best_thr),
        "train_seconds": round(time.time() - t0, 1),
        "note": "留出集按行号后30%切分，未参与训练/调参",
    }
    out = {"w": w, "b": b, "vocab": vocab, "threshold": best_thr,
           "ngrams": list(NGRAMS), "version": "light-v1"}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
