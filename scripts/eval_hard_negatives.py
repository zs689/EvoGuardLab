#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FRONTIER-03：硬负样本陷阱评测——量化"看起来像攻击"的良性文本误报。

零依赖、离线可复现。绝不篡改样本凑数：只报告真实误报与每类明细。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import security as S                                        # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures",
                   "hard_negative_traps.json")


def main():
    data = json.load(open(FIX, encoding="utf-8"))
    cases = data["cases"]
    fp, per_cat, details, advisories = 0, {}, [], []
    for c in cases:
        text = c["text"]
        findings = []
        try:
            findings += S.scan(text, "text") or []
        except Exception as exc:                            # noqa: BLE001
            findings.append({"code": "scan_error:" + type(exc).__name__})
        # security.scan 内部已经调用 pinyin_guard 并按通道降级；
        # 这里曾重复直调 PG.scan（无通道概念），造成同一条发现被算两次，
        # 且把元讨论语境里的引用句式当成硬拦。
        # enforce 只拦 severity=error（见 security.guard_messages）；
        # 元讨论语境降级后的 info 是 advisory，不是硬拦误报。
        # 两类都记录，但只有硬拦计入 FPR。
        hit = [str(f.get("code")) for f in findings
               if str(f.get("severity") or "error") in ("error", "warn")]
        advisory = [str(f.get("code")) for f in findings
                    if str(f.get("severity") or "") not in ("error", "warn")]
        if advisory:
            advisories.append({"id": c["id"], "codes": advisory})
        if hit:
            fp += 1
            cat = c["category"]
            per_cat.setdefault(cat, {"n": 0, "fp": 0})
            per_cat[cat]["n"] += 1
            per_cat[cat]["fp"] += 1
            details.append({"id": c["id"], "category": cat,
                            "codes": hit[:5], "text": text[:120]})
        else:
            per_cat.setdefault(c["category"], {"n": 0, "fp": 0})
            per_cat[c["category"]]["n"] += 1
    n = len(cases) or 1
    rep = {
        "schema_version": "hard-negatives-v1",
        "n": len(cases),
        "false_positive": fp,
        "fpr": round(fp / n, 4),
        "gate_max_fpr": 0.15,
        "by_category": {k: dict(v, fpr=round(v["fp"] / max(1, v["n"]), 4))
                        for k, v in sorted(per_cat.items())},
        "false_positives": details,
        "advisory_count": len(advisories),
        "advisories": advisories,
        "note": ("只测默认文本栈（L1 + pinyin）；不含模型语义层。"
                 "样本固定，不允许为过门禁而删样本或放宽判定。"
                 "enforce 只拦 severity=error；元讨论语境降级的 info 单列"
                 " advisory_count，不计入硬拦误报。"),
    }
    out = os.path.join(ROOT, "experiments", "hard_negatives.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("硬负样本 %d 条，误报 %d（FPR=%.3f，门槛<=%.2f）"
          % (rep["n"], fp, rep["fpr"], rep["gate_max_fpr"]))
    for c, v in rep["by_category"].items():
        print("  %-22s %d/%d" % (c, v["fp"], v["n"]))
    print("产物：%s" % os.path.relpath(out, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
