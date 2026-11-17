#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-003 静态自查：模拟 .dockerignore 过滤构建上下文。

    python scripts/check_build_context.py --json <path>

**边界**：这是 Python 近似模拟 Docker 的匹配规则，用来发现规则写漏；
不能替代 `docker build` 的镜像层实测（本机 daemon 不可达）。
"""
import argparse
import fnmatch
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SENSITIVE = [
    (".git", "版本控制历史"),
    (".env", "本地凭据"),
    ("data/external", "外部语料（许可+隐私）"),
    ("data/tenants", "租户数据"),
    ("experiments", "实验产物"),
    ("__pycache__", "缓存"),
]


def load_rules():
    with io.open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8") as f:
        lines = f.readlines()
    rules = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        if neg:
            line = line[1:]
        rules.append((neg, line.rstrip("/")))
    return rules


def matches(rule, rel):
    """近似 Docker：整段名匹配或前缀匹配 + fnmatch。"""
    rel = rel.replace("\\", "/")
    if rule == rel:
        return True
    if rel.startswith(rule + "/"):
        return True
    if fnmatch.fnmatch(rel, rule):
        return True
    # **/foo 形式
    if rule.startswith("**/"):
        tail = rule[3:]
        if fnmatch.fnmatch(os.path.basename(rel), tail):
            return True
        if fnmatch.fnmatch(rel, tail):
            return True
    return False


def included(rel, rules):
    inc = True
    for neg, rule in rules:
        if matches(rule, rel):
            inc = neg
    return inc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    rules = load_rules()

    files, excluded, sensitive_check = [], [], []
    for base, dirs, names in os.walk(ROOT):
        rel_base = os.path.relpath(base, ROOT).replace("\\", "/")
        if rel_base == ".":
            rel_base = ""
        # 目录级排除（模拟 Docker 的父目录规则）
        if rel_base and not included(rel_base, rules):
            dirs[:] = []
            continue
        for n in names:
            rel = (rel_base + "/" + n).lstrip("/")
            if included(rel, rules):
                try:
                    size = os.path.getsize(os.path.join(base, n))
                except OSError:
                    size = 0
                files.append((rel, size))
            else:
                excluded.append(rel)

    # Dockerfile 里显式 COPY 的源，必须都在上下文里
    with io.open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
        dockerfile = f.read()
    copies = []
    for m in re.finditer(r"^COPY\s+(.+?)\s+\S+\s*$", dockerfile, re.M):
        for tok in m.group(1).split():
            if not tok.startswith("$"):
                copies.append(tok)
    missing_copy = [c for c in copies
                    if not any(f == c or f.startswith(c.rstrip("/") + "/")
                               for f, _ in files)]

    # experiments/metrics.json 是 .dockerignore 里刻意 ! 放行的只读资产，
    # Dockerfile 也显式 COPY 它 —— 不算泄漏。
    INTENTIONAL = {"experiments/metrics.json"}
    for prefix, desc in SENSITIVE:
        leaked = [f for f, _ in files
                  if (f not in INTENTIONAL) and
                  (f == prefix or f.startswith(prefix + "/") or
                   (prefix in ("__pycache__",) and "__pycache__" in f))]
        sensitive_check.append({"item": prefix, "desc": desc,
                                "leaked_count": len(leaked),
                                "sample": leaked[:3]})

    # 后缀类敏感文件
    for pat, desc in ((".db", "数据库"), (".pem", "私钥"), (".key", "密钥")):
        leaked = [f for f, _ in files if f.endswith(pat)]
        sensitive_check.append({"item": "*" + pat, "desc": desc,
                                "leaked_count": len(leaked),
                                "sample": leaked[:3]})

    # pyproject.toml 声明的包目录 / data-files 源文件，必须都进构建上下文。
    # 只查「COPY 源存在」会漏掉「声明了却没 COPY」——EG-003 实测就踩到
    # evoguard_inspect 漏拷导致 pip install -e . 在容器里失败。
    with io.open(os.path.join(ROOT, "pyproject.toml"),
                 encoding="utf-8") as f:
        pyproj = f.read()
    declared_pkg_dirs = []
    m = re.search(r"package-dir\s*=\s*\{([^}]*)\}", pyproj, re.S)
    if m:
        declared_pkg_dirs = [v for _, v in
                             re.findall(r'"([^"]+)"\s*=\s*"([^"]+)"',
                                        m.group(1)) if v != "."]
    data_sources = []
    dm = re.search(r"\[tool\.setuptools\.data-files\](.*?)(?=\n\[|\Z)",
                   pyproj, re.S)
    if dm:
        for val in re.findall(r'"([^"]+)"', dm.group(1)):
            if "/" in val and not val.startswith("share/"):
                data_sources.append(val)

    def covered(path):
        return any(c == path or c.rstrip("/") == path.rstrip("/")
                   or path.startswith(c.rstrip("/") + "/")
                   or c.startswith(path.rstrip("/") + "/")
                   for c in copies)

    missing_declared = sorted({p for p in declared_pkg_dirs + data_sources
                               if not covered(p)})
    problems_extra = ["pyproject 声明但 Dockerfile 未 COPY：%s" % p
                      for p in missing_declared]

    # 反向校验：源码树里每个含 __init__.py 的目录都必须是声明的包。
    # 以前只查「声明的有没有 COPY」，查不出「有子包没声明」——
    # evoguard_inspect 下 loaders/rules/static 三个子包就是这样漏进 wheel 的。
    declared_packages = set(re.findall(r'"([A-Za-z0-9_.]+)"',
                                       (re.search(r"^packages\s*=\s*\[([^\]]*)\]",
                                                  pyproj, re.M | re.S) or
                                        type("", (), {"group": lambda self, n: ""})()).group(1)))
    src_root = os.path.join(ROOT, "ai-service")
    undeclared = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        if "__pycache__" in dirpath or "egg-info" in dirpath:
            continue
        if "__init__.py" in filenames:
            rel = os.path.relpath(dirpath, src_root).replace(os.sep, ".")
            if rel not in declared_packages:
                undeclared.append(rel)
    undeclared = sorted(set(undeclared))
    problems_extra += ["子包未在 pyproject packages 声明：%s" % u
                       for u in undeclared]

    ok = (not any(x["leaked_count"] for x in sensitive_check)
          and not missing_copy and not problems_extra)
    report = {
        "version": 1,
        "method": "Python 近似模拟 .dockerignore，不是 docker build 实测",
        "rules": len(rules),
        "included_files": len(files),
        "included_bytes": sum(s for _, s in files),
        "excluded_files": len(excluded),
        "dockerfile_copy_sources": copies,
        "missing_copy_sources": missing_copy,
        "declared_package_dirs": declared_pkg_dirs,
        "declared_data_sources": data_sources,
        "missing_declared": missing_declared,
        "undeclared_subpackages": undeclared,
        "intentional_includes": sorted(INTENTIONAL),
        "sensitive": sensitive_check,
        "passed": ok,
        "boundary": "镜像层实测仍需 docker daemon；本检查只保证规则无漏。",
    }
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
            f.write("\n")
    for x in sensitive_check:
        print("%-22s %s" % (x["item"], "OK" if not x["leaked_count"]
                            else "LEAK %d %s" % (x["leaked_count"],
                                                x["sample"])))
    print("COPY 源缺失: %s" % (missing_copy or "无"))
    for p in problems_extra:
        print("  [!] %s" % p)
    print("pyproject 声明包/资产: %d 个目录 + %d 个文件，漏拷 %d，未声明子包 %d"
          % (len(declared_pkg_dirs), len(data_sources),
             len(missing_declared), len(undeclared)))
    print("== %s ==（上下文 %d 文件 / %.1f MiB）"
          % ("PASS" if ok else "FAIL", len(files),
             report["included_bytes"] / 1048576.0))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
