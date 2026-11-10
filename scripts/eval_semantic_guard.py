# -*- coding: utf-8 -*-
"""第三级语义判定器的评测：召回、误报、耗时、代价，一次说清。

对比三方：
  1. 一级正则（`security.guard_*`）
  2. 二级字符 n-gram（`deep_scan`，只在 tool_output / peer_message 适用）
  3. 三级本地语义判定（`semantic_guard`，慢但看意图）

评测集全部**只用于报数**，prompt 在开发期定稿并记录 sha256；
不允许按这批结果回头改 prompt（那等于拿测试集调参）。

用法：
    NO_PROXY=127.0.0.1,localhost python scripts/eval_semantic_guard.py
"""
import argparse
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import deep_scan as DS            # noqa: E402
import expanded_vectors_v1 as EX  # noqa: E402
import import_external_attacks as IM  # noqa: E402
import security as S              # noqa: E402
import semantic_guard as SG       # noqa: E402

OUT = os.path.join(ROOT, "experiments", "semantic_guard_v1")
INJ = os.path.join(ROOT, "data", "external", "injecagent", "cases.jsonl")
JAIL = os.path.join(ROOT, "data", "external", "jailbreak_classification",
                    "prompts.jsonl")
MODEL_JSON = os.path.join(ROOT, "experiments", "detector_evolution_v1",
                          "model.json")

CONFIG_CODES = EX.CONFIG_CODES


def _mode_of(entry):
    return "user_input" if entry == "user_input" else "tool_output"


def build_dataset(limit_per_source=40):
    """每类都带合法对照；抽样按固定步长，可复现。"""
    items = []

    # A. 30 个独立向量（攻击 + 相似合法对照）
    for vid, entry, tech, attack, normal, basis in EX.VECTORS:
        m = _mode_of(entry)
        items.append({"id": vid + ":attack", "source": "vectors_30",
                      "mode": m, "label": "attack", "text": attack,
                      "entry": entry})
        items.append({"id": vid + ":normal", "source": "vectors_30",
                      "mode": m, "label": "normal", "text": normal,
                      "entry": entry})

    # B. 外部直接越狱语料
    if os.path.isfile(JAIL):
        rows = [json.loads(l) for l in open(JAIL, encoding="utf-8")]
        jail = [r for r in rows if r["label"] == "jailbreak"]
        benign = [r for r in rows if r["label"] == "benign"]
        for r in jail[::max(1, len(jail) // limit_per_source)][:limit_per_source]:
            items.append({"id": r["case_id"] + ":attack",
                          "source": "jailbreak_corpus", "mode": "user_input",
                          "label": "attack", "text": r["text"],
                          "entry": "user_input"})
        for r in benign[::max(1, len(benign) // limit_per_source)][:limit_per_source]:
            items.append({"id": r["case_id"] + ":normal",
                          "source": "jailbreak_corpus", "mode": "user_input",
                          "label": "normal", "text": r["text"],
                          "entry": "user_input"})

    # C. InjecAgent 间接注入（base 是自然语言、enhanced 是字面话术）+ 最小对照
    if os.path.isfile(INJ):
        rows = [json.loads(l) for l in open(INJ, encoding="utf-8")]
        for variant in ("base", "enhanced"):
            sub = [r for r in rows if r["variant"] == variant]
            picked = sub[::max(1, len(sub) // (limit_per_source // 2))][
                :limit_per_source // 2]
            for r in picked:
                items.append({"id": r["case_id"] + ":attack",
                              "source": "injecagent_" + variant,
                              "mode": "tool_output", "label": "attack",
                              "text": r["tool_response"],
                              "entry": "tool_output"})
                ctrl, kind = IM.minimal_pair(
                    r, IM.filler_for(r["case_id"], IM.FILLER_UNSEEN))
                if kind == "minimal_pair":
                    items.append({"id": r["case_id"] + ":normal",
                                  "source": "injecagent_" + variant,
                                  "mode": "tool_output", "label": "normal",
                                  "text": ctrl, "entry": "tool_output"})
    return items


def level1(item):
    g = EX.guard_for(item["entry"], item["text"])
    codes = sorted({f["code"] for f in g["findings"]} - CONFIG_CODES)
    return bool(codes)


def level2(item, model):
    """二级字符模型只适用于 tool_output / peer_message。

    早先按 mode 判断，把 tool_call / model_output 也喂给了它 —— 这些落点
    本来就不该判「像不像指令」，结果合法对照被大面积误报，表里却显示 N/A。
    """
    if model is None or item["entry"] not in ("tool_output", "peer_message"):
        return None                          # 明确不适用
    return DS.classify(model, item["text"])["is_injection"]


def evaluate(items, cache_path):
    guard = SG.SemanticGuard(cache_path=cache_path)
    try:
        model = DS.load(MODEL_JSON) if os.path.isfile(MODEL_JSON) else None
    except Exception:                        # noqa: BLE001
        model = None
    rows = []
    for i, item in enumerate(items):
        l1 = level1(item)
        l2 = level2(item, model)
        r = guard.judge(item["mode"], item["text"])
        ms = r["ms"]
        l3 = r["is_attack"] if r["status"] == "observed" else None
        rows.append({
            "id": item["id"], "source": item["source"], "mode": item["mode"],
            "label": item["label"], "lvl1": l1, "lvl2": l2, "lvl3": l3,
            "l3_status": r["status"], "l3_reason": r["reason"],
            "l3_cached": r["cached"], "ms": round(ms, 1),
        })
        if (i + 1) % 25 == 0:
            print("  [%d/%d]" % (i + 1, len(items)), flush=True)
    return rows, guard


def _rate(n, d):
    return None if not d else round(n / float(d), 3)


def summarize(rows, guard):
    by = {}
    for r in rows:
        d = by.setdefault(r["source"], {"attack": [], "normal": []})
        d[r["label"]].append(r)

    def pack(rs):
        obs = [r for r in rs if r["l3_status"] == "observed"]
        pos = [r for r in obs if r["label"] == "attack"]
        neg = [r for r in obs if r["label"] == "normal"]
        return {
            "cases": len(rs),
            "lvl1_recall": _rate(sum(r["lvl1"] for r in rs
                                     if r["label"] == "attack"),
                                 len([r for r in rs if r["label"] == "attack"])),
            "lvl2_recall": (None if any(r["lvl2"] is None for r in rs) else
                            _rate(sum(bool(r["lvl2"]) for r in rs
                                      if r["label"] == "attack"),
                                  len([r for r in rs if r["label"] == "attack"]))),
            "lvl3_recall": _rate(sum(bool(r["lvl3"]) for r in pos), len(pos)),
            "lvl3_fp": _rate(sum(bool(r["lvl3"]) for r in neg), len(neg)),
            "lvl1_fp": _rate(sum(r["lvl1"] for r in neg), len(neg)),
            # 三级合并（OR）：按攻击类型分工，不是拿一个检测器包打天下
            "combined_recall": _rate(
                sum(1 for r in pos if r["lvl1"] or r["lvl2"] is True
                    or bool(r["lvl3"])), len(pos)),
            "combined_fp": _rate(
                sum(1 for r in neg if r["lvl1"] or r["lvl2"] is True
                    or bool(r["lvl3"])), len(neg)),
            "l3_na": len(rs) - len(obs),
        }

    # 缓存命中时 ms 仍是首次实测值；报告注明「耗时为首次调用」
    times = sorted(r["ms"] for r in rows if r["ms"])
    return {
        "cases": len(rows),
        "model_calls": guard.calls,
        "cached": sum(r["l3_cached"] for r in rows),
        "l3_na": sum(1 for r in rows if r["l3_status"] != "observed"),
        "by_source": {k: pack(v["attack"] + v["normal"])
                      for k, v in sorted(by.items())},
        "latency_ms": {"p50": round(statistics.median(times), 1) if times else None,
                       "p95": round(times[int(0.95 * (len(times) - 1))], 1)
                       if times else None,
                       "total": round(sum(times), 1) if times else None},
        "prompt_fingerprints": SG.prompt_fingerprints(),
        "model": SG.MODEL, "version": SG.VERSION,
    }


def _markdown(s):
    L = ["# 第三级：本地语义判定（换设计，不再堆正则）", "",
         "> 模型 `%s`（本地 Ollama，temperature=0 / think=false / JSON grammar）"
         % s["model"],
         "> prompt 指纹：`%s`"
         % "、".join("%s=%s" % (k, v[:12])
                    for k, v in s["prompt_fingerprints"].items()),
         "> %d 条样本 ｜ 实际模型调用 %d 次（缓存命中 %d）｜ 无法解析记 N/A %d 条"
         % (s["cases"], s["model_calls"], s["cached"], s["l3_na"]),
         "", "## 三级对比（每个来源分开报）", "",
         "| 来源 | 样本 | 一级 | 二级 | 三级 | **三级合并** | 三级误报 | 合并误报 | N/A |",
         "|---|---|---|---|---|---|---|---|---|"]
    for k, v in s["by_source"].items():
        L.append("| `%s` | %d | %s | %s | %s | **%s** | %s | %s | %d |"
                 % (k, v["cases"], v["lvl1_recall"],
                    v["lvl2_recall"] if v["lvl2_recall"] is not None else "N/A",
                    v["lvl3_recall"], v["combined_recall"],
                    v["lvl3_fp"], v["combined_fp"], v["l3_na"]))
    L += ["", "## 时间代价", "",
          "首次实测：p50 %s ms ｜ p95 %s ms ｜ 总 %s ms"
          % (s["latency_ms"]["p50"], s["latency_ms"]["p95"],
             s["latency_ms"]["total"]), "",
          "**分工结论**：正则擅长内容特征（密钥 / 命令 / XSS），语义判定擅长"
          "语义攻击（角色扮演 / 规则绕过），但两者都漏「自然语言间接注入」。"
          "合并（OR）能补上各自盲区，代价是误报也取 OR。",
          "**默认链建议只保留一级 + 三级**：二级字符模型在正常文本上误报过高"
          "（见 `docs/detector-evolution.md`），并入 OR 会把误报一起放大，"
          "保持实验性、不进默认判定。", "",
          "三级比一二级慢 3 个数量级，所以**只在前两级没命中时调用**，"
          "且按文本 sha256 缓存：同一份文本重跑不再付这份开销。", "",
          "## 方法与边界（不粉饰）", "",
          "- prompt 在开发期定稿并登记 sha256；这批样本**只用于报数**，"
          "不许按结果回头改 prompt（那等于拿测试集调参）。",
          "- 第二级字符模型在 `user_input` 落点**不适用**（正常请求会被全判注入），"
          "表里记 N/A，不拿它凑数。",
          "- 语义判定器也会漏：小模型把部分自然语言注入当成「普通请求」，"
          "报告里逐条留了 `l3_reason` 供人工复核。",
          "- 这是**检测件的对照评测**，不是端到端拦截成功率；"
          "检测结果要接进执行控制才谈得上拦住。",
          "- 模型不可用或输出不可解析时记 N/A，绝不猜一个布尔值。", ""]
    return "\n".join(L)


def run(out=OUT, limit=40):
    os.makedirs(out, exist_ok=True)
    items = build_dataset(limit)
    cache = os.path.join(out, "cache.jsonl")
    rows, guard = evaluate(items, cache)
    s = summarize(rows, guard)
    data = {"version": 1, "summary": s, "rows": rows}
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(s))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="第三级语义判定评测")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--limit", type=int, default=40,
                    help="每个外部来源抽多少条（控制模型调用次数）")
    args = ap.parse_args(argv)
    d = run(args.out, args.limit)
    s = d["summary"]
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("    %d 条 ｜ 模型调用 %d ｜ N/A %d ｜ p50 %s ms"
          % (s["cases"], s["model_calls"], s["l3_na"],
             s["latency_ms"]["p50"]))
    return d


if __name__ == "__main__":
    main()
