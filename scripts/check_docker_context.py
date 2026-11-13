#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-003 验收：模拟一棵带敏感标记的工作树，验证它们不会进构建上下文。

    python scripts/check_docker_context.py
    python scripts/check_docker_context.py --out <dir>

**已知边界**：本机连不上 Docker Desktop 的 Linux daemon（独立审查也遇到
了同样的限制）。所以这里验证的是「.dockerignore 规则 + Dockerfile COPY
清单」这两层静态防线，用标记文件模拟 .env / .git / 数据库 / 语料目录。
**镜像层内容没有实测** —— 这句必须写进报告，不能拿静态检查冒充 build 验证。

有两层防线：任一层被改坏，另一层仍应挡住本地数据。
"""
import argparse
import fnmatch
import io
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 模拟工作树：前一组是必须进上下文的运行资产，后一组是绝不能进的标记文件
REQUIRED = [
    "pyproject.toml", "README.md", "LICENSE", "evoguard_cli.py",
    "ai-service/evoguard/core.py", "ai-service/api/app.py",
    "config/thresholds_v1.json", "frontend/index.html",
    "docs/taxonomy.md", "experiments/metrics.json",
    "scripts/defense_demo.py",
]
SENSITIVE = [
    ".env",
    ".git/config",
    "data/evoguard.db",
    "data/external/cases.jsonl",
    "data/license_corpus/big.bin",
    "_sandbox/local_notes.txt",
    "experiments/secret_run.json",
    "ai-service/evoguard/__pycache__/core.cpython-311.pyc",
]


def _rules():
    out = []
    with io.open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out


def _match_one(rule, path):
    """近似 Docker .dockerignore 的匹配（够用于本验收）。

    - 含 `/`：按仓库相对路径匹配，`*` 不跨目录、`**` 跨目录
    - 不含 `/`：匹配任意一段路径（文件名或目录名）
    - 尾 `/` 或目录规则：命中该目录前缀也算命中
    """
    r = rule.rstrip("/")
    if "/" in r:
        if "**" in r:
            expr = re.escape(r).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        else:
            expr = re.escape(r).replace(r"\*", "[^/]*")
        return re.match("^" + expr + "($|/)", path) is not None
    parts = path.split("/")
    return any(fnmatch.fnmatch(p, r) for p in parts)


def included(path, rules):
    """最后一条匹配的规则决定去留（`!` 表示重新包含）。"""
    keep = True
    for rule in rules:
        neg = rule.startswith("!")
        bare = rule[1:] if neg else rule
        if _match_one(bare, path):
            keep = neg
    return keep


def copy_sources():
    """Dockerfile 里所有 COPY 的源清单，并检查有没有整仓 COPY。"""
    srcs, whole = [], []
    with io.open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s.upper().startswith("COPY "):
                continue
            parts = s.split()
            for p in parts[1:-1]:
                if p.startswith("[") or p == "--chown":
                    continue
                srcs.append(p)
                if p in (".", "./") or p.endswith("/."):
                    whole.append(p)
    return srcs, whole


def main():
    ap = argparse.ArgumentParser(description="构建上下文静态验收")
    ap.add_argument("--out", default="", help="输出目录")
    a = ap.parse_args()

    rules = _rules()
    got_required = [p for p in REQUIRED if included(p, rules)]
    leaked = [p for p in SENSITIVE if included(p, rules)]
    srcs, whole = copy_sources()

    checks = [
        {"id": "dockerignore_exists", "pass": bool(rules),
         "detail": ".dockerignore 规则数 %d" % len(rules)},
        {"id": "sensitive_excluded", "pass": not leaked,
         "detail": "敏感标记仍会进上下文：%s" % leaked if leaked
                   else "8 个敏感标记全部被排除", "leaked": leaked},
        {"id": "required_included", "pass": len(got_required) == len(REQUIRED),
         "detail": "运行资产 %d/%d 可进上下文"
                   % (len(got_required), len(REQUIRED)),
         "missing": [p for p in REQUIRED if p not in got_required]},
        {"id": "no_whole_tree_copy", "pass": not whole,
         "detail": "Dockerfile 仍整仓 COPY：%s" % whole if whole
                   else "Dockerfile 无整仓 COPY"},
        {"id": "copy_sources_allowed", "pass": all(
            included(p.rstrip("/"), rules) for p in srcs),
         "detail": "COPY 源清单：%s" % srcs},
    ]
    ok = all(c["pass"] for c in checks)
    report = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passed": ok,
        "checks": checks,
        "limitations": [
            "本机 Docker daemon 不可达，未执行 docker build，镜像层内容未实测。",
            "本检查验证的是 .dockerignore 规则与 COPY 清单两层静态防线。",
            "规则匹配是 Docker 语义的近似实现，不是 Docker 引擎本身。",
        ],
    }
    for c in checks:
        print("%s %-24s %s" % ("PASS" if c["pass"] else "FAIL",
                               c["id"], c["detail"]))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "docker_context.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print("== %s ==" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
