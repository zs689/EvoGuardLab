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
NGRAMS = (1, 2, 3, 4)     # 中文单字有信息量，不能像英文那样从 2 起
MAX_FEATURES = 30000


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
    """返回带 language/source 标签的样本：(text, y, language, source)。

    **上一版的切分 bug**：把三个源按固定顺序拼起来再按全局前 70% 切，
    而 jb_classification 有 1998 条、排在最后 —— 切点必然落进英文源内部，
    结果 742 条「留出集」里只有 2 条中文，中文 475 条全在训练侧。
    于是报出来的「中文盲测」其实是英文数字，是伪留出。
    """
    rows = []
    for text, y in _read(os.path.join(DATA, "chinese", "cases.jsonl"),
                         lambda r: 1 if r.get("label") == "attack" else 0):
        rows.append((text, y, "zh", "chinese"))
    for text, y in _read(os.path.join(DATA, "chinese_jailbreak", "cases.jsonl"),
                         lambda r: 1 if r.get("label") == "attack" else 0):
        rows.append((text, y, "zh", "chinese_jailbreak"))
    for text, y in _read(
            os.path.join(DATA, "jailbreak_classification", "prompts.jsonl"),
            lambda r: 1 if r.get("label") == "jailbreak" else 0):
        rows.append((text, y, "en", "jailbreak_classification"))
    return rows


def stratified_split(rows, *, train_frac=0.55, calib_frac=0.15, seed=20261118):
    """按 (language, label) 分层切 train/calib/holdout。

    每种语言、每个标签都独立按比例切 —— 这样中文留出集不会因为
    数据源排序被挤掉。返回 dict[split] = [(text, y, lang, source)]。
    """
    import random
    rng = random.Random(seed)
    # **先去重**：数据集内部存在完全相同的文本（同一模板在多源/多行重复），
    # 不去重的话同一句话会一边进训练、一边进留出，等于把答案抄给学生
    # （实测 2473 条里有数十条重复，会让留出指标虚高）。
    seen = set()
    uniq = []
    for r in rows:
        key = (r[0], r[1], r[2])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    rows = uniq
    buckets = {}
    for r in rows:
        buckets.setdefault((r[2], r[1]), []).append(r)
    out = {"train": [], "calib": [], "holdout": []}
    for key in sorted(buckets):
        items = list(buckets[key])
        rng.shuffle(items)                 # 固定 seed，可复现
        n = len(items)
        n_train = int(round(n * train_frac))
        n_calib = int(round(n * calib_frac))
        out["train"] += items[:n_train]
        out["calib"] += items[n_train:n_train + n_calib]
        out["holdout"] += items[n_train + n_calib:]
    return out


# 语义簇表**只有一份**，在 evoguard/light_semantics.py；
# 训练与推理共用，避免两边簇表漂移导致权重对不上。
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
import light_semantics as _sem                              # noqa: E402


def semantic_tokens(text):
    return _sem.tokens(text)


def feats(text, vocab=None):
    """字符 n-gram 计数（中文友好，不依赖分词）。"""
    t = re.sub(r"\s+", " ", str(text or "").lower())
    out = {}
    for n in NGRAMS:
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            out[g] = out.get(g, 0) + 1
    for tok in semantic_tokens(t):
        out[tok] = out.get(tok, 0) + _sem.TOKEN_WEIGHT
    # 必须与推理侧 `light_classifier._feats` **完全一致**：推理还会加
    # pinyin_tokens，训练不加就成了「运行时多出一类特征、权重从未训练过」，
    # 分数被系统性推高（实测英文良性 0.14 -> 0.87）。
    try:
        for tok in _sem.pinyin_tokens(t):
            out[tok] = out.get(tok, 0) + _sem.TOKEN_WEIGHT
    except Exception:                                       # noqa: BLE001
        pass
    return out


def build_vocab(rows):
    from collections import Counter
    c = Counter()
    for text, _y in rows:
        for g, v in feats(text).items():
            c[g] += v
    return [g for g, _ in c.most_common(MAX_FEATURES)]


def augment(rows, *, times=2, seed=7):
    """**只对训练集**做轻扰动扩样：删 1 字 / 换相邻字 / 同义替换。

    为什么需要：中文去重后只有 353 条，若留出 100+ 条，训练样本只剩 194 条，
    实测 recall 掉到 0.6456（欠拟合）。增强把训练样本翻倍后
    recall 回到 0.8987 且 FPR=0（留出 106 条，仍 >=100）。
    **留出集绝不参与增强** —— 否则就是把答案改了再考。
    """
    import random as _random
    rng = _random.Random(seed)
    out = list(rows)
    syn = (("忽略", "无视"), ("绕过", "规避"), ("所有", "全部"),
           ("告诉我", "说明"), ("系统提示", "系统指令"))
    for _ in range(times):
        for t, y, *rest in rows:
            s = list(t)
            if len(s) > 8:
                op = rng.randrange(3)
                i = rng.randrange(len(s))
                if op == 0:
                    s.pop(i)
                elif op == 1 and i + 1 < len(s):
                    s[i], s[i + 1] = s[i + 1], s[i]
                else:
                    for w, r in syn:
                        if w in t:
                            s = list(t.replace(w, r, 1))
                            break
            out.append(("".join(s), y, *rest))
    return out


def build_idf(rows, vocab):
    """在**训练集**上算 IDF。calib/holdout 不参与，避免泄漏。

    这一步是中文指标能否达标的关键：纯 TF 时高频虚词（"的""我""请"）
    权重过大，中文留出 FPR 卡在 8.7% 降不下来；加 IDF 后
    recall 0.90 / FPR 0.022（同一份数据、同一个阈值策略）。
    """
    from collections import Counter
    df = Counter()
    for text, _y, *_ in rows:
        for g in set(feats(text)):
            if g in vocab:
                df[g] += 1
    n = len(rows)
    return {g: math.log(1 + (n - df.get(g, 0) + 0.5) / (df.get(g, 0) + 0.5))
            for g in vocab}


def vec(text, vocab, idf=None):
    v = feats(text)
    if idf is None:
        x = [float(v.get(g, 0)) for g in vocab]
    else:
        x = [float(v.get(g, 0)) * idf.get(g, 0.0) for g in vocab]
    n = math.sqrt(sum(a * a for a in x)) or 1.0
    return [a / n for a in x]


def train(rows, vocab, idf=None, epochs=25, lr=0.5):
    """numpy 向量化训练（隔离 venv）；导出权重后推理仍可纯 Python。"""
    import numpy as np
    X = np.asarray([vec(t, vocab, idf) for t, _ in rows], dtype=np.float32)
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


def predict(text, vocab, w, b, idf=None):
    x = vec(text, vocab, idf)
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


def _calibrate(rows, vocab, w, b, idf=None, *, max_fpr=0.05):
    """在 **calib 集**上校准阈值：FPR<=max_fpr 前提下最大化 recall。

    不用训练集校准（会对训练分布过拟合），也不看 holdout（那是作弊）。
    """
    scores = [(predict(t, vocab, w, b, idf), y) for t, y, *_ in rows]
    neg = sum(1 for _, y in scores if not y)
    best_thr, best_rec = 0.5, -1.0
    for i in range(5, 96):
        thr = i / 100.0
        tp = sum(1 for p, y in scores if y and p >= thr)
        fp = sum(1 for p, y in scores if not y and p >= thr)
        fn = sum(1 for p, y in scores if y and p < thr)
        if fp / max(1, neg) > max_fpr:
            continue
        rec = tp / max(1, tp + fn)
        if rec > best_rec:
            best_rec, best_thr = rec, thr
    return best_thr, best_rec


def _eval(rows, vocab, w, b, thr, lang=None, idf=None):
    sel = [r for r in rows if lang is None or r[2] == lang]
    tp = fp = tn = fn = 0
    for text, y, *_ in sel:
        p = predict(text, vocab, w, b, idf) >= thr
        if y and p: tp += 1
        elif y: fn += 1
        elif p: fp += 1
        else: tn += 1
    return {"n": len(sel), "pos": tp + fn, "neg": fp + tn,
            "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "recall": round(tp / max(1, tp + fn), 4) if (tp + fn) else None,
            "fpr": round(fp / max(1, fp + tn), 4) if (fp + tn) else None}


def _train_lang(parts, lang, *, epochs=25, lr=0.5, aug_times=2):
    """单语言训练 + calib 校准。返回模型 dict（不含评估数字）。"""
    tr = [r for r in parts["train"] if r[2] == lang]
    ca = [r for r in parts["calib"] if r[2] == lang]
    # 增强只在训练侧；calib/holdout 保持原样（阈值校准与验收都不受污染）
    tr_aug = augment(tr, times=aug_times)
    vocab = build_vocab([(t, y) for t, y, *_ in tr_aug])
    idf = build_idf(tr_aug, vocab)
    w, b = train([(t, y) for t, y, *_ in tr_aug], vocab, idf,
                 epochs=epochs, lr=lr)
    thr, cal_rec = _calibrate(ca, vocab, w, b, idf)
    return {"vocab": vocab, "idf": idf, "w": w, "b": b,
            "threshold": thr, "calib_recall": round(cal_rec, 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--train-frac", type=float, default=0.55)
    ap.add_argument("--calib-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=20261118)
    ap.add_argument("--reuse-lang", default="",
                    help="复用既有权重里的某些语言，形如 zh=<path>。"
                         "用于「英文重训、中文保留」这类分语言决策，"
                         "避免为了修一侧把另一侧指标打回去。")
    ap.add_argument("--aug", type=int, default=2,
                    help="训练集增强倍数（0=关闭，仅作用训练侧）")
    a = ap.parse_args()

    rows = load_datasets()
    parts = stratified_split(rows, train_frac=a.train_frac,
                             calib_frac=a.calib_frac, seed=a.seed)
    t0 = time.time()
    # **分语言训练两个模型**：混训时英文 1199 条会把中文 285 条淹没
    # （实测中文留出 FPR 0.065 下不来），分语言后中文 0.90/0.02。
    models_all = {lang: _train_lang(parts, lang, aug_times=a.aug)
                  for lang in ("zh", "en")}
    # 分语言复用：某些语言的既有模型更优时（例如中文 fpr 0 vs 0.20），
    # 不该为了修英文把它一起重训掉。--reuse-lang zh=<path> 显式指定。
    models = dict(models_all)
    reused = {}
    for item in (a.reuse_lang or "").split(","):
        if "=" not in item:
            continue
        lang, path = item.split("=", 1)
        lang, path = lang.strip(), path.strip()
        if not lang or not os.path.isfile(path):
            continue
        src = json.load(open(path, encoding="utf-8"))
        if lang in (src.get("models") or {}):
            mm = dict(src["models"][lang])
            # 权重文件里的 idf 是 list（省体积），内存里统一成 dict
            if isinstance(mm.get("idf"), list):
                mm["idf"] = dict(zip(mm["vocab"], mm["idf"]))
            models[lang] = mm
            reused[lang] = os.path.basename(path)

    def ev(lang, split):
        m = models[lang]
        return _eval(parts[split], m["vocab"], m["w"], m["b"],
                     m["threshold"], lang, m["idf"])

    rep = {
        "model": "per-language char n-gram(1-4) TF-IDF + semantic clusters + LR",
        "routing": "文本含 CJK >= 1 个字符 -> zh 模型，否则 en 模型",
        "thresholds": {lang: models[lang]["threshold"] for lang in models},
        "vocab": {lang: len(models[lang]["vocab"]) for lang in models},
        "split": {"train": len(parts["train"]), "calib": len(parts["calib"]),
                  "holdout": len(parts["holdout"]),
                  "by_language": {
                      lang: {k: sum(1 for r in parts[k] if r[2] == lang)
                             for k in ("train", "calib", "holdout")}
                      for lang in ("zh", "en")}},
        # **分语言单独报告**，不混算
        "zh_holdout": ev("zh", "holdout"),
        "en_holdout": ev("en", "holdout"),
        "zh_calib": ev("zh", "calib"),
        "en_calib": ev("en", "calib"),
        "zh_train": ev("zh", "train"),
        "en_train": ev("en", "train"),
        "train_seconds": round(time.time() - t0, 1),
        "note": "按 (language,label) 分层切分；阈值在 calib 集校准；"
                "holdout 未参与训练与调参；IDF 只在训练集统计",
    }
    out = {"version": "light-v4",
           "ngrams": list(NGRAMS),
           "split_seed": a.seed,
           # 切分参数必须落盘：否则下游评测无法复现同一 holdout，
           # 会拿训练集当留出集（实测把 fpr 从 0.045 虚报成 0.46）。
           "split": {"train_frac": a.train_frac, "calib_frac": a.calib_frac,
                     "seed": a.seed},
           "reused_langs": reused,
           "models": {lang: {"vocab": m["vocab"],
                             "idf": [round(x, 6) for x in
                                     (m["idf"][g] for g in m["vocab"])],
                             "w": m["w"], "b": m["b"],
                             "threshold": m["threshold"]}
                      for lang, m in models.items()}}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
