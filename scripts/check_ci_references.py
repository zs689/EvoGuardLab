#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-023：CI workflow 的本地可静态验证部分（不能替代真跑 CI）。

    python scripts/check_ci_references.py --json <path>

检查三件事：
1. `run:` 里引用的 `scripts/*.py` 文件是否真的存在；
2. 命令里用的 `--flag` 是否在该脚本的 argparse 定义里
   （能抓住 `--out` 写成 `--json` 这类会让 CI 直接挂的错误）；
3. 关键门槛脚本是否都被 workflow 调用
   （能抓住「新加的脚本没进 CI」，例如这次的 check_build_context.py）。

**边界**：不执行 workflow、不验证 GitHub 表达式、不验证 runner 环境。
"""
import argparse
import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")

REQUIRED_SCRIPTS = (
    "review_checks.py", "check_wheel_manifest.py", "wheel_smoke.py",
    "check_docker_context.py", "check_build_context.py",
    "defense_demo.py",
)


def run_blocks(text):
    """提取 `run: <cmd>` 与 `run: |` 多行块。"""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^(\s*)run:\s*\|?\s*$", ln)
        if m and ln.rstrip().endswith("|"):
            indent = len(m.group(1))
            i += 1
            buf = []
            while i < len(lines):
                cur = lines[i]
                if cur.strip() and (len(cur) - len(cur.lstrip())) <= indent:
                    break
                buf.append(cur)
                i += 1
            out.append("\n".join(buf))
            continue
        m2 = re.match(r"^\s*run:\s*(.+)$", ln)
        if m2:
            out.append(m2.group(1))
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    with io.open(WORKFLOW, encoding="utf-8") as f:
        text = f.read()
    blocks = run_blocks(text)
    problems, checked = [], []

    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            for script in re.findall(r"scripts/([A-Za-z0-9_]+\.py)", line):
                path = os.path.join(ROOT, "scripts", script)
                if not os.path.isfile(path):
                    problems.append("CI 引用了不存在的脚本：%s" % script)
                    continue
                with io.open(path, encoding="utf-8") as f:
                    src = f.read()
                flags = re.findall(r"(?<![\w-])--([a-z][a-z0-9_-]*)", line)
                # 去掉值本身被误认的情况：只查紧跟脚本名的命令行 token
                for flag in set(flags):
                    needle = 'add_argument("--%s"' % flag
                    if needle not in src and \
                            "add_argument('-%s'" % flag not in src:
                        # 允许 argparse 从别处动态生成（保守跳过）
                        if "argparse" in src:
                            problems.append(
                                "%s 未定义 --%s，但 CI 命令用了它"
                                % (script, flag))
                checked.append(script)

    # 只统计**非注释的 run: 行**里真正被执行的脚本。
    # 扫全文会把注释掉的引用也算进去 —— 那会造成「脚本没进 CI 却 PASS」的假绿。
    referenced = set()
    for block in blocks:
        for line in block.splitlines():
            if line.strip().startswith("#"):
                continue
            referenced.update(
                re.findall(r"scripts/([A-Za-z0-9_]+\.py)", line))
    for need in REQUIRED_SCRIPTS:
        if need not in referenced:
            problems.append("关键门槛脚本没进 CI：%s" % need)

    report = {
        "version": 1,
        "workflow": ".github/workflows/test.yml",
        "method": "静态解析 run: 块，不执行 workflow，不替代真实 CI",
        "run_blocks": len(blocks),
        "scripts_referenced": sorted(referenced),
        "required_scripts": list(REQUIRED_SCRIPTS),
        "reference_checks": len(checked),
        "problems": problems,
        "passed": not problems,
    }
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
            f.write("\n")
    for p in problems:
        print("  [!] %s" % p)
    print("== %s ==（%d 个 run 块，%d 个脚本引用）"
          % ("PASS" if report["passed"] else "FAIL",
             len(blocks), len(checked)))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
