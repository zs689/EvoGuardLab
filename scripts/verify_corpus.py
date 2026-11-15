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


def _collect_sources(m):
    """从顶层与嵌套 source/sources 收集上游声明。

    之前只读 m["license"] / m["repo"]，而 4 个 manifest 把 repo 和 license
    写在 `source` 或 `sources` 里 —— 于是「4 个源缺 license」其实是校验器
    的读取 bug，不是数据缺失。这里把三种形态都吃进来。
    """
    out = []

    def add(x):
        if isinstance(x, dict):
            out.append(x)

    if m.get("repo") or m.get("license") or m.get("license_url"):
        add({"repo": m.get("repo"), "license": m.get("license"),
             "license_url": m.get("license_url")})
    add(m.get("source"))
    srcs = m.get("sources")
    if isinstance(srcs, dict):
        for v in srcs.values():
            add(v)
    elif isinstance(srcs, list):
        for v in srcs:
            add(v)
    return out


def _content_hashes(m, srcs):
    """列出真正锁定**文件内容**的哈希字段（比 commit 更精确）。"""
    found = []
    for obj in [m] + srcs:
        for k in ("blob_sha", "git_blob_sha", "raw_sha256",
                  "cases_sha256", "sha256"):
            v = obj.get(k)
            if v and str(v).lower() not in PLACEHOLDER_COMMITS:
                found.append(k)
    for f in (m.get("files") or []):
        if isinstance(f, dict) and f.get("sha256"):
            found.append("files[].sha256")
    for f in (m.get("files") or []):
        if isinstance(f, dict) and f.get("git_sha") and \
                str(f["git_sha"]).lower() not in PLACEHOLDER_COMMITS:
            found.append("files[].git_sha")
    return sorted(set(found))


def build_entry(root, name):
    d = os.path.join(root, "data", "external", name)
    mf = os.path.join(d, "manifest.json")
    cases = os.path.join(d, "cases.jsonl")
    entry = {"name": name, "manifest": False, "cases": False,
             "license": None, "license_url": None, "repo": None,
             "commit": None, "pin_status": "unpinned",
             "content_hashes": [], "sources_count": 0,
             "upstream_commit": None, "upstream_evidence": None,
             "commit_status": None, "commit_verified": False,
             "from_upstream": [],
             "cases_sha256": None, "cases_bytes": None, "cases_lines": None,
             "upstream_files": 0}
    if os.path.isfile(mf):
        try:
            with io.open(mf, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError) as exc:
            entry["manifest_error"] = repr(exc)
            return entry
        srcs = _collect_sources(m)
        repos = sorted({x["repo"] for x in srcs if x.get("repo")})
        lics = sorted({x["license"] for x in srcs if x.get("license")})
        urls = sorted({x["license_url"] for x in srcs
                       if x.get("license_url")})
        hashes = _content_hashes(m, srcs)
        commit = m.get("commit") or m.get("git_sha")
        entry.update({
            "manifest": True,
            "license": "+".join(lics) or None,
            "license_url": (urls[0] if urls else m.get("license_url")),
            "repo": ", ".join(repos) or m.get("repo"),
            "commit": commit,
            "sources_count": len(srcs),
            "content_hashes": hashes,
            "upstream_files": len(m.get("files") or []),
        })
        # 内容哈希锁定 > commit 锁定 > 未锁定。
        # `cache` 是占位符，不算。
        c = str(commit or "").lower()
        if hashes:
            entry["pin_status"] = "content_pinned"
        elif c and c not in PLACEHOLDER_COMMITS:
            entry["pin_status"] = "commit_pinned"
        else:
            entry["pin_status"] = "unpinned"
        # 联网核实的不可变上游版本（可选文件，见 docs/corpus-supply-chain.md）
        up_path = os.path.join(root, "data", "corpus_upstream.json")
        if os.path.isfile(up_path):
            try:
                with io.open(up_path, encoding="utf-8") as f:
                    up = json.load(f).get("sources", {}).get(name)
                if up:
                    entry["upstream_commit"] = up.get("commit")
                    entry["upstream_evidence"] = up.get("evidence")
                    entry["commit_status"] = up.get("commit_status")
                    entry["commit_verified"] = str(
                        up.get("commit_status", "")).startswith("verified")
                    # 只在 manifest 缺字段时补全，并留痕来源
                    for k in ("license", "license_url"):
                        if not entry.get(k) and up.get(k):
                            entry[k] = up[k]
                            entry.setdefault("from_upstream", []).append(k)
            except (OSError, ValueError):
                pass
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
        if cur["pin_status"] == "unpinned":
            warnings.append("%s: 既无内容哈希也无上游 commit（git_sha=%r）"
                            % (name, cur["commit"]))
        if cur.get("pin_status") != "unpinned" and \
                not cur.get("commit_verified"):
            warnings.append(
                "%s: 上游 commit 未核实（内容 sha256 已锁，status=%s）"
                % (name, cur.get("commit_status") or "unknown"))
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
            print("  %-24s cases=%-5s lines=%-5s pin=%-15s license=%-12s repo=%s"
                  % (e["name"], e["cases"], e["cases_lines"],
                     e["pin_status"], e["license"], (e["repo"] or "-")[:38]))
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
