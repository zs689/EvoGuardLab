#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-004：验证 wheel 清单里**真的有**承诺分发的模块与只读资产。

    python scripts/check_wheel_manifest.py dist/evoguardlab-0.6.0-py3-none-any.whl

以前 wheel 里只有 `evoguard/`，enterprise/ 子包、Inspector、前端、配置和
演示脚本一个都没有 —— 装完能 import，但 CLI 一用就崩。CI 里必须对着
**制品本身**（不是源码树）检查，editable 冒烟不能代替它。
"""
import argparse
import json
import os
import sys
import zipfile

REQUIRED = [
    "evoguard_cli.py",
    "evoguard/baseline.py",
    "evoguard/security.py",
    "evoguard/enterprise/__init__.py",
    "evoguard/enterprise/audit.py",
    "evoguard/enterprise/scorecard.py",
    "evoguard/enterprise/tenancy.py",
    "evoguard_inspect/__init__.py",
]
REQUIRED_DATA = [
    "share/evoguardlab/api/app.py",
    "share/evoguardlab/scripts/defense_demo.py",
    "share/evoguardlab/frontend/index.html",
    "share/evoguardlab/config/thresholds_v1.json",
    "share/evoguardlab/docs/taxonomy.md",
    "share/evoguardlab/experiments/metrics.json",
]
FORBIDDEN_HINTS = (".env", ".git/", "data/external/", "data/tenants/",
                   "tests/", "__pycache__/", ".pyc", "sandbox")


def main():
    ap = argparse.ArgumentParser(description="wheel 清单验收")
    ap.add_argument("wheel")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.wheel):
        print("[×] 找不到 wheel：%s" % a.wheel)
        return 2
    with zipfile.ZipFile(a.wheel) as z:
        names = z.namelist()
    # data-files 在 wheel 里带前缀，安装时才落到 share/
    def has(suffix):
        return any(n.endswith(suffix) for n in names)

    missing_modules = [p for p in REQUIRED if not has(p)]
    missing_data = [p for p in REQUIRED_DATA if not has(p)]
    leaked = sorted({n for n in names
                     if any(h in n for h in FORBIDDEN_HINTS)})
    ok = not missing_modules and not missing_data and not leaked

    report = {
        "wheel": os.path.basename(a.wheel),
        "entries": len(names),
        "passed": ok,
        "missing_modules": missing_modules,
        "missing_data": missing_data,
        "forbidden_entries": leaked[:20],
        "note": "对制品本身检查，不用 editable 安装冒烟代替。",
    }
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("wheel: %s (%d entries)" % (report["wheel"], len(names)))
        print("缺模块: %s" % (missing_modules or "无"))
        print("缺资产: %s" % (missing_data or "无"))
        print("不该出现的条目: %s" % (leaked[:5] or "无"))
        print("== %s ==" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
