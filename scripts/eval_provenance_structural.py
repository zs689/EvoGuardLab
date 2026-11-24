#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FRONTIER-01 对照评测：关闭全部文本检测器，只靠结构血缘门禁。

诚实输出两列：攻击穿透率（ASR）与良性任务的人工确认代价。
零第三方依赖，可离线复现。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import provenance as PV                                     # noqa: E402


def main():
    rep = PV.evaluate_structural()
    rep["schema_version"] = "provenance-structural-v1"
    rep["text_detectors_enabled"] = False
    out = os.path.join(ROOT, "experiments", "provenance_structural.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("structural_asr = %.3f" % rep["structural_asr"])
    print("benign_auto_pass_rate = %.3f" % rep["benign_auto_pass_rate"])
    print("benign_approval_rate = %.3f" % rep["benign_approval_rate"])
    print("产物：%s" % os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
