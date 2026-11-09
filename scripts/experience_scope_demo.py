# -*- coding: utf-8 -*-
"""E03 演示：结构过滤先于 BM25，相似度不能替代可信性。

关键反例：一条**文本完全匹配查询但已撤销**的经验，BM25 分数最高，
却必须在打分之前就被剔除，绝不进候选。
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import experience_scope as ES  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "experiments", "e03_v1")
NOW = 1_000_000.0
CTX = {
    "allowed_tools": ["kb.search_all", "kb.tier_lookup"],
    "tool_contract_version": "tool-contract-v1",
    "data_version": "kb-v3",
    "agent": "xref",
}


def _records():
    base = dict(source_run="run_demo", tool_contract_version="tool-contract-v1",
                data_version="kb-v3",
                permission_scope=["kb.search_all", "kb.tier_lookup"],
                created_at=NOW - 1000, agent="xref")
    return [
        ES.make_record(rid="exp_valid_new",
                       text="退货政策 七天无理由 退款时限",
                       ttl=10000, **base),
        ES.make_record(rid="exp_valid_old",
                       text="旧版退货政策 三天内退货",
                       ttl=10000, **base),
        # 反例：文本与查询逐字一致（BM25 最高分），但已撤销
        ES.revoke(ES.make_record(
            rid="exp_revoked_best_match",
            text="新版退货政策 按文档版本号选证据",
            ttl=10000, **base)),
        # 过期
        ES.make_record(rid="exp_expired",
                       text="退货政策 新版 文档版本号", ttl=10,
                       **dict(base, created_at=NOW - 100)),
        # 数据版本不匹配
        ES.make_record(rid="exp_wrong_data_version",
                       text="新版退货政策 文档版本号", ttl=10000,
                       **dict(base, data_version="kb-v2")),
        # 工具契约版本不匹配
        ES.make_record(rid="exp_wrong_contract",
                       text="退货政策 新版", ttl=10000,
                       **dict(base, tool_contract_version="tool-contract-v0")),
        # 权限超出当前策略
        ES.make_record(rid="exp_privilege_overflow",
                       text="退货政策 新版 越权", ttl=10000,
                       **dict(base, permission_scope=["kb.search_all", "shell.exec"])),
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description="E03 经验适用范围与检索演示")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    args = ap.parse_args(argv)
    records = _records()
    query = "新版退货政策 按文档版本号选证据"
    ret = ES.retrieve(records, query, context=CTX, now=NOW, top_k=3)

    # 反例：把撤销标记去掉，同一条记录会拿到最高分 —— 证明分数不能替代可信性。
    unrevoked = [dict(r, revoked=False) if r["id"] == "exp_revoked_best_match"
                 else r for r in records]
    counterfactual = ES.retrieve(unrevoked, query, context=CTX, now=NOW,
                                 top_k=3)
    excluded_ids = {x["id"] for x in ret["excluded_records"]}
    revoked_excluded = "exp_revoked_best_match" in excluded_ids
    cf_top = counterfactual["results"][0]["id"] if counterfactual["results"] else None
    trustworthy_top = ret["results"][0]["id"] if ret["results"] else None

    data = {
        "version": 1, "scope_version": ES.SCOPE_VERSION,
        "query": query, "context": CTX, "now": NOW,
        "retrieval": ret,
        "counterfactual_top_if_revocation_ignored": cf_top,
        "actual_top": trustworthy_top,
        "revoked_record_excluded": revoked_excluded,
        "pass": revoked_excluded and cf_top == "exp_revoked_best_match"
                and trustworthy_top != "exp_revoked_best_match",
        "note": "结构过滤先于 BM25；撤销/过期/版本/权限不匹配一律剔除，"
                "分数再高也不进候选。相似度不能替代可信性。",
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "demo.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("  可进候选 %d / 被剔除 %d ｜ 实际 top=%s ｜ 忽略撤销会 top=%s ｜ 反例 %s"
          % (ret["eligible"], ret["excluded"], trustworthy_top, cf_top,
             "通过" if data["pass"] else "未通过"))
    if not data["pass"]:
        raise SystemExit("反例未成立")
    return data


def _markdown(d):
    r = d["retrieval"]
    L = ["# E03：经验适用范围 + 结构过滤 + BM25", "",
         "> 版本 `%s` ｜ %s" % (d["scope_version"], d["note"]), "",
         "## 反例（必须成立）", "",
         "- 查询：`%s`" % d["query"],
         "- 一条**文本最像但已撤销**的记录：实际被剔除 = **%s**"
         % d["revoked_record_excluded"],
         "- 若忽略撤销标记，它会排第一：`%s`"
         % d["counterfactual_top_if_revocation_ignored"],
         "- 结构过滤后实际第一：`%s`" % d["actual_top"],
         "- 结论：BM25 分数不能替代可信性，撤销/过期/版本/权限必须先过滤。", "",
         "## 检索结果", "",
         "| 排名 | 经验 id | BM25 | 来源 run |", "|---|---|---|---|"]
    for i, x in enumerate(r["results"], 1):
        L.append("| %d | `%s` | %s | %s |"
                 % (i, x["id"], x["score"], x["source_run"]))
    L += ["", "## 被剔除的记录与原因", "",
          "| id | 原因 |", "|---|---|"]
    for x in r["excluded_records"]:
        L.append("| `%s` | %s |" % (x["id"], "；".join(x["reasons"])))
    L += ["", "## 边界", "",
          "- 纯标准库 BM25（k1=1.5 / b=0.75），中文按 2-gram、ASCII 按词。",
          "- **未使用 embedding**：只有语义改写确实造成召回不足时才加；相似度不能替代可信性。",
          "- 适用范围字段：工具契约版本 / 数据版本 / 权限范围 / 有效期 / 撤销状态 / 来源 run。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
