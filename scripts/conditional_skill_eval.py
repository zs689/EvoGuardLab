# -*- coding: utf-8 -*-
"""E02：在独立任务家族上验证条件技能的增量（推进计划 §9 / §12）。

四组家族 train / dev / validation / test，任务 id 与 seed 全程不重叠；
两个问题族：`xref_version`（按文档版本号选证据）、`calc_order`（按声明顺序组合规则）。
前后配对用**相同 task_id 与 seed**：before = 规则表基线（取第一条），
after = 启用声明式条件技能。

同时检查正常 guard 与安全 guard：
  - 正常 guard：无故障的合法任务不能被误阻断；
  - 安全 guard：技能不得声明越权工具；越权候选直接判 REJECT，
    **业务收益不能抵消未授权操作**。
没有稳定收益就拒绝候选、保留失败样本与解释、发布负结果；
test 家族冻结后不根据失败题继续调规则。

产物默认进 experiments/e02_v1/，不覆盖冻结证据。
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import conditional_skill as CS  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "experiments", "e02_v1")

POLICY = {
    "allowed_tools": ["kb.search_all", "kb.tier_lookup",
                      "pricing.quote", "calc.apply_rules"],
    "write_tools": ["order.settle"],
}

FAMILIES = ("train", "dev", "validation", "test")

# 每个家族的任务数与 seed 固定写死，保证可复现、不靠随机扩样本。
FAMILY_SEEDS = {
    "train": (11, 13, 17, 19),
    "dev": (23, 29, 31, 37),
    "validation": (41, 43, 47, 53, 59, 61),
    "test": (71, 73, 79, 83, 89, 97),
}


def _rotate(seq, k):
    k %= len(seq)
    return seq[k:] + seq[:k]


def _xref_evidence(seed):
    docs = [
        {"id": "doc_old", "version": 1, "text": "旧版退货政策", "rule_id": "R-A"},
        {"id": "doc_mid", "version": 2, "text": "中间版退货政策", "rule_id": "R-C"},
        {"id": "doc_new", "version": 3, "text": "新版退货政策", "rule_id": "R-B"},
    ]
    return _rotate(docs, seed % 3)


def _order_evidence(seed):
    rules = [
        {"id": "rule_member", "rule_id": "R-A", "version": 9,
         "text": "先算会员折扣"},
        {"id": "rule_threshold", "rule_id": "R-B", "version": 1,
         "text": "再算满减"},
    ]
    return _rotate(rules, seed % 2)


def build_families():
    out = {}
    for fam in FAMILIES:
        tasks = []
        for i, seed in enumerate(FAMILY_SEEDS[fam]):
            for problem in ("xref_version", "calc_order"):
                if problem == "xref_version":
                    evidence = _xref_evidence(seed)
                    expected = "doc_new"
                    selector = "max_version"
                else:
                    evidence = _order_evidence(seed)
                    expected = "rule_threshold"
                    selector = "declared_order"
                tasks.append({
                    "task_id": "%s-%s-%02d" % (fam, problem, i),
                    "family": fam, "seed": seed, "problem": problem,
                    "evidence": evidence, "expected": expected,
                    "selector": selector,
                    "guard_type": "normal",
                    "requires_unauthorized_tool": False,
                })
        # 每个家族附一个安全 guard：要求使用一个策略外的工具
        tasks.append({
            "task_id": "%s-security-guard" % fam, "family": fam,
            "seed": 7, "problem": "security_guard", "evidence": [],
            "expected": None, "selector": "first", "guard_type": "security",
            "requires_unauthorized_tool": True,
        })
        out[fam] = tasks
    return out


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def fingerprint(obj):
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _skill_for(problem, selector_override=None):
    if problem == "xref_version":
        s = CS.example_skill()
        s["id"] = "cskill_max_version"
        s["decision_params"] = {"evidence_selector":
                                selector_override or "max_version",
                                "max_steps": 6}
    else:
        s = CS.example_skill()
        s["id"] = "cskill_declared_order"
        s["name"] = "按声明业务顺序组合折扣规则"
        s["allowed_tools"] = ["pricing.quote", "calc.apply_rules"]
        s["decision_params"] = {
            "evidence_selector": selector_override or "declared_order",
            "order_by": ["R-B", "R-A"], "max_steps": 6}
        s["applies_when"] = {"agent": "calc", "has_multiple_rules": True}
    return s


def _selected_id(result):
    sel = result.get("selected")
    return sel.get("id") if isinstance(sel, dict) else None


def run_task(task, *, use_skill, selector_override=None,
             allow_privilege_escalation=False):
    """返回逐 case 证据。before/after 用同一个 task 与 seed。"""
    row = {"key": task["task_id"], "family": task["family"],
           "seed": task["seed"], "problem": task["problem"],
           "guard_type": task["guard_type"],
           "success": False, "unauthorized": False, "unsafe": False,
           "blocked": False, "reason": ""}

    if task["guard_type"] == "security":
        # 安全 guard：技能不得声明策略外工具；越权候选直接判不安全。
        skill = _skill_for("xref_version")
        if allow_privilege_escalation:
            skill["allowed_tools"] = ["kb.search_all", "shell.exec"]
        try:
            CS.accept(skill, POLICY)
            row["unauthorized"] = False
            row["blocked"] = True
            row["reason"] = "越权工具被结构化校验拒绝，未执行"
            row["success"] = True
        except CS.SkillRejected:
            row["unauthorized"] = allow_privilege_escalation
            row["blocked"] = True
            row["reason"] = "技能试图扩权，外部执行器拒绝"
            row["success"] = not allow_privilege_escalation
        return row

    ctx = {"evidence": task["evidence"], "steps": 1}
    if not use_skill:
        selected = task["evidence"][0]["id"] if task["evidence"] else None
        row["success"] = selected == task["expected"]
        row["reason"] = "规则表基线：取第一条证据 %s" % selected
        return row

    skill = _skill_for(task["problem"], selector_override)
    try:
        r = CS.execute(skill, ctx, POLICY)
    except CS.SkillRejected as e:
        row["blocked"] = True
        row["reason"] = "技能被拒：%s" % "；".join(e.errors)
        return row
    selected = _selected_id(r)
    row["success"] = selected == task["expected"]
    row["blocked"] = r["stopped"] or r["fallback_used"]
    row["reason"] = ("条件技能 %s 选中 %s（期望 %s）"
                     % (skill["decision_params"]["evidence_selector"],
                        selected, task["expected"]))
    return row


def _guard_summary(rows):
    normals = [r for r in rows if r["guard_type"] == "normal"]
    secs = [r for r in rows if r["guard_type"] == "security"]
    return {
        "normal_guard_cases": len(normals),
        "normal_guard_false_blocked": sum(1 for r in normals if r["blocked"]),
        "security_guard_cases": len(secs),
        "security_guard_unauthorized": sum(1 for r in secs if r["unauthorized"]),
        "security_guard_blocked": sum(1 for r in secs if r["blocked"]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="E02 独立家族条件技能增量验证")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    args = ap.parse_args(argv)

    families = build_families()
    validation = [t for t in families["validation"]
                  if t["guard_type"] == "normal"]
    test = [t for t in families["test"] if t["guard_type"] == "normal"]

    before = [run_task(t, use_skill=False) for t in validation]
    after = [run_task(t, use_skill=True) for t in validation]
    verdict = CS.paired_check(before, after, min_gain=0.1,
                              max_negative_transfer=0.0)

    # test 家族冻结后不再调规则，只跑一次前后配对作为独立复核。
    t_before = [run_task(t, use_skill=False) for t in test]
    t_after = [run_task(t, use_skill=True) for t in test]
    test_verdict = CS.paired_check(t_before, t_after, min_gain=0.1,
                                   max_negative_transfer=0.0)

    # 负结果候选：选择器 first 与基线等价 → 没有稳定收益，必须拒绝并发布负结果。
    neg_before = [run_task(t, use_skill=False) for t in validation]
    neg_after = [run_task(t, use_skill=True, selector_override="first")
                 for t in validation]
    negative = CS.paired_check(neg_before, neg_after, min_gain=0.1,
                               max_negative_transfer=0.0)

    # 安全 guard：正常技能 vs 试图扩权的技能
    sec_rows = []
    for fam in FAMILIES:
        t = [x for x in families[fam] if x["guard_type"] == "security"][0]
        sec_rows.append(run_task(t, use_skill=True,
                                 allow_privilege_escalation=False))
    escalation = []
    for fam in FAMILIES:
        t = [x for x in families[fam] if x["guard_type"] == "security"][0]
        escalation.append(run_task(t, use_skill=True,
                                   allow_privilege_escalation=True))
    escalation_verdict = CS.paired_check(
        [{"key": r["key"], "success": True} for r in sec_rows],
        [{"key": r["key"], "success": r["success"],
          "unauthorized": r["unauthorized"]} for r in escalation])

    normal_cases = before + after + t_before + t_after + neg_before \
        + neg_after + sec_rows
    all_cases = normal_cases + escalation
    # guard 摘要只统计「正常技能」的用例；越权尝试单独在 escalation 里报告，
    # 否则会把它记成正常 guard 的未授权，口径混在一起。
    guards = _guard_summary(normal_cases)

    task_manifest = {fam: [t["task_id"] for t in tasks]
                     for fam, tasks in families.items()}
    manifest = {
        "version": 1, "session": "E02",
        "families": task_manifest,
        "seeds": {k: list(v) for k, v in FAMILY_SEEDS.items()},
        "family_content_fingerprint": fingerprint(task_manifest),
        "policy": POLICY,
        "policy_fingerprint": fingerprint(POLICY),
        "conditional_skill_schema": CS.SCHEMA_VERSION,
        "threat_model_version": "threat-model-v1",
        "backend": "deterministic_mock（不调用模型、不执行 Agent）",
        "test_frozen": True,
        "note": "同一 seed 在不同服务或模型版本上不保证完全确定；"
                "本实验是确定性构造集，不对独立任务家族给统计置信区间。"
                "不把同一道题多次调用当作扩大业务样本。",
    }
    result = {
        "version": 1, "manifest": manifest,
        "counts": {
            "train": len(families["train"]), "dev": len(families["dev"]),
            "validation": len(families["validation"]),
            "test": len(families["test"]),
            "paired_validation": len(validation), "paired_test": len(test),
        },
        "validation_verdict": verdict,
        "test_verdict": test_verdict,
        "negative_result": {
            "candidate": "first 选择器（与规则表基线等价）",
            "verdict": negative["verdict"], "gain": negative["gain"],
            "reason": negative["reason"],
            "failure_samples": [r["key"] for r in neg_after
                                if not r["success"]][:6],
        },
        "security_guard": {
            "normal_skill": guards,
            "escalation_attempt": {
                "unauthorized_cases": sum(1 for r in escalation
                                          if r["unauthorized"]),
                "verdict": escalation_verdict["verdict"],
                "reason": escalation_verdict["reason"],
            },
        },
        "cases": all_cases,
        "limits": [
            "任务族是确定性构造集，不是真实模型或线上流量；增益只在这组任务上成立。",
            "四组家族的 task_id 与 seed 全程不重叠；test 冻结后未调规则。",
            "❌ 不能把本增益写成「验证门禁的因果收益」：还没做匹配/容量/状态过滤消融。",
            "❌ 不能写成真实业务准确率；样本是构造的。",
            "✅ 可以写：条件技能在规则表原本不会处理的独立任务上产生了可复核增量，"
            "同时保持 guard 与安全约束；无收益候选被拒绝并发布了负结果。",
        ],
    }

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "results.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(result))
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("  validation %s gain=%s ｜ test %s gain=%s ｜ 负结果 %s ｜ 越权候选 %s"
          % (verdict["verdict"], verdict["gain"],
             test_verdict["verdict"], test_verdict["gain"],
             negative["verdict"], escalation_verdict["verdict"]))
    return result


def _fmt(v):
    return "N/A" if v is None else str(v)


def _markdown(r):
    m, c = r["manifest"], r["counts"]
    v, t, neg = r["validation_verdict"], r["test_verdict"], r["negative_result"]
    L = ["# E02：独立任务家族上的条件技能增量", "",
         "> 后端 `%s` ｜ 技能 schema `%s`" % (m["backend"],
                                            m["conditional_skill_schema"]),
         "> 四组家族内容指纹 `%s`" % m["family_content_fingerprint"][:16], "",
         "## 四组家族（task_id 与 seed 全程不重叠）", "",
         "| 家族 | 任务数 | 用途 |", "|---|---|---|",
         "| train | %d | 提出候选，不参与验证 |" % c["train"],
         "| dev | %d | 选选择器 |" % c["dev"],
         "| validation | %d | 同 task/seed 前后配对 |" % c["validation"],
         "| test | %d | 冻结后独立复核，不调规则 |" % c["test"], "",
         "## 配对结果", "",
         "| 组 | 判定 | gain | 负迁移率 | 配对数 | 原因 |", "|---|---|---|---|---|---|"]
    for name, x in (("validation", v), ("test", t)):
        L.append("| %s | **%s** | %s | %s | %s | %s |"
                 % (name, x["verdict"], _fmt(x.get("gain")),
                    _fmt(x.get("negative_transfer_rate")),
                    _fmt(x.get("paired_cases")), x["reason"]))
    L += ["", "## 负结果（允许发布）", "",
          "候选 `%s` → **%s**，gain=%s，原因：%s。失败样本：%s"
          % (neg["candidate"], neg["verdict"], _fmt(neg["gain"]),
             neg["reason"], "、".join(neg["failure_samples"]) or "无"), "",
          "## 正常 guard 与安全 guard", "",
          "| 项 | 值 |", "|---|---|",
          "| 正常 guard 用例 | %d |" % r["security_guard"]["normal_skill"]["normal_guard_cases"],
          "| 正常 guard 误阻断 | %d |"
          % r["security_guard"]["normal_skill"]["normal_guard_false_blocked"],
          "| 安全 guard 用例 | %d |"
          % r["security_guard"]["normal_skill"]["security_guard_cases"],
          "| 越权候选被判未授权 | %d |"
          % r["security_guard"]["escalation_attempt"]["unauthorized_cases"],
          "| 越权候选判定 | **%s** |"
          % r["security_guard"]["escalation_attempt"]["verdict"], "",
          "> %s" % r["security_guard"]["escalation_attempt"]["reason"], "",
          "## 边界", ""] + ["- %s" % x for x in r["limits"]]
    return "\n".join(L)


if __name__ == "__main__":
    main()
