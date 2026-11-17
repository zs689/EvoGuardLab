#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WS-03-T3：按需下载外部模型资产到项目内缓存（gitignored），并校验 sha256。

    python scripts/fetch_pi_model.py --all
    python scripts/fetch_pi_model.py --verify
    python scripts/fetch_pi_model.py --install-parent-cache   # 可选：把已有的 ../_pi_model 复制进缓存

为什么需要：之前 ONNX 判官默认去父目录 `../_pi_model` 找模型，
独立 clone 后路径不存在，判官只能静默降级。这里把资产归属收回项目：
`.cache/models/pi/`（gitignore），由用户显式下载，不随仓库分发权重。

零第三方依赖：只用标准库下载与校验；ONNX 推理仍在独立 venv。
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.environ.get("AGENTLAB_MODEL_CACHE") or os.path.join(
    ROOT, ".cache", "models", "pi")
MANIFEST = os.path.join(ROOT, "docs", "reference", "pi-model-assets.json")
PARENT = os.path.join(os.path.dirname(ROOT), "_pi_model")

BASE = "https://hf-mirror.com/ProtectAI/deberta-v3-base-prompt-injection-v2/resolve/main/"


def load_manifest():
    with io.open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url, dst, size_hint=0):
    import urllib.request
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    req = urllib.request.Request(url, headers=hdr)
    tmp = dst + ".part"
    got = 0
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
    os.replace(tmp, dst)
    return got


def verify(verbose=True):
    m = load_manifest()
    rows = []
    for name, meta in m["assets"].items():
        p = os.path.join(CACHE, name)
        ok = os.path.isfile(p) and (not meta.get("sha256")
                                    or sha256(p) == meta["sha256"])
        rows.append({"name": name, "present": os.path.isfile(p),
                     "sha256_ok": bool(ok),
                     "size": os.path.getsize(p) if os.path.isfile(p) else 0})
        if verbose:
            print("  %-22s %s %s" % (name,
                  "OK" if ok else "MISSING/BAD", rows[-1]["size"]))
    return all(r["sha256_ok"] for r in rows), rows


def fetch(names):
    m = load_manifest()
    os.makedirs(CACHE, exist_ok=True)
    for name in names:
        meta = m["assets"].get(name)
        if not meta:
            print("[!] 未知资产：%s" % name)
            continue
        dst = os.path.join(CACHE, name)
        if os.path.isfile(dst) and (not meta.get("sha256")
                                    or sha256(dst) == meta["sha256"]):
            print("[skip] %s 已存在且校验通过" % name)
            continue
        print("[get] %s <- %s" % (name, meta["url"]))
        got = _download(meta["url"], dst, meta.get("size", 0))
        actual = sha256(dst)
        expect = meta.get("sha256")
        if expect and actual != expect:
            os.unlink(dst)
            print("[FAIL] %s sha256 不匹配（下载 %d 字节），已删除"
                  % (name, got))
            return 1
        print("[ok] %s (%d 字节, sha256 %s)" % (name, got, actual[:12]))
    return 0


def install_parent():
    """把已有的父目录模型复制进项目缓存（离线场景，不重新下载）。"""
    if not os.path.isdir(PARENT):
        print("[!] 父目录不存在：%s" % PARENT)
        return 1
    os.makedirs(CACHE, exist_ok=True)
    m = load_manifest()
    n = 0
    for name in m["assets"]:
        src, dst = os.path.join(PARENT, name), os.path.join(CACHE, name)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)
            print("[copy] %s" % name)
            n += 1
    print("复制 %d 个文件" % n)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--asset", action="append", default=[])
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--install-parent-cache", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    if a.install_parent_cache:
        return install_parent()
    if a.verify or not (a.all or a.asset):
        ok, rows = verify()
        print("缓存目录：%s" % CACHE)
        print("校验：%s" % ("PASS" if ok else "FAIL"))
        if a.json:
            with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"ok": ok, "rows": rows}, f, indent=2,
                          ensure_ascii=False, sort_keys=True)
                f.write("\n")
        return 0 if ok else 1
    names = list(load_manifest()["assets"]) if a.all else a.asset
    rc = fetch(names)
    if rc == 0:
        verify()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
