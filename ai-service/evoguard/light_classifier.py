# -*- coding: utf-8 -*-
"""PROD-03 / FIX-C2：轻量注入分类器的**推理侧**（核心零依赖，纯标准库）。

权重由 scripts/train_light_classifier.py 训练导出（char 1-4gram + 语义簇
+ TF-IDF + LR），本模块只读 JSON + 算点积，不 import numpy/onnxruntime。

**分语言路由**：文本含任意 CJK 字符走 `zh` 模型，否则走 `en` 模型。
为什么必须分开：混训时英文 1199 条会把中文 285 条淹没 —— 实测中文留出
FPR 卡在 0.065 下不来；分语言后中文 0.8356/0.0217、英文 0.9819/0.0390。

**分级路由**：
- p >= high (默认 0.85)：高置信注入 -> 直接拦，不调 LLM
- p <= low  (默认 0.15)：高置信正常 -> 直接放行
- 灰区 -> need_llm=True，交回 4B 判官

**诚实边界**：阈值来自**训练内部 calib 集**校准（不看 holdout）；
留出数字见 experiments/light_classifier/train_result.json，不由本文件自评。
权重缺失时明确报错，不静默放行。
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
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _feats(text, ngrams):
    t = re.sub(r"\s+", " ", str(text or "").lower())
    out = {}
    for n in ngrams:
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            out[g] = out.get(g, 0) + 1
    toks = []
    try:
        from . import light_semantics as _sem
        toks = _sem.tokens(t)
    except ImportError:
        try:
            import light_semantics as _sem                # type: ignore
            toks = _sem.tokens(t)
        except ImportError:
            toks = []
    for tok in toks:
        out[tok] = out.get(tok, 0) + 2        # 与训练侧同一权重
    return out


def load(path=None):
    """加载并缓存权重；返回 {models:{lang:{inv,b,threshold}}, ngrams}。"""
    p = path or DEFAULT_WEIGHTS
    if p in _CACHE:
        return _CACHE[p]
    if not os.path.isfile(p):
        raise FileNotFoundError(
            "轻量分类器权重不存在：%s（先跑 train_light_classifier.py）" % p)
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    ngrams = tuple(d.get("ngrams") or (1, 2, 3, 4))
    models = {}
    raw_models = d.get("models")
    if raw_models:
        for lang, m in raw_models.items():
            vocab, ws = m["vocab"], m["w"]
            idf_list = m.get("idf") or [1.0] * len(vocab)
            # 倒排：token -> (w, idf)；点积时 O(命中数)
            inv = {g: (float(w), float(idf_list[i]))
                   for i, (g, w) in enumerate(zip(vocab, ws)) if w}
            models[lang] = {"inv": inv, "b": float(m["b"]),
                            "threshold": float(m.get("threshold", 0.5))}
    else:
        # 兼容旧单模型格式（light-v1/v2/v3）
        vocab, ws = d["vocab"], d["w"]
        idf_list = d.get("idf") or [1.0] * len(vocab)
        models["default"] = {
            "inv": {g: (float(w), float(idf_list[i]))
                    for i, (g, w) in enumerate(zip(vocab, ws)) if w},
            "b": float(d["b"]),
            "threshold": float(d.get("threshold", 0.5))}
    obj = {"models": models, "ngrams": ngrams, "path": p,
           "version": d.get("version")}
    _CACHE[p] = obj
    return obj


def route_language(text):
    """含 CJK 即判中文，否则英文。"""
    return "zh" if _CJK.search(str(text or "")) else "en"


def _model_for(text, m):
    lang = route_language(text)
    if lang in m["models"]:
        return lang, m["models"][lang]
    if "default" in m["models"]:
        return "default", m["models"]["default"]
    first = sorted(m["models"])[0]
    return first, m["models"][first]


def score(text, *, weights_path=None):
    """返回注入概率 p（0..1）。纯 Python 稀疏点积 + TF-IDF 归一化。"""
    m = load(weights_path)
    lang, model = _model_for(text, m)
    fs = _feats(text, m["ngrams"])
    num = 0.0
    sq = 0.0
    for g, cnt in fs.items():
        hit = model["inv"].get(g)
        if hit:
            w, idf = hit
            val = cnt * idf
            num += val * w
            sq += val * val
    norm = math.sqrt(sq) or 1.0
    z = model["b"] + num / norm
    return 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))


def classify(text, *, high=None, low=0.15, weights_path=None):
    """分级判定；返回 {p, decision, need_llm, language, route}。

    `high` 默认取**该语言模型校准过的阈值**（calib 集上 FPR<=0.05 的最大
    recall 点）。留出集的 FPR 就是在这个阈值下测出来的，写死 0.85 会让
    绝大多数真注入落进灰区、全部转给 4B 判官（慢且没必要）。
    """
    m = load(weights_path)
    lang, model = _model_for(text, m)
    if high is None:
        high = model["threshold"]
    p = score(text, weights_path=weights_path)
    if p >= high:
        return {"p": round(p, 4), "decision": "block", "need_llm": False,
                "language": lang, "route": "light_high_confidence"}
    if p <= low:
        return {"p": round(p, 4), "decision": "allow", "need_llm": False,
                "language": lang, "route": "light_high_confidence"}
    return {"p": round(p, 4), "decision": "review", "need_llm": True,
            "language": lang, "route": "llm_fallback"}


def guard_input_light(text, *, high=None, low=0.15, judge=None,
                      weights_path=None):
    """主入口可用的守卫：高置信度直接定，灰区转双判官。"""
    r = classify(text, high=high, low=low, weights_path=weights_path)
    if not r["need_llm"]:
        return {"decision": r["decision"], "route": r["route"],
                "p": r["p"], "language": r["language"],
                "attempted": False, "executed": False}
    if judge is not None:
        v = judge(text)
        verdict = "block" if (v.get("harmful") or
                              v.get("persona_jailbreak")) else "allow"
        return {"decision": verdict, "route": "llm_fallback(injected)",
                "p": r["p"], "language": r["language"], "attempted": True,
                "executed": False, "judge": v}
    h = _H.HarmfulRequestJudge().judge(text)
    pj = _PJ.PersonaJailbreakJudge().judge(text)
    if h.get("error") or pj.get("error"):
        verdict = "review"
    elif h.get("harmful") or pj.get("persona_jailbreak"):
        verdict = "block"
    else:
        verdict = "allow"
    return {"decision": verdict, "route": "llm_fallback",
            "p": r["p"], "language": r["language"], "attempted": True,
            "executed": False, "harmful": h, "persona": pj}


def describe():
    return {"runtime_dependency": "none (stdlib only)",
            "weights": os.path.basename(DEFAULT_WEIGHTS),
            "routing": "CJK -> zh model, else en model",
            "default_thresholds": {"high": "calibrated per language",
                                   "low": 0.15},
            "fallback": "harmful + persona 4B judges",
            "training_report": "experiments/light_classifier/train_result.json",
            "holdout": "zh n=119 recall 0.8356 FPR 0.0217 / "
                       "en n=499 recall 0.9819 FPR 0.0390"}
