#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ZERO-04：跨模型对比 + 公开语料 provenance 汇总报告。

**诚实原则**：没有第二厂商 key 时，报告里那个模型标
`status: NOT_RUN` 并写明缺什么，绝不伪造数字或把同一模型换名当两家。

用法：
    python scripts/report_cross_model.py --out experiments/cross_model/report.json
"""
import argparse
import glob
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_provenance():
    """把所有公开语料的来源/许可/sha256 汇总（不依赖网络）。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(
            ROOT, "data", "external", "*", "manifest.json"))):
        name = os.path.basename(os.path.dirname(p))
        m = json.load(open(p, encoding="utf-8"))
        entry = {
            "manifest": os.path.relpath(p, ROOT),
            "manifest_sha256": _sha256_file(p),
            "source": m.get("source") or m.get("sources") or m.get("repo"),
            "license": m.get("license"),
            "license_url": m.get("license_url"),
            "version": m.get("version"),
            "note": m.get("note"),
        }
        # 逐条语料文件也记录实际 sha256（与 manifest 声明交叉核对）
        files = []
        for src in m.get("sources") or []:
            if not isinstance(src, dict):
                continue
            for f in src.get("files") or []:
                if isinstance(f, dict):
                    files.append({"path": f.get("path"),
                                  "declared_sha256": f.get("sha256")})
        for f in m.get("files") or []:
            if isinstance(f, dict):
                files.append({"path": f.get("path"),
                              "declared_sha256": f.get("sha256")})
        entry["declared_files"] = files[:50]
        # 本地实际语料文件
        local = []
        for casefile in glob.glob(os.path.join(
                os.path.dirname(p), "*.jsonl")) + glob.glob(
                os.path.join(os.path.dirname(p), "*.csv")):
            local.append({"path": os.path.relpath(casefile, ROOT),
                          "bytes": os.path.getsize(casefile),
                          "sha256": _sha256_file(casefile)})
        entry["local_files"] = local
        entry["self_built"] = "自行构造" in str(m.get("built", {}).get("note", ""))
        out[name] = entry
    return out


def _already_run_providers():
    """从既有实验产物里读真正跑过的 provider（不靠推断）。"""
    done = {}
    p = os.path.join(ROOT, "experiments", "coding_agent_real",
                     "ab_result.json")
    if os.path.isfile(p):
        r = json.load(open(p, encoding="utf-8"))
        prov = str(r.get("provider") or "").strip().lower()
        if prov:
            done[prov] = {"evidence": os.path.relpath(p, ROOT),
                          "model": r.get("model"),
                          "note": "既有真容器 A/B 产物，本轮未重跑"}
    return done


def cross_model_table():
    """跨模型对比：只有真跑过的模型才写数字。"""
    models = []
    ran = _already_run_providers()
    presets = GW.PROVIDER_PRESETS
    for key in ("deepseek", "qwen", "openai", "glm", "kimi", "ollama"):
        pre = presets.get(key)
        if not pre:
            continue
        backend, base, keyenv, model, _tools = pre
        # 本地/无 key 后端（如 ollama）不能用 key 判断可用性，
        # 没有真实探针结果就一样标 NOT_RUN，不能靠"key 不需要"冒充跑过。
        have_key = bool(keyenv and os.environ.get(keyenv))
        models.append({
            "provider": key,
            "model": model,
            "key_env": keyenv or None,
            "key_present": have_key,
            "status": ("DONE" if key in ran else "NOT_RUN"),
            "reason": (("已有实物证据（见 evidence）；本轮未重跑"
                        if key in ran else
                        ("未设置 %s；不伪造跨模型数字" % keyenv if keyenv else
                         "本地后端：未做真机探针，不把'无需 key'当跑过"))),
            "evidence": (ran.get(key) or {}).get("evidence"),
            "coding_agent": None,
            "env_invariant_40": None,
        })
    return models


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="experiments/cross_model/report.json")
    a = ap.parse_args()
    pub = os.path.join(ROOT, "experiments", "pub_bench.json")
    rep = {
        "version": 1,
        "purpose": "ZERO-04 跨模型与公开语料 provenance；未跑的模型标 NOT_RUN",
        "cross_model": cross_model_table(),
        "public_corpus_provenance": collect_provenance(),
        "latest_public_eval": (json.load(open(pub, encoding="utf-8"))
                               if os.path.isfile(pub) else None),
        "honest_note": (
            "本仓库当前只验证过 DeepSeek 单厂商。其它模型即使 key 存在，"
            "也必须真跑 eval_coding_agent.py / eval_env_invariant_bench.py "
            "并写入结果后才算 ELIGIBLE->DONE；本报告不代跑、不推算。"),
    }
    out = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    ran = [m["provider"] for m in rep["cross_model"]
           if m["status"] == "DONE"]
    print("跨模型真跑：%s；NOT_RUN：%d 个（如实标注，无伪造）"
          % (ran or "0 个",
             sum(1 for m in rep["cross_model"] if m["status"] == "NOT_RUN")))
    print("公开语料 provenance：%d 套，报告 %s"
          % (len(rep["public_corpus_provenance"]), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
