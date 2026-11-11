# -*- coding: utf-8 -*-
"""在中文语料上按**类别**测检测效果，并给逐 case 混淆矩阵。

为什么必须分类别（评审 R07 的延伸）：
`direct_jailbreak`（越狱手法）、`indirect_injection`（工具返回夹动作请求）、
`harmful_request`（有害意图请求）是三个**不同任务**。合成一个召回数字，
等于把「认不认得包装手法」「判不判动作越界」「做不做内容审核」搅在一起，
既不能说明能力，也不能说明缺陷。这里每一类单独出数字，并注明任务性质。

口径（评审 R07）：
  - 主口径是**逐 case**：每条样本算一次，分母是本类样本数；
  - 另外给逐手法宏平均与「最差手法」，但**不拿它当主指标**；
  - 不同检测器比较时必须同一样本、同一入口、同一单位。

输出只含统计、类别、手法代号，**不输出正文**。
"""
import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import action_scope as A  # noqa: E402
import security as S     # noqa: E402

DATA = os.path.join(ROOT, "data", "external", "chinese")
OUT = os.path.join(ROOT, "experiments", "chinese_corpus_v2")

# 每一类的入口与判据：入口不同，结论不能互相搬运。
ENTRY = {
    # 直接越狱：用户输入侧，看一级正则认不认得出
    "direct_jailbreak": {"entry": "user_input", "detector": "l1_regex"},
    # 有害意图请求：也是用户输入侧，但属内容审核任务
    "harmful_request": {"entry": "user_input", "detector": "l1_regex"},
    # 间接注入：工具返回侧，看「动作-职责不符」
    "indirect_injection": {"entry": "tool_output", "detector": "action_scope"},
}


def load():
    with open(os.path.join(DATA, "cases.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    with open(os.path.join(DATA, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    return rows, man


def _l1_hit(text):
    return [f["code"] for f in S.scan(text, "user_input")
            if f["code"].startswith("injection_")]


def _scope_hit(tool, text):
    f = A.check_output_scope(tool, text)
    return bool(f and f["code"] == "action_on_read_only_tool")


def confusion(rows, cls):
    """逐 case 混淆矩阵。只统计本类的攻击样本（清洁对照单独算）。"""
    spec = ENTRY[cls]
    tp = fn = fp = tn = 0
    per_tech = collections.defaultdict(lambda: [0, 0])   # tech -> [hit, total]
    misses = []
    for r in rows:
        if r["class"] != cls:
            continue
        hit = (_scope_hit(r["tool"], r["text"])
               if spec["detector"] == "action_scope" else bool(_l1_hit(r["text"])))
        if r["label"] == "attack":
            per_tech[r["technique"]][1] += 1
            if hit:
                tp += 1
                per_tech[r["technique"]][0] += 1
            else:
                fn += 1
                misses.append({"case_id": r["case_id"],
                               "technique": r["technique"],
                               "carrier": r.get("carrier")})
        else:
            fp += 1 if hit else 0
            tn += 0 if hit else 1
    return {
        "detector": spec["detector"], "entry": spec["entry"],
        "cases": {"tp": tp, "fn": fn, "fp": fp, "tn": tn},
        "recall_case": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "fp_rate_case": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "macro_by_technique": _macro(per_tech),
        "worst_techniques": _worst(per_tech),
        "misses": misses,
    }


def _macro(per_tech):
    rates = [h / t for h, t in per_tech.values() if t]
    if not rates:
        return None
    return round(sum(rates) / len(rates), 4)


def _worst(per_tech, k=5):
    rows = [{"technique": t, "hit": h, "n": n, "rate": round(h / n, 3)}
            for t, (h, n) in per_tech.items() if n]
    rows.sort(key=lambda x: (x["rate"], x["technique"]))
    return rows[:k]


def _clean_control(rows):
    """间接注入的清洁对照：同载体、同长度量级的正常文档。"""
    fp = tn = 0
    per_carrier = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        if r["class"] != "indirect_clean":
            continue
        hit = _scope_hit(r["tool"], r["text"])
        fp += 1 if hit else 0
        tn += 0 if hit else 1
        per_carrier[r["carrier"]][0] += 1 if hit else 0
        per_carrier[r["carrier"]][1] += 1
    return {"fp": fp, "tn": tn,
            "fp_rate": round(fp / (fp + tn), 4) if (fp + tn) else None,
            "by_carrier": {k: {"fp": v[0], "n": v[1]}
                           for k, v in sorted(per_carrier.items())}}


def run(out=OUT):
    rows, man = load()
    result = {"version": 2, "corpus": {
        "cases_sha256": man.get("cases_sha256"), "total": man.get("total"),
        "attacks": man.get("attacks"), "benign": man.get("benign"),
        "by_class": man.get("by_class"),
        "distinct_techniques": man.get("distinct_techniques"),
        "sources": {k: {"repo": v.get("repo"), "license": v.get("license"),
                        "parsed": v.get("parsed")}
                    for k, v in (man.get("sources") or {}).items()},
    }, "classes": {}}

    for cls in ("direct_jailbreak", "harmful_request", "indirect_injection"):
        d = confusion(rows, cls)
        if cls == "indirect_injection":
            d["clean_control"] = _clean_control(rows)
        result["classes"][cls] = d

    result["note"] = "不含正文；每类单独统计，不合并成一个召回数字。"
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(result))
    return result


def _markdown(d):
    c = d["corpus"]
    L = ["# 中文语料：三个任务分开测", "",
         "语料共 %d 条（攻击 %d / 正常 %d），不同手法 %d 种。"
         % (c["total"], c["attacks"], c["benign"], c["distinct_techniques"]),
         "", "来源与许可：", "",
         "| 来源 | 仓库 | 许可 | 解析 |", "|---|---|---|---|"]
    for k, v in sorted((c.get("sources") or {}).items()):
        L.append("| %s | %s | %s | %s 条 |"
                 % (k, v.get("repo"), v.get("license"), v.get("parsed")))
    L += ["", "另外：`indirect_injection` 那 120 条是**本项目自建**，不是公开语料。", "",
          "**必须分开看的原因**：越狱手法、间接注入、有害意图请求是三种不同任务。"
          "合成一个召回数字，既说不清能力，也说不清缺陷。", "",
          "## 1 中文越狱手法（direct_jailbreak）", "",
          "任务：检测器认不认得出「包装手法」。入口 `user_input`，一级正则。", ""]
    L += _cls_block(d["classes"]["direct_jailbreak"])

    L += ["## 2 中文有害意图请求（harmful_request）", "",
          "任务：**内容审核**，不是注入检测。这一类的召回接近 0 是预期内的 ——"
          "本检测器判的是「有没有注入/越权结构」，不判「这个问题该不该回答」。"
          "放在这里是为了**如实说明边界**，不是拿来凑总分。", ""]
    L += _cls_block(d["classes"]["harmful_request"])

    ind = d["classes"]["indirect_injection"]
    L += ["## 3 中文间接注入（indirect_injection）", "",
          "任务：工具返回里出现「要求助手执行动作」的句子，而该工具是只读的。"
          "入口 `tool_output`，判据是**动作-职责不符**（纯正则，零模型调用）。", "",
          "**这一集是本项目自建的**（`scripts/zh_indirect_cases.py`，"
          "10 载体 × 12 手法 = 120 条，互不重复）。规则是在同一批样本上定稿的，"
          "所以下面的召回是**样本内自测**，只能说明「这类写法现在能被认出来」，"
          "**不能当泛化能力**。泛化证据看英文 InjecAgent 的 held-out 结果"
          "（`experiments/action_scope_v2`）。", ""]
    L += _cls_block(ind)
    cc = ind.get("clean_control") or {}
    n = cc.get("tn", 0) + cc.get("fp", 0)
    L += ["清洁对照：同载体、同长度量级的正常业务文档 %d 条，误报 **%d/%d（%s）**。"
          % (n, cc.get("fp", 0), n, cc.get("fp_rate")), ""]

    L += ["## 口径与边界", "",
          "- 主指标是**逐 case 召回**；逐手法宏平均只作参考，不替代主指标。",
          "- 三个类别的**入口不同**（user_input / tool_output），"
          "它们的召回**不能互相比较**，也不能相加。",
          "- 间接注入集的规则在样本内定稿，属覆盖集自测；真正的 held-out 证据在"
          " `experiments/action_scope_v2`。",
          "- `harmful_request` 召回低是任务不匹配，不是实现缺陷；"
          "要做内容审核需要另一套模型与标注。",
          "- 语料不入库，报告只给类别、手法代号与统计，不含正文。", ""]
    return "\n".join(L)


def _cls_block(d):
    cases = d["cases"]
    L = ["| 指标 | 值 |", "|---|---|",
         "| 逐 case 召回 | **%s** |" % d["recall_case"],
         "| TP / FN | %d / %d |" % (cases["tp"], cases["fn"]),
         "| 逐手法宏平均（参考） | %s |" % d["macro_by_technique"], ""]
    w = d.get("worst_techniques") or []
    if w:
        L += ["最差的手法：", "", "| 手法 | 命中/总数 | 命中率 |", "|---|---|---|"]
        for x in w:
            L.append("| %s | %d/%d | %s |"
                     % (x["technique"], x["hit"], x["n"], x["rate"]))
        L.append("")
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="中文语料分类别评测")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    d = run(args.out)
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    for k, v in d["classes"].items():
        print("    %-20s 逐case召回 %s（%d/%d）宏平均 %s"
              % (k, v["recall_case"], v["cases"]["tp"],
                 v["cases"]["tp"] + v["cases"]["fn"], v["macro_by_technique"]))
    cc = d["classes"]["indirect_injection"].get("clean_control") or {}
    print("    间接注入清洁对照误报 %s" % cc.get("fp_rate"))
    return d


if __name__ == "__main__":
    main()
