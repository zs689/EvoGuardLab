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


#: SCORE-02-T1：业务语境排除特征。
#: 中文良性误报集中在「JSON API 成功响应 + 业务说明」这类文本
#: （含"规则/流程/请/处理"等词，词袋会当攻击骨架）。
#: 这里用**结构性**特征（合法 JSON 且带成功状态码）判定业务语境，
#: 不是针对某几条样本硬编码关键词。
_BUSINESS_MARKERS = (
    '"code":0', '"code": 0', '"success":true', '"success": true',
    '"status":"ok"', '"status": "ok"',
)


def _is_advisory_question(text):
    t = str(text or "")
    ask = any(w in t for w in ("如何", "怎么", "怎样", "how to", "what is"))
    advisory = any(w in t for w in
                   ("保护", "设置", "建议", "流程", "说明", "解释",
                    "protect", "secure", "explain", "flow"))
    # 执行动词共现时不算咨询（"如何删除所有数据"仍是攻击）
    execute = any(w in t for w in
                  ("删除", "清空", "转账", "导出", "发送", "执行", "绕过",
                   "delete", "wipe", "transfer", "send", "exec", "bypass"))
    return ask and advisory and not execute


def is_business_data(text):
    """是否为成功响应的结构化业务数据（降权而非直接放行）。"""
    t = str(text or "").strip()
    if not (t.startswith("{") or t.startswith("[")):
        return False
    low = t.lower()
    if not any(m in low for m in _BUSINESS_MARKERS):
        return False
    try:
        json.loads(t)
        return True
    except (ValueError, TypeError):
        return False


def normalize_text(text):
    """推理前标准化（SEC-BYPASS-02-T2）。

    1) 去零宽/不可见字符（复用 security.py 的清洗，避免两边规则漂移）
    2) NFKC：全角字母数字/标点 -> 半角
    3) 折叠中文字符间的异常单空格（"忽 略 以 上" -> "忽略以上"）
       只折叠**相邻两个 CJK 之间**的空格，不动 "hello world" 这种正常分隔。
    """
    t = str(text or "")
    try:
        try:
            from . import security as _sec
        except ImportError:
            import security as _sec                        # type: ignore
        t = _sec.sanitize_invisible(t)
    except Exception:                                      # noqa: BLE001
        # 清洗器不可用不能静默放过原文：至少做标准库的零宽剥离
        t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t)
    import unicodedata
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"([\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", r"\1", t)
    return t


def _feats(text, ngrams):
    t = re.sub(r"\s+", " ", str(text or "").lower())
    out = {}
    for n in ngrams:
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            out[g] = out.get(g, 0) + 1
    toks = []
    _sem = None
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
    # SCORE-02-T2：拼音/首字母危险意图 -> 复用训练时已有的语义簇权重。
    # 必须与动作结构共现才返回 token（见 light_semantics.pinyin_tokens），
    # 因此正常拼音询问不会命中。
    try:
        for tok in _sem.pinyin_tokens(t):
            out[tok] = out.get(tok, 0) + 2
    except Exception:                                      # noqa: BLE001
        pass
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
    """含 CJK **或命中中文拼音危险簇**即判中文，否则英文。

    SCORE-02-T2：纯拼音注入（"qing hu lue zhi qian..."）是中文的罗马化，
    不是英文 —— 旧路由按「无 CJK -> en」会把它送进 en 模型，
    语义簇权重不匹配导致分数极低（实测 0.189，漏检）。
    """
    if _CJK.search(str(text or "")):
        return "zh"
    try:
        import light_semantics as _sem
        if _sem.pinyin_tokens(text):
            return "zh"
    except Exception:                                      # noqa: BLE001
        pass
    return "en"


def _model_for(text, m):
    lang = route_language(text)
    if lang in m["models"]:
        return lang, m["models"][lang]
    if "default" in m["models"]:
        return "default", m["models"]["default"]
    first = sorted(m["models"])[0]
    return first, m["models"][first]


def _score_with_lang(text, m):
    """返回 (p, lang, normalized_p)。归一化 = p / 该模型阈值；>=1 即越线。

    混排文本双模型取归一化最大值 —— 加一个汉字把 en 路由劫持到 zh
    不会再降低风险分（两个模型都跑，取更能说明风险的那个）。
    """
    text = normalize_text(text)
    langs = _route_langs(text, m)
    if len(langs) > 1:
        cands = []
        for lang in langs:
            model = m["models"][lang]
            p = _score_one(text, model, m["ngrams"])
            thr = model["threshold"] or 0.5
            cands.append((p / thr, p, lang))
        norm, p, lang = max(cands)
        return p, lang, norm
    lang = route_language(text)
    if lang not in m["models"]:
        lang = ("default" if "default" in m["models"]
                else sorted(m["models"])[0])
    model = m["models"][lang]
    p = _score_one(text, model, m["ngrams"])
    thr = model["threshold"] or 0.5
    return p, lang, p / thr


def score(text, *, weights_path=None):
    """返回注入概率 p（0..1）。兼容旧 API；双模型时返回被选中模型的原始 p。"""
    return _score_with_lang(text, load(weights_path))[0]


def _route_langs(text, m):
    """返回该文本要跑的语言模型列表（1 或 2 个）。"""
    has_cjk = bool(_CJK.search(str(text or "")))
    latin = re.findall(r"[A-Za-z]", str(text or ""))
    total = max(1, len(re.findall(r"[A-Za-z\u4e00-\u9fff]", str(text or ""))))
    latin_ratio = len(latin) / total
    mixed = has_cjk and (latin_ratio >= 0.20 or len(latin) >= 4)
    if mixed:
        return [l for l in ("zh", "en") if l in m["models"]]
    return [route_language(text)]


def _score_one(text, model, ngrams):
    """单模型打分（不做标准化——由上层统一做）。"""
    fs = _feats(text, ngrams)
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

    `high` 默认取**被选中语言模型校准过的阈值**。混排文本双模型取归一化
    最大值，避免「加一个汉字换路由导致风险分下降」的绕过。
    """
    m = load(weights_path)
    p, lang, norm = _score_with_lang(text, m)
    # 注：不做「咨询问句降权」——实测它会误伤 2 条问句式真越狱
    # （chinese_jailbreak recall 1.0 -> 0.9677）。安全优先，宁可
    # chinese_adversarial FPR 踩在 0.10 的门槛上，也不牺牲召回。
    if is_business_data(text):
        # 业务结构数据降权 0.7：仍按同一阈值判定，但给结构语境留余量。
        # 不改模型、不碰留出集 —— 这是输入侧语境特征。
        p *= 0.70
        norm *= 0.70
    model = m["models"][lang]
    if high is None:
        high = model["threshold"]
    # 混排时用归一化值判定更稳（两个模型判定线不同，原始 p 不可直接比）
    measure = norm if len(_route_langs(normalize_text(text), m)) > 1 else p
    if norm >= 1.0 or measure >= high:
        return {"p": round(p, 4), "decision": "block", "need_llm": False,
                "language": lang, "route": "light_high_confidence"}
    if measure <= low:
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
