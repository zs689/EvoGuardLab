# -*- coding: utf-8 -*-
"""Q02 权限专项评测：白名单只能来自**事先冻结**的 Agent 策略。

问题（推进计划 §7 Q02）：`eval_labeled.py` 把轨迹实际用到的工具直接填进
`allowed_tools`，可消除结构评测的无配置噪声，却会让「未经授权但已经执行的工具」
被评测端自动视为允许 —— 越权永远测不出来。

这里做两件事：
  1. 从 `tests/fixtures/labeled/policies.json` 读冻结策略，对每条标注轨迹跑权限专项；
     没有独立策略的样本标 `unjudgeable_permission`，**不计入分母也不静默通过**。
  2. 同一批轨迹再用「反推白名单」跑一遍，把两种口径的差异逐条列出来。

额外加入一条受控对照样本（`trace_attack.json`，含 http.post / shell.exec），
作为反例：反推白名单下不报越权，冻结策略下必须报。

跑法：
    python scripts/eval_permission.py                  # 默认写新目录
    python scripts/eval_permission.py --out /tmp/perm
产物：
    {--out}/{report.md,result.json}
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import config, loaders  # noqa: E402
from evoguard_inspect.rules import registry, trace_rules  # noqa: E402

LABELS = os.path.join(ROOT, "tests", "fixtures", "labeled", "labels.json")
POLICIES = os.path.join(ROOT, "tests", "fixtures", "labeled", "policies.json")
ATTACK = os.path.join(ROOT, "tests", "fixtures", "trace_attack.json")
DEFAULT_OUT = os.path.join(ROOT, "experiments", "permission_eval_v1")
FROZEN_OUT = os.path.join(ROOT, "experiments", "labeled_eval")
UNAUTH_RULE = "tool_not_allowed"


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def fingerprint(obj):
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _cfg(allowed_tools):
    d = dict(config.DEFAULTS)
    d["allowed_tools"] = sorted(set(allowed_tools))
    return config.Config(**d)


def _run(trace, allowed_tools):
    issues = trace_rules.check_all([trace], _cfg(allowed_tools), registry.select())
    hits = sorted({i["rule_id"] for i in issues
                   if i["rule_id"] == UNAUTH_RULE})
    return {"unauthorized_hits": hits,
            "all_rules": sorted({i["rule_id"] for i in issues})}


def _reverse_tools(trace):
    return sorted({s.tool or s.name for s in trace.tools()})


def main(argv=None):
    ap = argparse.ArgumentParser(description="权限专项评测（冻结策略，不从轨迹反推）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--labels", default=LABELS, help="标注集清单（测试可注入）")
    ap.add_argument("--policies", default=POLICIES, help="冻结策略（测试可注入）")
    args = ap.parse_args(argv)
    if os.path.abspath(args.out) == os.path.abspath(FROZEN_OUT):
        raise SystemExit("拒绝写入 v1 冻结报告目录；请用 --out 指定新目录")

    with open(args.labels, encoding="utf-8") as f:
        doc = json.load(f)
    with open(args.policies, encoding="utf-8") as f:
        policies = json.load(f)

    records = []
    for e in doc["entries"]:
        agent = e.get("agent")
        policy = policies["policies"].get(agent)
        rec = {"key": e["key"], "agent": agent, "label": e["label"],
               "file": e["file"], "source": "labeled_set"}
        if policy is None:
            rec.update({"policy_status": "missing", "judgeable": False,
                        "reason": "没有事先冻结的独立授权策略，权限专项不可评"})
            records.append(rec)
            continue
        traces = loaders.load_file(os.path.join(ROOT, e["file"]))
        tr = traces[0]
        frozen = _run(tr, policy)
        reverse = _run(tr, _reverse_tools(tr))
        rec.update({
            "policy_status": "frozen", "judgeable": True,
            "frozen_allowed_tools": sorted(policy),
            "reverse_allowed_tools": _reverse_tools(tr),
            "tools_used": _reverse_tools(tr),
            "frozen": frozen, "reverse": reverse,
            "divergence": bool(frozen["unauthorized_hits"]) !=
                          bool(reverse["unauthorized_hits"]),
        })
        records.append(rec)

    # 受控反例：一条真的用了白名单外工具的轨迹。
    attack_tr = loaders.load_file(ATTACK)[0]
    atk_policy = policies["policies"].get("support-agent", [])
    atk_frozen = _run(attack_tr, atk_policy)
    atk_reverse = _run(attack_tr, _reverse_tools(attack_tr))
    records.append({
        "key": "counterexample_trace_attack", "agent": "support-agent",
        "label": "failed", "file": os.path.relpath(ATTACK, ROOT).replace(os.sep, "/"),
        "source": "controlled_counterexample", "policy_status": "frozen",
        "judgeable": True,
        "frozen_allowed_tools": sorted(atk_policy),
        "reverse_allowed_tools": _reverse_tools(attack_tr),
        "tools_used": _reverse_tools(attack_tr),
        "frozen": atk_frozen, "reverse": atk_reverse,
        "divergence": bool(atk_frozen["unauthorized_hits"]) !=
                      bool(atk_reverse["unauthorized_hits"]),
        "note": "反例：轨迹里执行了白名单外的工具，反推白名单会把它当成允许",
    })

    judged = [r for r in records if r["judgeable"]]
    missing = [r for r in records if not r["judgeable"]]
    frozen_hits = [r for r in judged if r["frozen"]["unauthorized_hits"]]
    reverse_hits = [r for r in judged if r["reverse"]["unauthorized_hits"]]
    divergence = [r for r in judged if r["divergence"]]

    result = {
        "version": 1,
        "protocol": "permission-eval-v1",
        "policy_protocol": policies["protocol"],
        "policy_fingerprint": fingerprint(policies["policies"]),
        "n_total": len(records),
        "n_judgeable": len(judged),
        "n_unjudgeable": len(missing),
        "applicable_rate": round(len(judged) / len(records), 3) if records else None,
        "unauthorized_detected_frozen": len(frozen_hits),
        "unauthorized_detected_reverse": len(reverse_hits),
        "divergence_cases": [r["key"] for r in divergence],
        "counterexample": {
            "frozen_reports": bool(atk_frozen["unauthorized_hits"]),
            "reverse_reports": bool(atk_reverse["unauthorized_hits"]),
            "tools_used": _reverse_tools(attack_tr),
        },
        "records": records,
        "limits": [
            "白名单来自手写冻结策略，不是从已执行轨迹反推；这是权限专项的必要条件。",
            "没有独立策略的样本标不可评、不计入分母，绝不静默通过。",
            "结构评测仍可用反推白名单消除无配置噪声，但必须与本专项分开标注，不能混成一个数。",
            "37 条标注轨迹全部来自本仓库 mock 执行，不是外部真实业务轨迹；"
            "独立标注者仍缺，见 docs/external-labeling-protocol.md。",
        ],
    }

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(result))
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("  可评 %d/%d（适用率 %s）｜ 冻结策略检出越权 %d ｜ 反推白名单检出 %d ｜ 差异 %d"
          % (result["n_judgeable"], result["n_total"], result["applicable_rate"],
             result["unauthorized_detected_frozen"],
             result["unauthorized_detected_reverse"], len(divergence)))
    return result


def _markdown(r):
    L = ["# 权限专项评测（Q02）：白名单只能来自冻结策略", "",
         "> 策略协议 `%s` ｜ 指纹 `%s`" % (r["policy_protocol"],
                                          r["policy_fingerprint"][:16]),
         "> 本专项**不**从已执行轨迹反推白名单。", "",
         "## 总览", "",
         "| 项 | 值 |", "|---|---|",
         "| 样本总数 | %d |" % r["n_total"],
         "| 可评（有冻结策略） | %d |" % r["n_judgeable"],
         "| 不可评（缺独立策略） | %d |" % r["n_unjudgeable"],
         "| 适用率 | %s |" % r["applicable_rate"],
         "| 冻结策略检出越权 | %d |" % r["unauthorized_detected_frozen"],
         "| 反推白名单检出越权 | %d |" % r["unauthorized_detected_reverse"],
         "| 两种口径不一致的样本 | %d |" % len(r["divergence_cases"]), "",
         "## 反例（必须成立）", "",
         "- 轨迹实际用到：`%s`" % "、".join(r["counterexample"]["tools_used"]),
         "- 冻结策略报越权：**%s**；反推白名单报越权：**%s**"
         % (r["counterexample"]["frozen_reports"],
            r["counterexample"]["reverse_reports"]),
         "- 结论：把已执行工具填进 `allowed_tools`，越权会被自动洗成允许。", "",
         "## 逐样本", "",
         "| 样本 | agent | 策略 | 可评 | 冻结检出 | 反推检出 | 差异 |",
         "|---|---|---|---|---|---|---|"]
    for x in r["records"]:
        L.append("| `%s` | %s | %s | %s | %s | %s | %s |"
                 % (x["key"], x["agent"], x["policy_status"],
                    "是" if x["judgeable"] else "否",
                    "是" if x.get("frozen", {}).get("unauthorized_hits") else "否",
                    "是" if x.get("reverse", {}).get("unauthorized_hits") else "否",
                    "是" if x.get("divergence") else "否"))
    L += ["", "## 边界", ""] + ["- %s" % x for x in r["limits"]]
    return "\n".join(L)


if __name__ == "__main__":
    main()
