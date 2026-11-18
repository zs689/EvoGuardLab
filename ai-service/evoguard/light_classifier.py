# -*- coding: utf-8 -*-
"""PROD-03：轻量注入分类器的**推理侧**（核心零依赖，纯标准库）。

权重由 scripts/train_light_classifier.py 训练导出（char 2-4gram + LR），
本模块只读 JSON + 算点积，不 import numpy / onnxruntime / 任何第三方包。

分级路由（方案要求）：
- p >= high (默认 0.85)：高置信注入 -> 直接判，不调 LLM（<30ms）
- p <= low  (默认 0.15)：高置信正常 -> 直接放行
- 中间灰区 -> 交回调用方做 4B 判官（本模块给出 need_llm=True）

**诚实边界**：
- 阈值来自训练内校准（约束 FPR<=0.05），报告里的留出数字是
  scripts/train_light_classifier.py 产物，不是本文件自评。
- 权重文件缺失时明确报 unavailable，**不静默放行**。
"""
import json
import math
import os
import re

try:
    from . import harmful_request_judge as _H
    from . import persona_jailbreak_judge as _PJ
except ImportError:
    import harmful_request_judge as _H                      # type: ignore
    import persona_jailbreak_judge as _PJ                   # type: ignore

DEFAULT_WEIGHTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "light_classifier_weights.json")
_CACHE = {}


def load(path=None):
    p = path or DEFAULT_WEIGHTS
    if p in _CACHE:
        return _CACHE[p]
    if not os.path.isfile(p):
        raise FileNotFoundError("轻量分类器权重不存在：%s（先跑 train_light_classifier.py）"
                                % p)
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    # 倒排索引：token -> (index, weight)，把稀疏点积从 O(vocab) 降到 O(命中数)
    inv = {g: (i, float(w)) for i, (g, w) in
           enumerate(zip(d["vocab"], d["w"])) if w}
    obj = {"inv": inv, "b": float(d["b"]),
           "threshold": float(d.get("threshold", 0.5)),
           "ngrams": tuple(d.get("ngrams", (2, 3, 4))),
           "path": p}
    _CACHE[p] = obj
    return obj


def _feats(text, ngrams):
    t = re.sub(r"\s+", " ", str(text or "").lower())
    out = {}
    for n in ngrams:
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            out[g] = out.get(g, 0) + 1
    return out


def score(text, *, weights_path=None):
    """返回注入概率 p（0..1）。纯 Python 点积。"""
    m = load(weights_path)
    fs = _feats(text, m["ngrams"])
    z = m["b"]
    for g, cnt in fs.items():
        hit = m["inv"].get(g)
        if hit:
            z += hit[1] * cnt
    return 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))


def classify(text, *, high=0.85, low=0.15, weights_path=None):
    """分级判定；返回 {p, decision, need_llm, threshold}。"""
    p = score(text, weights_path=weights_path)
    if p >= high:
        return {"p": round(p, 4), "decision": "block", "need_llm": False,
                "route": "light_high_confidence"}
    if p <= low:
        return {"p": round(p, 4), "decision": "allow", "need_llm": False,
                "route": "light_high_confidence"}
    return {"p": round(p, 4), "decision": "review", "need_llm": True,
            "route": "llm_fallback"}


def guard_input_light(text, *, high=0.85, low=0.15, judge=None,
                      weights_path=None):
    """可直接接主入口的守卫：高置信度直接定，灰区转双判官。

    judge=None 时灰区默认调 harmful + persona 两个 4B 判官（可注入替换）。
    """
    r = classify(text, high=high, low=low, weights_path=weights_path)
    if not r["need_llm"]:
        return {"decision": r["decision"], "route": r["route"],
                "p": r["p"], "attempted": False, "executed": False}
    if judge is not None:
        v = judge(text)
        verdict = "block" if (v.get("harmful") or
                              v.get("persona_jailbreak")) else "allow"
        return {"decision": verdict, "route": "llm_fallback(injected)",
                "p": r["p"], "attempted": True, "executed": False,
                "judge": v}
    # 默认：两个判官并联，error 转 review
    h = _H.HarmfulRequestJudge().judge(text)
    pj = _PJ.PersonaJailbreakJudge().judge(text)
    if h.get("error") or pj.get("error"):
        verdict = "review"
    elif h.get("harmful") or pj.get("persona_jailbreak"):
        verdict = "block"
    else:
        verdict = "allow"
    return {"decision": verdict, "route": "llm_fallback",
            "p": r["p"], "attempted": True, "executed": False,
            "harmful": h, "persona": pj}


def describe():
    return {"runtime_dependency": "none (stdlib only)",
            "weights": os.path.basename(DEFAULT_WEIGHTS),
            "default_thresholds": {"high": 0.85, "low": 0.15},
            "fallback": "harmful + persona 4B judges",
            "training_report": "experiments/light_classifier/train_result.json"}
