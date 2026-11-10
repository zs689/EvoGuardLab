# -*- coding: utf-8 -*-
"""接入「直接越狱话术」语料，补齐外部数据的另一面。

在已有 InjecAgent（**间接**注入，工具返回里藏指令）之后，还需要
**直接**越狱（用户把攻击话术直接塞进输入）这一类，才能谈覆盖面。

来源：`jackhhao/jailbreak-classification`（HuggingFace，Apache-2.0）。
本机 `huggingface.co` 不可达，但 `hf-mirror.com` 可达 —— 这里走镜像，
并把下载到的字节算 sha256 写进清单；仓库只留下载器、清单与报告，语料不入库。

用法：
    python scripts/import_jailbreak_prompts.py            # 走镜像下载
    python scripts/import_jailbreak_prompts.py --offline  # 只用缓存
"""
import argparse
import csv
import hashlib
import io
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "data", "external", "jailbreak_classification")
CACHE = os.path.join(ROOT, "_sandbox", "hf")

SOURCE = {
    "name": "jailbreak_classification",
    "repo": "jackhhao/jailbreak-classification",
    "license": "Apache-2.0",
    "license_url": "https://hf-mirror.com/datasets/jackhhao/jailbreak-classification/raw/main/README.md",
    "file": "default/jailbreak_dataset_full.csv",
    "url": ("https://hf-mirror.com/datasets/jackhhao/"
            "jailbreak-classification/resolve/main/default/"
            "jailbreak_dataset_full.csv"),
    "mirror_of": "https://huggingface.co/datasets/jackhhao/jailbreak-classification",
    "note": "这套是**直接越狱/正常**二分类话术，不是工具返回里的间接注入；"
            "入口应为 guard_input，二级模型不适用于该落点。",
}

MANIFEST_VERSION = "jailbreak-prompts-v1"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def fetch(offline=False, timeout=120):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "jailbreak_full.csv")
    if offline or os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read(), "cache"
    req = urllib.request.Request(SOURCE["url"], headers={
        "User-Agent": "EvoGuardLab/import-jailbreak-prompts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if len(data) < 10000:
        raise SystemExit("下载内容异常短（%d 字节），可能是重定向页" % len(data))
    with open(path, "wb") as f:
        f.write(data)
    return data, "network"


def normalize(raw):
    """CSV → 逐条记录。标签只认 jailbreak / benign，其余不猜。"""
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not {"prompt", "type"}.issubset(set(reader.fieldnames or [])):
        raise SystemExit("CSV 字段不符：%s" % reader.fieldnames)
    out = []
    for i, row in enumerate(reader):
        label = (row.get("type") or "").strip().lower()
        prompt = (row.get("prompt") or "").strip()
        if label not in ("jailbreak", "benign") or not prompt:
            continue
        out.append({
            "case_id": "%s#%04d" % (SOURCE["name"], i),
            "dataset": SOURCE["name"], "label": label,
            "text": prompt, "source_row": i,
            "category": "direct_jailbreak" if label == "jailbreak" else "benign",
        })
    return out


def build(out_dir=DEFAULT_OUT, offline=False, log=print):
    raw, how = fetch(offline)
    cases = normalize(raw)
    if not cases:
        raise SystemExit("归一化后没有样本")
    os.makedirs(out_dir, exist_ok=True)
    cases_path = os.path.join(out_dir, "prompts.jsonl")
    with open(cases_path, "w", encoding="utf-8", newline="\n") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    with open(cases_path, "rb") as f:
        cases_sha = _sha256(f.read())
    counts = {}
    for c in cases:
        counts[c["label"]] = counts.get(c["label"], 0) + 1
    manifest = {
        "version": MANIFEST_VERSION,
        "source": {k: v for k, v in SOURCE.items() if k != "url"},
        "download_url": SOURCE["url"],
        "download_mode": how,
        "raw_sha256": _sha256(raw),
        "raw_bytes": len(raw),
        "cases_file": "prompts.jsonl",
        "cases_sha256": cases_sha,
        "total_cases": len(cases),
        "by_label": counts,
        "note": "语料不入库：本文件只记录来源、许可、sha256 与条数。",
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log("  [√] %d 条（%s），原始 %.0f KB，来源 %s"
        % (len(cases), counts, len(raw) / 1024.0, how))
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="接入直接越狱话术语料")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args(argv)
    m = build(args.out, args.offline)
    print("[√] 产物：%s" % os.path.join(args.out, "prompts.jsonl"))
    print("    许可 %s ｜ 原始 sha256 %s"
          % (m["source"]["license"], m["raw_sha256"][:16]))
    return m


if __name__ == "__main__":
    main()
