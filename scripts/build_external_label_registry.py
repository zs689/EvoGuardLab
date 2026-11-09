# -*- coding: utf-8 -*-
"""生成「待复核」独立标注清单（Q01/Q03）。不冒充人工标签、不覆盖冻结证据。"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import independent_labels as IL  # noqa: E402

LABELS = os.path.join(ROOT, "tests", "fixtures", "labeled", "labels.json")
DEFAULT_OUT = os.path.join(ROOT, "experiments", "external_labeling_v1")


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成独立标注待复核清单")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--external", default=None,
                    help="可选的外部轨迹清单 JSON（需带 source 与 authorization）")
    args = ap.parse_args(argv)

    with open(LABELS, encoding="utf-8") as f:
        doc = json.load(f)
    ext = []
    if args.external:
        with open(args.external, encoding="utf-8") as f:
            ext = json.load(f)
        if isinstance(ext, dict):
            ext = ext.get("traces", [])
    reg = IL.build_registry(doc, ext)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "registry.json"), "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "protocol.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(reg))
    print("[√] 产物：%s" % os.path.join(args.out, "registry.json"))
    print("  自生成 %d ｜ 外部可授权 %d ｜ 独立人工标注 %d ｜ 待复核 %d ｜ kappa=%s"
          % (reg["counts"]["self_generated"],
             reg["counts"]["external_authorized"],
             reg["counts"]["independent_human_labels"],
             reg["counts"]["pending_independent_review"], reg["kappa"]))
    return reg


def _fmt(v):
    return "N/A" if v is None else str(v)


def _markdown(r):
    c = r["counts"]
    L = ["# 独立标注协议与待复核清单", "",
         "> 协议 `%s` ｜ **当前没有外部轨迹，也没有独立标注者**" % r["protocol"], "",
         "## 三种真值必须分开存（Q01）", "",
         "| 维度 | 含义 |", "|---|---|"]
    for k, v in r["truth_dimensions"].items():
        L.append("| `%s` | %s |" % (k, v))
    L += ["", "`recovered` 里确实存在过的攻击发现不一定是假阳性；"
          "任务失败也不意味着任意轨迹规则都该能检出。", "",
          "## 当前状态（如实）", "",
          "| 项 | 值 |", "|---|---|",
          "| 自生成样本 | %d |" % c["self_generated"],
          "| 可授权外部轨迹 | %d |" % c["external_authorized"],
          "| 缺来源/授权被拒 | %d |" % c["external_rejected"],
          "| 独立人工标注 | %d |" % c["independent_human_labels"],
          "| 待复核 | %d |" % c["pending_independent_review"],
          "| 争议待讨论 | %d |" % c["needs_discussion"],
          "| Cohen's kappa | %s |" % _fmt(r["kappa"]), "",
          "> %s" % r["kappa_reason"], "",
          "## 要变成真准确率还差什么（Q03）", ""]
    L += ["%d. %s" % (i, x) for i, x in
          enumerate(r["required_for_real_accuracy"], 1)]
    L += ["", "## 分歧处理", "",
          "两人标同一批、算 kappa；分歧标 `needs_discussion`，"
          "**先讨论再定，不直接取多数**，也不静默丢弃。", "",
          "## 边界", ""] + ["- %s" % x for x in r["limits"]]
    return "\n".join(L)


if __name__ == "__main__":
    main()
