#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-018：语料供应链校验 + 锁清单。

    python scripts/verify_corpus.py                 # 校验（语料缺失=未导入，不算失败）
    python scripts/verify_corpus.py --write-lock    # 重新生成 data/corpus_lock.json
    python scripts/verify_corpus.py --strict        # 未锁定上游 commit 也算失败

现状如实说明：`data/external/*/manifest.json` 里 `git_sha` 写的是 `cache`
占位符，**不是不可变的上游 commit**。所以锁清单把每个源标成
`pin_status: unpinned`，`--strict` 会因此失败 —— 这是已知缺口，不假装锁定。
真正能锁的是**生成产物 `cases.jsonl` 的 sha256/行数**：语料被改一个字节
就会被发现。语料本体按许可与体积不入库，锁清单入库。
"""
import argparse
import hashlib
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "data", "corpus_lock.json")
PLACEHOLDER_COMMITS = frozenset(("cache", "", "unknown", "head", "main", "master"))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_lines(path):
    n = 0
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def list_sources(root=ROOT):
    """只把「有 manifest 或 cases.jsonl」的目录当语料源。

    `label_sheet/` 是人工标注页，不是语料，不能因为它在 external/ 下
    就被判成「缺 manifest」。
    """
    d = os.path.join(root, "data", "external")
    if not os.path.isdir(d):
        return []
    out = []
    for x in sorted(os.listdir(d)):
        p = os.path.join(d, x)
        if not os.path.isdir(p):
            continue
        if os.path.isfile(os.path.join(p, "manifest.json")) or \
                os.path.isfile(os.path.join(p, "cases.jsonl")):
            out.append(x)
    return out


def build_entry(root, name):
    d = os.path.join(root, "data", "external", name)
    mf = os.path.join(d, "manifest.json")
    cases = os.path.join(d, "cases.jsonl")
    entry = {"name": name, "manifest": False, "cases": False,
             "license": None, "license_url": None, "repo": None,
             "commit": None, "pin_status": "unpinned",
             "cases_sha256": None, "cases_bytes": None, "cases_lines": None,
             "upstream_files": 0}
    if os.path.isfile(mf):
        try:
            with io.open(mf, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError) as exc:
            entry["manifest_error"] = repr(exc)
            return entry
        entry.update({
            "manifest": True,
            "license": m.get("license"),
            "license_url": m.get("license_url"),
            "repo": m.get("repo"),
            "commit": m.get("commit") or m.get("git_sha"),
            "upstream_files": len(m.get("files") or []),
        })
        commit = str(entry["commit"] or "").lower()
        entry["pin_status"] = ("unpinned" if (not commit or
                               commit in PLACEHOLDER_COMMITS) else "pinned")
    if os.path.isfile(cases):
        entry.update({
            "cases": True,
            "cases_sha256": _sha256(cases),
            "cases_bytes": os.path.getsize(cases),
            "cases_lines": _count_lines(cases),
        })
    return entry


def verify(root=ROOT, lock_path=None):
    """返回 (ok, report)。

    `problems` 是**篡改类**问题（哈希不符、行数不符、manifest 损坏），
    默认就判失败；`warnings` 是**既有归档状态**（缺许可、上游未锁定），
    默认只报不改判，`--strict` 才算失败 —— 不把历史遗留当成本轮回归。
    语料目录整体缺失时 ok=True 但标 not_imported。
    """
    lock_path = lock_path or os.path.join(root, "data", "corpus_lock.json")
    lock = {}
    if os.path.isfile(lock_path):
        try:
            with io.open(lock_path, encoding="utf-8") as f:
                lock = json.load(f)
        except (OSError, ValueError):
            lock = {}
    expected = {e["name"]: e for e in (lock.get("sources") or [])}
    names = list_sources(root)
    problems, warnings, entries = [], [], []
    for name in names:
        cur = build_entry(root, name)
        exp = expected.get(name)
        entries.append(cur)
        if cur.get("manifest_error"):
            problems.append("%s: manifest 解析失败 %s"
                            % (name, cur["manifest_error"]))
            continue
        if not cur["manifest"]:
            problems.append("%s: 缺 manifest.json" % name)
            continue
        if not cur["license"] or not cur["license_url"]:
            warnings.append("%s: manifest 缺 license/license_url" % name)
        if exp and exp.get("cases_sha256") and cur["cases"]:
            if exp["cases_sha256"] != cur["cases_sha256"]:
                problems.append("%s: cases.jsonl 哈希与锁清单不一致"
                                % name)
        if exp and exp.get("cases_lines") and cur["cases"]:
            if exp["cases_lines"] != cur["cases_lines"]:
                problems.append("%s: 有效行数 %s != 锁清单 %s"
                                % (name, cur["cases_lines"],
                                   exp["cases_lines"]))
        if cur["pin_status"] != "pinned":
            warnings.append("%s: 上游 commit 未锁定（git_sha=%r）"
                            % (name, cur["commit"]))
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources_present": len(names),
        "not_imported": not names,
        "lock_present": bool(lock),
        "entries": entries,
        "problems": problems,
        "warnings": warnings,
    }
    return (not problems), report


def write_lock(root=ROOT, lock_path=None):
    lock_path = lock_path or os.path.join(root, "data", "corpus_lock.json")
    entries = [build_entry(root, n) for n in list_sources(root)]
    doc = {
        "schema": 1,
        "purpose": "语料本体不入库；这里锁生成产物的哈希、许可与上游版本状态。",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": entries,
    }
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    tmp = lock_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, lock_path)
    return doc


def main():
    ap = argparse.ArgumentParser(description="语料供应链校验")
    ap.add_argument("--write-lock", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="未锁定上游 commit 也算失败")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.write_lock:
        doc = write_lock()
        print("[√] %s（%d 个源）" % (LOCK, len(doc["sources"])))
        return 0

    ok, rep = verify()
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        if rep["not_imported"]:
            print("未导入外部语料（data/external 不存在）—— 不算失败，"
                  "但依赖语料的测试会 skip。")
        for e in rep["entries"]:
            print("  %-24s cases=%s lines=%s pin=%s license=%s"
                  % (e["name"], e["cases"], e["cases_lines"],
                     e["pin_status"], e["license"]))
        for p in rep["problems"]:
            print("  ! " + p)
        for w in rep["warnings"]:
            print("  ~ " + w)
        print("== %s ==" % ("PASS" if ok else "FAIL"))
    if not ok:
        return 1
    if rep["warnings"]:
        if a.strict:
            print("[×] --strict：%d 条供应链告警判失败（缺许可 / 上游未锁定）。"
                  % len(rep["warnings"]))
            return 1
        print("[!] %d 条供应链告警（缺许可 / 上游 commit 未锁定）；"
              "加 --strict 判失败，见 docs/corpus-supply-chain.md。"
              % len(rep["warnings"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
