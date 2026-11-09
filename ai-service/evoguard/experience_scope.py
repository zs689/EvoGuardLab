# -*- coding: utf-8 -*-
"""E03：给经验与检索加适用范围，再做「结构过滤 + BM25」。

推进计划 §9 E03：现有经验已绑定来源，但检索仍主要依赖结构条件。
先记录经验适用的**工具契约、数据版本、权限范围、有效期和撤销状态**，
再做结构过滤加 BM25。只有语义改写确实造成召回不足时才增加 embedding；
**相似度不能替代可信性** —— 撤销、过期、权限/契约/版本不匹配的记录
在打分之前就被剔除，分数再高也不进候选。

纯标准库实现（BM25 k1=1.5 / b=0.75，中文按 2-gram、ASCII 按词），
不引入 embedding、向量库或第三方依赖。
"""
import math
import re
import time
from collections import Counter

SCOPE_VERSION = "experience-scope-v1"
K1, B = 1.5, 0.75

REQUIRED_FIELDS = (
    "id", "text", "source_run", "tool_contract_version", "data_version",
    "permission_scope", "valid_until", "revoked",
)


def make_record(*, rid, text, source_run, tool_contract_version,
                data_version, permission_scope, ttl=24 * 3600.0,
                created_at=None, agent=None, fault=None, revoked=False):
    """生成一条带适用范围的经验记录。"""
    now = created_at if created_at is not None else time.time()
    return {
        "id": rid, "text": text, "source_run": source_run,
        "tool_contract_version": tool_contract_version,
        "data_version": data_version,
        "permission_scope": sorted(permission_scope or []),
        "created_at": now, "valid_until": now + float(ttl),
        "revoked": bool(revoked), "agent": agent, "fault": fault,
        "scope_version": SCOPE_VERSION,
    }


def structural_filter(record, context, now=None):
    """结构过滤：可信性判断，先于任何相似度计算。

    context 可给：allowed_tools / tool_contract_version / data_version /
    agent / fault。缺字段不放宽，只有显式匹配的记录才放行。
    """
    now = time.time() if now is None else now
    reasons = []
    for f in REQUIRED_FIELDS:
        if f not in record:
            reasons.append("缺适用范围字段 %s" % f)
    if reasons:
        return False, reasons
    if record.get("revoked"):
        reasons.append("经验已撤销")
    if record.get("valid_until") is not None and record["valid_until"] < now:
        reasons.append("经验已过期")
    if "tool_contract_version" in context and \
            record["tool_contract_version"] != context["tool_contract_version"]:
        reasons.append("工具契约版本不匹配")
    if "data_version" in context and \
            record["data_version"] != context["data_version"]:
        reasons.append("数据版本不匹配")
    if "allowed_tools" in context:
        declared = set(record["permission_scope"] or [])
        allowed = set(context["allowed_tools"] or [])
        if not declared.issubset(allowed):
            reasons.append("权限范围超出当前策略：%s"
                           % sorted(declared - allowed))
    for key in ("agent", "fault"):
        if key in context and record.get(key) is not None \
                and record.get(key) != context[key]:
            reasons.append("%s 不匹配" % key)
    return (not reasons), reasons


def tokens(text):
    """中文 2-gram + ASCII 词。纯规则分词，不用模型。"""
    s = str(text or "").lower()
    out = re.findall(r"[a-z0-9_]+", s)
    for seg in re.findall(r"[\u4e00-\u9fa5]+", s):
        if len(seg) == 1:
            out.append(seg)
        else:
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def bm25(query, docs, k1=K1, b=B):
    """标准 BM25。零分母给 0 分，不抛异常。"""
    q = tokens(query)
    toks = [tokens(d) for d in docs]
    n = len(toks)
    if n == 0 or not q:
        return []
    avg = sum(len(t) for t in toks) / n or 1.0
    df = Counter()
    for t in toks:
        for w in set(t):
            df[w] += 1
    scores = []
    for t in toks:
        tf = Counter(t)
        s = 0.0
        for w in q:
            f = tf.get(w, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(t) / avg))
        scores.append(round(s, 4))
    return scores


def retrieve(records, query, *, context=None, now=None, top_k=3):
    """先结构过滤，再 BM25 排序。返回候选与被剔除记录及原因。"""
    ctx = context or {}
    eligible, excluded = [], []
    for r in records:
        ok, why = structural_filter(r, ctx, now)
        if ok:
            eligible.append(r)
        else:
            excluded.append({"id": r.get("id"), "reasons": why})
    scores = bm25(query, [r.get("text", "") for r in eligible])
    ranked = [{"id": r["id"], "text": r["text"], "score": sc,
               "source_run": r.get("source_run"),
               "scope": {k: r.get(k) for k in
                         ("tool_contract_version", "data_version",
                          "permission_scope", "valid_until", "revoked")}}
              for r, sc in zip(eligible, scores)]
    ranked.sort(key=lambda x: (-x["score"], x["id"]))
    return {
        "version": SCOPE_VERSION,
        "query": query,
        "context": ctx,
        "eligible": len(eligible), "excluded": len(excluded),
        "results": ranked[:top_k], "excluded_records": excluded,
        "note": "结构过滤先于相似度；撤销/过期/权限或版本不匹配一律剔除，"
                "分数再高也不进候选。相似度不能替代可信性。",
        "embedding_used": False,
        "embedding_note": "只有语义改写确实造成召回不足时才增加 embedding；本轮未加。",
    }


def revoke(record):
    """撤销经验：返回一个新记录，不改原对象（保留历史证据）。"""
    out = dict(record)
    out["revoked"] = True
    return out
