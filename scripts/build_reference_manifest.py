#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""为参考仓库生成统一清单：commit SHA / 许可证 / 来源 / 用途。

    python scripts/build_reference_manifest.py --root <workspace>/github \
        --out <workspace>/github/SOURCE_MANIFEST.json

为什么需要（外部审查 REF-ORG-001）：
`learn/` 和 `github/` 下散着十几个参考实现，没有统一清单 ——
不知道用的是哪个 commit、什么许可证，也没法复现「当时对照的是哪一版」。
`EvoGuardLab` 自己的语料有 `manifest.json` 记 commit，参考仓库却没有。

**只读** git 元数据与 LICENSE 文件，不修改任何仓库内容。
"""
import argparse
import io
import json
import os
import re
import subprocess

LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING",
                 "COPYING.md", "LICENSE-APACHE", "LICENSE-MIT")
_SPDX = (
    ("Apache-2.0", re.compile(r"Apache License[,\s]+Version 2\.0", re.I)),
    ("MIT", re.compile(r"\bMIT License\b|Permission is hereby granted, free of charge", re.I)),
    ("BSD-3-Clause", re.compile(r"BSD 3-Clause|Redistributions of source code", re.I)),
    ("BSD-2-Clause", re.compile(r"BSD 2-Clause", re.I)),
    ("GPL-3.0", re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 3", re.I)),
    ("GPL-2.0", re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 2", re.I)),
    ("LGPL-3.0", re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE", re.I)),
    ("MPL-2.0", re.compile(r"Mozilla Public License Version 2\.0", re.I)),
    ("AGPL-3.0", re.compile(r"GNU AFFERO GENERAL PUBLIC LICENSE", re.I)),
    ("Unlicense", re.compile(r"This is free and unencumbered software", re.I)),
)


def _git(repo, *args):
    try:
        out = subprocess.run(["git", "-C", repo] + list(args),
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def detect_license(repo):
    """返回 (spdx, 文件名)。识别不了就返回 ("UNKNOWN", 文件名)。"""
    for name in LICENSE_FILES:
        p = os.path.join(repo, name)
        if not os.path.isfile(p):
            continue
        try:
            with io.open(p, encoding="utf-8", errors="replace") as f:
                head = f.read(4000)
        except OSError:
            continue
        for spdx, rx in _SPDX:
            if rx.search(head):
                return spdx, name
        return "UNKNOWN", name
    return "NO_LICENSE_FILE", None


def describe_repo(path):
    name = os.path.basename(os.path.normpath(path))
    if not os.path.isdir(os.path.join(path, ".git")):
        return {"name": name, "path": path, "is_git": False,
                "note": "不是 git 仓库，无法锁定 commit"}
    dirty = _git(path, "status", "--porcelain") or ""
    spdx, lic_file = detect_license(path)
    return {
        "name": name,
        "path": path,
        "is_git": True,
        "commit": _git(path, "rev-parse", "HEAD"),
        "commit_date": _git(path, "log", "-1", "--format=%cI"),
        "subject": (_git(path, "log", "-1", "--format=%s") or "")[:120],
        "remote": _git(path, "remote", "get-url", "origin") or "",
        "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD") or "",
        "dirty_files": len([x for x in dirty.splitlines() if x.strip()]),
        "license": spdx,
        "license_file": lic_file,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = a.root or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "..", "github")
    root = os.path.abspath(root)
    out = a.out or os.path.join(root, "SOURCE_MANIFEST.json")

    repos = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p) and not name.startswith("."):
            repos.append(describe_repo(p))

    missing_lic = [r["name"] for r in repos
                   if r.get("license") in ("UNKNOWN", "NO_LICENSE_FILE", None)]
    nogit = [r["name"] for r in repos if not r.get("is_git")]
    dirty = {r["name"]: r["dirty_files"] for r in repos
             if r.get("dirty_files")}
    manifest = {
        "version": 1,
        "generated_by": "scripts/build_reference_manifest.py",
        "purpose": "参考实现对照清单：锁定 commit 与许可证，保证可复现",
        "root": root,
        "repos": repos,
        "summary": {
            "total": len(repos),
            "with_commit": sum(1 for r in repos if r.get("commit")),
            "missing_or_unknown_license": missing_lic,
            "not_git": nogit,
            "dirty_worktrees": dirty,
        },
    }
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    print("清单写入：%s" % out)
    print("仓库 %d 个，能锁 commit 的 %d 个" %
          (len(repos), manifest["summary"]["with_commit"]))
    if missing_lic:
        print("[!] 许可证缺失或无法识别：%s" % ", ".join(missing_lic))
    if nogit:
        print("[!] 非 git 仓库：%s" % ", ".join(nogit))
    if dirty:
        print("[!] 工作树有未提交改动：%s" % dirty)
    if a.json:
        print(json.dumps(manifest["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
