# -*- coding: utf-8 -*-
"""注入打分 v2：逐字段过滤 + 重叠分窗取 max + FP 预算标定阈值（纯标准库）。

融合的公开方法（均已联网核实，只融方法、不引入权重或第三方包）：

- **StackOne SFE**（一手博客）：对 JSON 每个 string 字段按**形状**判 pass/drop，
  UUID / ISO 时间戳 / opaque token / 版本号 / ID 形字符串不送内容分类器；
  实测 drop ~45% 字段，FP 1.7% → 0.2%。上次 E3 把「逐字段独立打分」错实现成
  「拼接后整体一次打分」，这里按原意重做。
- **Meta Llama Prompt Guard 2**（模型卡 / 文档，二手）：512-token 窗口、长输入
  **重叠切分 + 取 max malicious score**；报告 AUC 与固定 FPR 预算下的召回。
  权重是 gated 的 Llama 4 Community License 且需 transformers，本项目**不引入**，
  只用它的窗口与评价口径。
- **DeFuse**（arXiv:2609.22510v1，一手 PDF）：max-pooling 下文档级误报随窗口数
  按 `1−(1−p)^N` 复合；固定阈值下的 F1 只量「阈值放得对不对」，应报 AUC 等
  阈值无关量，并用**长度感知 / 预算感知**阈值。本项目分数是 `z/√n` 实数域，
  不能套它的概率公式，改为在 calibration 上按窗口数分桶、反推每桶阈值。
- **The Guardrail Cries Wolf**（Zenodo 页面摘要）：截断扫描对末尾 payload 几乎
  全瞎（8.3%），分窗能恢复到 75%，但长 benign 误报从 6% 升到 18% —— 所以本模块
  必须同时输出「窗口数 / 丢弃比例 / 阈值」，方便上层如实报代价。

刻意不做：不换模型、不调既有 `deep_scan` 权重、不装包、不联网推理。
"""
import json
import re

import deep_scan as DS

STRUCTURAL_KEYS = {
    "id", "created_at", "updated_at", "request_token", "version",
    "status", "owner_id", "scope", "applied", "is_template", "env", "ts",
    "request_id", "trace_id", "checksum", "hash",
}

_VALUE_PATTERNS = [
    re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
               r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"),
    re.compile(r"^[0-9a-fA-F]{16,}$"),
    re.compile(r"^v?\d+(\.\d+){1,3}$"),
    re.compile(r"^[A-Za-z]+-?\d{3,}$"),
    re.compile(r"^[A-Z][A-Z0-9_]{2,}$"),
]

DEFAULT_WINDOW = 512
DEFAULT_OVERLAP = 64
DEFAULT_ALPHA = 0.05          # 目标文档级误报预算（沿用项目既有 5% 发布线）


def is_structural(key, value):
    """字段是否「不可能承载注入指令」，按形状而非业务来源判断（可泛化到未见 schema）。"""
    k = str(key).lower()
    if k in STRUCTURAL_KEYS or k.endswith("_id") or k.endswith("_at") \
            or k.endswith("_token") or k.endswith("_key"):
        return True
    return any(p.match(str(value)) for p in _VALUE_PATTERNS)


def filter_fields(payload):
    """返回 (kept, dropped, drop_ratio)。kept 是 [(key, value)]，逐字段独立送分。"""
    if not isinstance(payload, dict):
        return [("text", str(payload))], [], 0.0
    kept, dropped = [], []
    for k, v in payload.items():
        (dropped if is_structural(k, v) else kept).append((k, str(v)))
    total = len(payload) or 1
    return kept, dropped, round(len(dropped) / total, 3)


def windows(text, size=DEFAULT_WINDOW, overlap=DEFAULT_OVERLAP):
    """重叠切窗，保证末尾 payload 至少完整落在一个窗口里。"""
    if not text or not text.strip():
        return []
    if size <= overlap or size <= 0:
        raise ValueError("窗口必须大于重叠长度")
    if len(text) <= size:
        return [text]
    step = size - overlap
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        if i + size >= len(text):
            break
        i += step
    return out


def score_document(model, payload, size=DEFAULT_WINDOW,
                   overlap=DEFAULT_OVERLAP, use_filter=True):
    """逐字段 → 每字段重叠分窗 → 取所有窗口分数的 max 作为文档分。

    返回诊断字段：窗口数、保留/丢弃字段数、丢弃比例。没有任何窗口时
    score 记 None（不是 0），避免把「无可评分内容」伪装成干净。
    """
    if use_filter:
        kept, dropped, drop_ratio = filter_fields(payload)
    else:
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False)
        kept, dropped, drop_ratio = [("text", text)], [], 0.0

    scores, n_windows = [], 0
    for _key, value in kept:
        for w in windows(value, size, overlap):
            scores.append(DS.score(model, w))
            n_windows += 1
    return {
        "score": max(scores) if scores else None,
        "n_windows": n_windows,
        "kept_fields": len(kept),
        "dropped_fields": len(dropped),
        "drop_ratio": drop_ratio,
        "max_window_scores": sorted(scores, reverse=True)[:3],
    }


def _bucket(n_windows):
    """按窗口数分桶，实现 DeFuse 要求的长度感知阈值。"""
    if n_windows <= 2:
        return "short"
    if n_windows <= 8:
        return "medium"
    return "long"


def _quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def calibrate(model, benign_payloads, alpha=DEFAULT_ALPHA,
              size=DEFAULT_WINDOW, overlap=DEFAULT_OVERLAP, use_filter=True):
    """只在 calibration（干净）文档上定阈值：使文档级 FP ≈ alpha。

    按窗口数分桶各定一个阈值，抵消 max-pooling 的 `1-(1-p)^N` 复合效应。
    桶内样本不足时回退到全局阈值。绝不看任何正例。
    """
    by_bucket, all_scores = {}, []
    for p in benign_payloads:
        r = score_document(model, p, size, overlap, use_filter)
        if r["score"] is None:
            continue
        all_scores.append(r["score"])
        by_bucket.setdefault(_bucket(r["n_windows"]), []).append(r["score"])
    global_t = _quantile(all_scores, 1.0 - alpha)
    thresholds = {}
    for b, vals in by_bucket.items():
        # 桶太小就别自己定，免得过拟合
        thresholds[b] = (_quantile(vals, 1.0 - alpha)
                         if len(vals) >= 20 else global_t)
    return {
        "alpha": alpha,
        "global": global_t,
        "by_bucket": thresholds,
        "n_calibration": len(all_scores),
        "bucket_sizes": {b: len(v) for b, v in by_bucket.items()},
        "note": "阈值只在干净 calibration 上标定；test 只报数，不回头调。",
    }


def threshold_for(cal, n_windows):
    return cal["by_bucket"].get(_bucket(n_windows), cal["global"])


def classify(model, payload, cal, size=DEFAULT_WINDOW,
             overlap=DEFAULT_OVERLAP, use_filter=True):
    r = score_document(model, payload, size, overlap, use_filter)
    t = threshold_for(cal, r["n_windows"])
    r["threshold"] = t
    r["flagged"] = bool(r["score"] is not None and t is not None
                        and r["score"] > t)
    return r


def auc(scores, labels):
    """阈值无关的 AUC（Mann-Whitney，含并列按 0.5 计）。O(n²) 但纯标准库。"""
    pos = [s for s, y in zip(scores, labels) if y == 1 and s is not None]
    neg = [s for s, y in zip(scores, labels) if y == 0 and s is not None]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return round(wins / (len(pos) * len(neg)), 4)


def metrics_at_threshold(scores, labels, threshold):
    tp = fp = tn = fn = 0
    for s, y in zip(scores, labels):
        hit = s is not None and threshold is not None and s > threshold
        if y == 1:
            tp += int(hit)
            fn += int(not hit)
        else:
            fp += int(hit)
            tn += int(not hit)
    n_pos, n_neg = tp + fn, fp + tn
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "recall": round(tp / n_pos, 4) if n_pos else None,
        "fp_rate": round(fp / n_neg, 4) if n_neg else None,
        "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
    }
