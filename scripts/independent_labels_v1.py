# -*- coding: utf-8 -*-
"""两位**独立代理标注者**的一致性（Cohen's kappa）+ 分歧清单。

真人双标仍缺人；这里用两个原理完全独立的自动标注器充当代理：
  A = 规则标注器（security.guard_* 的内容信号，字面特征）
  B = 语义判定器（本地 qwen3:4b，看意图，不看关键词）

两者输入相同、判定原理不同，所以一致性有参考价值；但**这不是真人标注**，
也不能拿 kappa 宣称「标注质量达标」。协议与真人待复核清单保持原样。

产物：`experiments/independent_labels_v1/`。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard_inspect"))

from independent_labels import cohen_kappa, disagreements  # noqa: E402

SRC = os.path.join(ROOT, "experiments", "semantic_guard_v1", "result.json")
OUT = os.path.join(ROOT, "experiments", "independent_labels_v1")


def to_label(v, abstain="abstain"):
    if v is None:
        return abstain
    return "attack" if v else "normal"


def build_records(rows):
    a, b = [], []
    for r in rows:
        key = r["id"]
        a.append({"key": key, "truth": {
            "execution_state": to_label(r["lvl1"]),
            "observable_fault": r["source"],
            "business_outcome": None},
            "source": "rules_annotator_A"})
        b.append({"key": key, "truth": {
            "execution_state": to_label(r["lvl3"]),
            "observable_fault": r["source"],
            "business_outcome": None},
            "source": "semantic_annotator_B"})
    return a, b


def run(src=SRC, out=OUT):
    if not os.path.isfile(src):
        raise SystemExit("先跑 eval_semantic_guard.py 生成 %s" % src)
    with open(src, encoding="utf-8") as f:
        rows = json.load(f)["rows"]
    a, b = build_records(rows)
    la = [r["truth"]["execution_state"] for r in a]
    lb = [r["truth"]["execution_state"] for r in b]
    k = cohen_kappa(la, lb)
    diff = disagreements(a, b)
    by_source = {}
    for r in rows:
        d = by_source.setdefault(r["source"], {"n": 0, "agree": 0, "kappa": None})
        d["n"] += 1
    for r, x, y in zip(rows, la, lb):
        if x == y:
            by_source[r["source"]]["agree"] += 1
    for src_name, d in by_source.items():
        sub = [r for r in rows if r["source"] == src_name]
        sa = [to_label(r["lvl1"]) for r in sub]
        sb = [to_label(r["lvl3"]) for r in sub]
        d["kappa"] = cohen_kappa(sa, sb)
        d["agreement_rate"] = round(d["agree"] / float(d["n"]), 3)

    data = {
        "version": 1,
        "annotators": {
            "A": {"type": "rules", "principle": "字面/结构特征",
                  "is_human": False},
            "B": {"type": "local_llm", "principle": "语义意图判定",
                  "is_human": False},
        },
        "cases": len(rows),
        "cohen_kappa": k,
        "agreement_rate": round(sum(1 for x, y in zip(la, lb) if x == y)
                                / float(len(rows)), 3),
        "by_source": by_source,
        "disagreements": diff[:30],
        "disagreement_count": len(diff),
        "human_annotation_status": "仍缺真人双标；本结果是代理标注器一致性",
        "note": "kappa 为 None 表示分母为 0 或完全一致导致 pe=1，不写 0。",
    }
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    return data


def _markdown(d):
    L = ["# 双人标注一致性（代理标注者）", "",
         "> **不是真人标注**：A=规则标注器、B=本地语义判定器，"
         "原理独立但都是自动的。真人双标与 Cohen's kappa 仍缺人。",
         "> %d 条样本 ｜ kappa **%s** ｜ 一致率 %s ｜ 分歧 %d 条"
         % (d["cases"], d["cohen_kappa"], d["agreement_rate"],
            d["disagreement_count"]), "",
         "## 按来源", "", "| 来源 | 样本 | 一致率 | kappa |",
         "|---|---|---|---|"]
    for k, v in sorted(d["by_source"].items()):
        L.append("| `%s` | %d | %s | %s |"
                 % (k, v["n"], v["agreement_rate"], v["kappa"]))
    L += ["", "## 分歧样例（需人工讨论，不自动取多数）", ""]
    for x in d["disagreements"][:8]:
        L.append("- `%s` ｜ A=%s ｜ B=%s" % (x["key"], x["a"], x["b"]))
    L += ["", "## 边界", "",
          "- 两个自动标注器可能**共享同一种盲区**（例如自然语言间接注入），"
          "一致 ≠ 正确；分歧清单才是要人看的部分。",
          "- 真人独立标注协议在 `docs/external-labeling-protocol.md`，"
          "待复核清单在 `experiments/external_labeling_v1/`，"
          "没有真人参与前不宣称标注质量达标。", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="代理标注者 kappa")
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    d = run(args.src, args.out)
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("    %d 条 ｜ kappa %s ｜ 分歧 %d"
          % (d["cases"], d["cohen_kappa"], d["disagreement_count"]))
    return d


if __name__ == "__main__":
    main()
