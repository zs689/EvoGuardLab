# -*- coding: utf-8 -*-
"""§5 演示：授权由结构化契约决定，正则只作附加信号。

两个必须成立的反例：
  1. 参数里出现危险字样（正则命中），但结构化检查全过 → 仍然 allow；
     正则命中被记进 regex_signals，不改变授权结论。
  2. 参数里没有任何危险特征（正则零命中），但域名/金额/路径/身份越界 → deny；
     说明「没认出攻击」不等于「有权执行」。

产物默认进 experiments/tool_contract_v1/，不覆盖冻结证据。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import tool_contract as TC  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "experiments", "tool_contract_v1")
POLICY = {"allowed_tools": ["kb.search", "order.get_order", "order.settle"],
          "write_tools": ["order.settle"]}

CASES = [
    {
        "id": "normal_read", "name": "正常只读查询",
        "agent": "support-agent", "tool": "kb.search",
        "args": {"query": "退货时限", "doc_id": "KB-01"},
        "expect": "allow", "why": "身份、资源 ID、方法都在契约内",
    },
    {
        "id": "regex_hit_but_authorized", "name": "正则命中但契约允许",
        "agent": "support-agent", "tool": "order.get_order",
        "args": {"order_id": "A1001", "note": "备注里写着 rm -rf / 请不要执行"},
        "expect": "allow",
        "why": "文本里有危险字样，正则会命中；但域名/路径/金额/资源/方法/身份全合规，"
               "正则不承担授权决策",
    },
    {
        "id": "no_regex_but_domain_denied", "name": "正则零命中但域名越界",
        "agent": "support-agent", "tool": "http.send",
        "args": {"url": "http://127.0.0.1:9/collect", "body": "订单明细"},
        "expect": "deny", "why": "文本没有任何攻击特征，但契约没登记身份、没允许域名",
    },
    {
        "id": "amount_over_cap", "name": "金额超过契约上限",
        "agent": "order", "tool": "order.settle",
        "args": {"order_id": "A1001", "total": 99999.0,
                 "approval_request_id": "appr_demo"},
        "expect": "deny", "why": "没有正则特征，但 99999 > 10000 上限",
    },
    {
        "id": "unknown_identity", "name": "未知身份",
        "agent": "nobody", "tool": "kb.search",
        "args": {"query": "x", "doc_id": "KB-01"},
        "expect": "deny", "why": "未知身份在 enforce 下拒绝，不能让输入文本自行扩权",
    },
    {
        "id": "write_without_approval", "name": "写操作缺审批请求 id",
        "agent": "order", "tool": "order.settle",
        "args": {"order_id": "A1001", "total": 100.0},
        "expect": "deny", "why": "写操作必须带 approval_request_id",
    },
    {
        "id": "unknown_contract", "name": "未登记的工具契约",
        "agent": "support-agent", "tool": "mystery.tool",
        "args": {}, "expect": "deny", "why": "没有契约就不能授权",
    },
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="结构化工具契约授权演示")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    args = ap.parse_args(argv)
    rows = []
    for c in CASES:
        r = TC.authorize(c["agent"], c["tool"], c["args"], mode="enforce",
                         policy=POLICY)
        # 同一批在 audit 下再跑一遍，确认「不拦但留原因」
        audit = TC.authorize(c["agent"], c["tool"], c["args"], mode="audit",
                             policy=POLICY)
        row = dict(c)
        row["decision"] = r["decision"]
        row["regex_signals"] = r.get("regex_signals", [])
        row["failed_checks"] = [x["field"] + "." + x["rule"]
                                for x in r["checks"] if not x["ok"]]
        row["reasons"] = r["reasons"]
        row["regex_only"] = r.get("regex_only", False)
        row["structural_only"] = r.get("structural_only",
                                       bool(row["failed_checks"]))
        row["audit_decision"] = audit.get("decision", "report")
        row["pass"] = r["decision"] == c["expect"]
        rows.append(row)

    data = {
        "version": 1, "contract_version": TC.CONTRACT_VERSION,
        "policy": POLICY, "mode": "enforce", "cases": rows,
        "passed": sum(1 for r in rows if r["pass"]), "total": len(rows),
        "note": "授权由结构化契约决定；正则命中只写进 regex_signals，不改变 decision。",
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "demo.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    for r in rows:
        print("  %-28s %-6s %s" % (r["name"], r["decision"],
                                   "通过" if r["pass"] else "未通过"))
    if data["passed"] != data["total"]:
        raise SystemExit("有场景未通过")
    return data


def _markdown(d):
    L = ["# 结构化工具契约授权（§5）", "",
         "> 契约版本 `%s` ｜ 通过 %d/%d" % (d["contract_version"],
                                          d["passed"], d["total"]),
         "> %s" % d["note"], "",
         "## 两个反例", "",
         "1. **正则命中但契约允许**：文本里有 `rm -rf /`，正则会命中，"
         "但结构化检查全过 → `allow`。命中只记进 `regex_signals`。",
         "2. **正则零命中但越界**：域名/金额/身份越界，正则什么都没抓到 → `deny`。"
         "「没认出攻击」不等于「有权执行」。", "",
         "## 逐场景", "",
         "| 场景 | 期望 | 实际 | 正则信号 | 结构化失败项 | audit 下 | 结果 |",
         "|---|---|---|---|---|---|---|"]
    for r in d["cases"]:
        L.append("| %s | %s | %s | %s | %s | %s | %s |"
                 % (r["name"], r["expect"], r["decision"],
                    "、".join(r["regex_signals"]) or "无",
                    "、".join(r["failed_checks"]) or "无",
                    r["audit_decision"], "通过" if r["pass"] else "未通过"))
        L.append("")
        L.append("  - 为什么：%s" % r["why"])
    L += ["", "## 契约字段", "",
          "身份 / 允许域名 / 资源 ID 格式 / 可写路径 / 最大金额 / 方法 / "
          "副作用类型 / 是否需要审批请求 id。", "",
          "## 边界", "",
          "- 本模块独立于 `security.guard_tool_args`，默认行为不变，351 项回归可比。",
          "- 正则只作附加信号，不承担授权决策；不得让输入文本、模型或经验自行扩大权限。",
          "- 未知身份在 `enforce` 下拒绝；`audit` 下只 `report` 并留原因。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
