# -*- coding: utf-8 -*-
"""在直接越狱语料上量一级检测：召回与误报必须一起报。

和 InjecAgent 那条链的区别：
  - InjecAgent 是**间接注入**（工具返回里藏指令）→ `guard_tool_output`；
  - 这套是**直接越狱**（用户把话术塞进输入）→ `guard_input`。
  两类不能混成一个准确率。

特别说明：`deep_scan` 的第二级模型**不适用于这里**。它在 16/16 条正常用户请求上
全报注入，学的是「像不像指令」；挂到 user_input 会把所有请求都拦掉
（见 docs/detector-evolution.md）。所以本报告只评一级正则，不伪装成端到端防护。
"""
import argparse
import collections
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import security as S  # noqa: E402

DEFAULT_SRC = os.path.join(ROOT, "data", "external", "jailbreak_classification")
DEFAULT_OUT = os.path.join(ROOT, "experiments", "jailbreak_eval_v1")

INJECTION_CODES = {d["code"] for d in S.DETECTORS
                   if d["code"].startswith("injection_")}


def load(src):
    with open(os.path.join(src, "prompts.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def evaluate(cases):
    rows, t = [], []
    for c in cases:
        t0 = time.perf_counter()
        g = S.guard_input(c["text"])
        t.append((time.perf_counter() - t0) * 1000.0)
        codes = {f["code"] for f in g["findings"]}
        rows.append({
            "case_id": c["case_id"], "label": c["label"],
            "any_signal": bool(codes),
            "injection_signal": bool(codes & INJECTION_CODES),
            "codes": sorted(codes),
            "decision": g["decision"],
            "executed": g["executed"],
        })
    return rows, t


def _rate(n, d):
    return None if not d else round(n / float(d), 3)


def summarize(rows, times):
    pos = [r for r in rows if r["label"] == "jailbreak"]
    neg = [r for r in rows if r["label"] == "benign"]
    counts = collections.Counter()
    pos_codes = collections.Counter()
    neg_codes = collections.Counter()
    for r in rows:
        counts.update(r["codes"])
        (pos_codes if r["label"] == "jailbreak" else neg_codes).update(r["codes"])
    times = sorted(times)
    return {
        "cases": len(rows), "jailbreak": len(pos), "benign": len(neg),
        "recall_any_signal": _rate(sum(r["any_signal"] for r in pos), len(pos)),
        "recall_injection_signal": _rate(
            sum(r["injection_signal"] for r in pos), len(pos)),
        "fp_any_signal": _rate(sum(r["any_signal"] for r in neg), len(neg)),
        "fp_injection_signal": _rate(
            sum(r["injection_signal"] for r in neg), len(neg)),
        "any_execution_claimed": sum(r["executed"] for r in rows),
        "signal_counts": dict(counts.most_common(12)),
        "top_jailbreak_signals": dict(pos_codes.most_common(8)),
        "top_benign_signals": dict(neg_codes.most_common(8)),
        "latency_ms": {
            "p50": round(times[len(times) // 2], 3) if times else None,
            "p95": round(times[int(0.95 * (len(times) - 1))], 3) if times else None,
        },
    }


def samples(cases, rows, kind, k=5):
    by_id = {c["case_id"]: c for c in cases}
    if kind == "miss":
        picked = [r for r in rows if r["label"] == "jailbreak"
                  and not r["any_signal"]][:k]
    else:
        picked = [r for r in rows if r["label"] == "benign"
                  and r["any_signal"]][:k]
    return [{"case_id": r["case_id"], "codes": r["codes"],
             "text": by_id[r["case_id"]]["text"][:180]} for r in picked]


def _markdown(data, manifest):
    s = data["summary"]
    L = ["# 直接越狱话术上的检测能力（外部语料）", "",
         "> 语料：`%s` ｜ 许可 **%s** ｜ 通过 `hf-mirror.com` 获取（官方域本机不可达）"
         % (manifest["source"]["repo"], manifest["source"]["license"]),
         "> %d 条：越狱 %d / 正常 %d ｜ 入口 `guard_input`（audit 档）"
         % (s["cases"], s["jailbreak"], s["benign"]),
         "> 原始 CSV sha256 `%s`" % manifest["raw_sha256"][:16],
         "", "## 召回与误报必须一起看", "",
         "| 指标 | 值 | 说明 |", "|---|---|---|",
         "| 越狱召回（任意信号） | %s | 含密钥/PII/命令等，会虚高 |" % s["recall_any_signal"],
         "| 越狱召回（注入类信号） | %s | 只算 `injection_*` |" % s["recall_injection_signal"],
         "| 正常误报（任意信号） | %s | |" % s["fp_any_signal"],
         "| 正常误报（注入类信号） | %s | |" % s["fp_injection_signal"],
         "| 声称执行的次数 | %s | 检测层必须恒为 0 |" % s["any_execution_claimed"],
         "", "耗时：p50 %s ms / p95 %s ms。"
         % (s["latency_ms"]["p50"], s["latency_ms"]["p95"]),
         "", "## 命中的是哪些信号", "",
         "| 越狱侧 | 次数 | 正常侧 | 次数 |", "|---|---|---|---|"]
    p = list(s["top_jailbreak_signals"].items())
    n = list(s["top_benign_signals"].items())
    for i in range(max(len(p), len(n))):
        a = "`%s`" % p[i][0] if i < len(p) else "—"
        av = p[i][1] if i < len(p) else ""
        b = "`%s`" % n[i][0] if i < len(n) else "—"
        bv = n[i][1] if i < len(n) else ""
        L.append("| %s | %s | %s | %s |" % (a, av, b, bv))
    L += ["", "## 漏掉的越狱样例", ""]
    for x in data["samples"]["misses"]:
        L += ["- `%s` ｜ %s" % (x["case_id"], x["codes"] or "无信号"),
              "  - %s" % x["text"].replace("\n", " ")]
    L += ["", "## 误报的正常样例", ""]
    for x in data["samples"]["false_positives"]:
        L += ["- `%s` ｜ %s" % (x["case_id"], x["codes"]),
              "  - %s" % x["text"].replace("\n", " ")]
    L += ["", "## 边界（不粉饰）", "",
          "- 这套是**直接越狱**（用户输入侧），和 InjecAgent 的**间接注入**"
          "（工具返回侧）是两类；不能把两边的数字加在一起当总召回。",
          "- 第二级字符模型**不适用于这个入口**：它在 16/16 条正常用户请求上"
          "全报注入（见 `docs/detector-evolution.md`），所以本报告只评一级正则。",
          "- 这里量的是「检测件看见什么」，不是「防住了越狱」：agent 是规则式的，"
          "检测结果要接进执行控制才谈得上拦住。",
          "- 语料只用于对照评测、不随仓库分发；引用遵循上游 Apache-2.0。",
          "", "## 复现", "", "```bash",
          "python scripts/import_jailbreak_prompts.py",
          "python scripts/eval_jailbreak_prompts.py", "```", ""]
    return "\n".join(L)


def run(src=DEFAULT_SRC, out=DEFAULT_OUT):
    cases = load(src)
    with open(os.path.join(src, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    rows, times = evaluate(cases)
    data = {
        "version": 1, "entry_point": "security.guard_input", "mode": "audit",
        "summary": summarize(rows, times),
        "samples": {"misses": samples(cases, rows, "miss"),
                    "false_positives": samples(cases, rows, "fp")},
        "rows": rows,
        "deep_scan_applicable": False,
        "deep_scan_reason": "第二级模型在 user_input 上会把正常请求全判为注入",
    }
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data, manifest))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="直接越狱话术检测评测")
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    d = run(args.src, args.out)
    s = d["summary"]
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("    越狱召回 任意 %s / 注入类 %s ｜ 正常误报 任意 %s / 注入类 %s"
          % (s["recall_any_signal"], s["recall_injection_signal"],
             s["fp_any_signal"], s["fp_injection_signal"]))
    return d


if __name__ == "__main__":
    main()
