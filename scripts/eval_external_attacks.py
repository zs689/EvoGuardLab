# -*- coding: utf-8 -*-
"""在外部语料上量检测能力，并且**分开**报「命中」和「认出攻击」。

为什么要拆：InjecAgent 的工具返回里本来就带被偷的 PII（邮箱、电话），
`pii_email` 一命中就算「检测到攻击」，命中率会好看得离谱，但那是内容特征，
不是注入特征。这里强制分开算四个数：

  1. `injection_recall`   —— 注入类检测器（injection_*）命中率：真能力
  2. `any_signal_rate`    —— 任何检测器命中率：含 PII/密钥/命令等，会虚高
  3. `control_fp_rate`    —— 干净对照（模板里注入位换成正常评价）上的误报率
  4. `payload_recall`     —— 只扫攻击者那句话的命中率，用来暴露
                             「payload 单独能看到，埋进大返回里就被稀释」

产物只落 `experiments/external_attacks_v1/`，语料仍在 `data/external/`（不入库）。
"""
import argparse
import json
import os
import sys
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import security as S  # noqa: E402

DEFAULT_DIR = os.path.join(ROOT, "data", "external", "injecagent")
DEFAULT_OUT = os.path.join(ROOT, "experiments", "external_attacks_v1")
BENIGN = "Great product, works as described."

INJECTION_CODES = {c for c in (d["code"] for d in S.DETECTORS)
                   if c.startswith("injection_")}


def load_cases(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _hit(codes):
    return bool(codes & INJECTION_CODES)


def evaluate(cases):
    """逐条跑守卫，返回逐条结果与汇总（不写盘，便于测试）。"""
    rows = []
    for c in cases:
        g = S.guard_tool_output(c["user_tool"], c["tool_response"])
        codes = {f["code"] for f in g["findings"]}
        payload = S.scan(c["attacker_instruction"], "payload")
        payload_codes = {f["code"] for f in payload}
        ctrl_text = (c.get("tool_response_template") or "").replace(
            "<Attacker Instruction>", BENIGN)
        ctrl_codes = set()
        if ctrl_text.strip():
            cg = S.guard_tool_output(c["user_tool"], ctrl_text)
            ctrl_codes = {f["code"] for f in cg["findings"]}
        rows.append({
            "case_id": c["case_id"], "goal": c["goal"], "variant": c["variant"],
            "category": c["category"],
            "injection_hit": _hit(codes),
            "any_signal": bool(codes),
            "blocked": g["action"] == "blocked",
            "codes": sorted(codes),
            "payload_hit": _hit(payload_codes),
            "payload_codes": sorted(payload_codes),
            "control_fp": bool(ctrl_codes),
            "control_codes": sorted(ctrl_codes),
            "has_control": bool(ctrl_text.strip()),
        })
    return rows


def _rate(n, d):
    return None if not d else round(n / float(d), 3)


def summarize(rows):
    n = len(rows)
    inj = sum(1 for r in rows if r["injection_hit"])
    any_ = sum(1 for r in rows if r["any_signal"])
    blocked = sum(1 for r in rows if r["blocked"])
    payload = sum(1 for r in rows if r["payload_hit"])
    ctrl = [r for r in rows if r["has_control"]]
    fp = sum(1 for r in ctrl if r["control_fp"])
    fp_inj = sum(1 for r in ctrl if r["control_codes"]
                 and _hit(set(r["control_codes"])))
    fp_codes = collections.Counter()
    for r in ctrl:
        fp_codes.update(r["control_codes"])

    def group(key):
        out = {}
        for name in sorted({r[key] for r in rows}):
            sub = [r for r in rows if r[key] == name]
            out[name] = {
                "cases": len(sub),
                "injection_recall": _rate(
                    sum(1 for r in sub if r["injection_hit"]), len(sub)),
                "any_signal_rate": _rate(
                    sum(1 for r in sub if r["any_signal"]), len(sub)),
                "payload_recall": _rate(
                    sum(1 for r in sub if r["payload_hit"]), len(sub)),
            }
        return out

    codes = collections.Counter()
    miss_codes = collections.Counter()
    for r in rows:
        codes.update(r["codes"])
        if not r["injection_hit"]:
            miss_codes.update(r["codes"])
    return {
        "cases": n,
        "injection_recall": _rate(inj, n),
        "any_signal_rate": _rate(any_, n),
        "blocked_rate": _rate(blocked, n),
        "payload_recall": _rate(payload, n),
        "control_cases": len(ctrl),
        "control_fp_rate": _rate(fp, len(ctrl)),
        "control_fp_injection_rate": _rate(fp_inj, len(ctrl)),
        "control_fp_codes": dict(fp_codes.most_common(8)),
        "by_goal": group("goal"),
        "by_category": group("category"),
        "by_variant": group("variant"),
        "signal_counts": dict(codes.most_common(12)),
        "only_pii_signals": sum(1 for r in rows if r["any_signal"]
                                and not r["injection_hit"]),
        "injection_codes": sorted(INJECTION_CODES),
    }


def samples(rows, cases, kind, k=5):
    """挑出可人工复核的样例：漏掉的注入 / 干净对照上的误报。"""
    by_id = {c["case_id"]: c for c in cases}
    if kind == "miss":
        picked = [r for r in rows if not r["injection_hit"]][:k]
    else:
        picked = [r for r in rows if r["control_fp"]][:k]
    out = []
    for r in picked:
        c = by_id[r["case_id"]]
        out.append({
            "case_id": r["case_id"],
            "category": r["category"],
            "attacker_instruction": c["attacker_instruction"][:200],
            "codes": r["codes"] if kind == "miss" else r["control_codes"],
        })
    return out


def _markdown(data, manifest):
    s = data["summary"]
    L = ["# 外部语料上的检测能力（InjecAgent）", "",
         "> 语料：%s ｜ 许可：%s ｜ 用例 %d 条（%s）"
         % (manifest["sources"][0]["repo"],
            manifest["sources"][0]["license"], s["cases"],
            "、".join("%s %d" % (k, v) for k, v in
                     sorted(manifest["by_goal"].items()))),
         "> 入口：`guard_tool_output(user_tool, tool_response)`，"
         "`audit` 档（只检测，不改内容、不停调用）。",
         "", "## 先把数字拆开，别混着看", "",
         "| 指标 | 值 | 这条数说明什么 |",
         "|---|---|---|",
         "| 注入特征命中率 | %s | 只有 `injection_*` 类检测器算命中 —— **这是真能力** |"
         % s["injection_recall"],
         "| 任意信号命中率 | %s | 含 PII / 密钥 / 外带 / 命令等，会虚高 |"
         % s["any_signal_rate"],
         "| 判为 blocked 的比例 | %s | audit 档下 blocked 只是「建议拦」 |"
         % s["blocked_rate"],
         "| 干净对照误报率 | %s（%d 条对照） | 模板里注入位换成正常评价后的误报 |"
         % (s["control_fp_rate"], s["control_cases"]),
         "| 只扫攻击句子的命中率 | %s | payload 单独可见，埋进大返回就被稀释 |"
         % s["payload_recall"],
         "",
         "「任意信号命中」里有 %d 条**只有 PII 类信号**、没有注入特征："
         "这些是工具返回里本来就有的邮箱/电话，不能算认出攻击。" % s["only_pii_signals"],
         "", "## 按目标与类别", "",
         "| 目标 | 用例 | 注入命中 | 任意信号 | 只扫载荷 |", "|---|---|---|---|---|"]
    for k, v in s["by_goal"].items():
        L.append("| %s | %d | %s | %s | %s |" % (k, v["cases"], v["injection_recall"],
                                                 v["any_signal_rate"],
                                                 v["payload_recall"]))
    L += ["", "| 类别 | 用例 | 注入命中 | 任意信号 |", "|---|---|---|---|"]
    for k, v in s["by_category"].items():
        L.append("| %s | %d | %s | %s |" % (k, v["cases"], v["injection_recall"],
                                            v["any_signal_rate"]))
    L += ["", "## 基础版 vs 增强版（这条差距才是真问题）", "",
          "| 变体 | 用例 | 注入命中 | 任意信号 |", "|---|---|---|---|"]
    for k, v in s["by_variant"].items():
        L.append("| %s | %d | %s | %s |" % (k, v["cases"], v["injection_recall"],
                                            v["any_signal_rate"]))
    L += ["", "注入类检测器在干净对照上的误报率：**%s**；"
          "整体误报率 %s 里，绝大部分来自内容类信号："
          % (s["control_fp_injection_rate"], s["control_fp_rate"]), "",
          "| 误报信号 | 条数 |", "|---|---|"]
    for k, v in s["control_fp_codes"].items():
        L.append("| `%s`%s | %d |" % (k, "（注入类）" if k in INJECTION_CODES else "", v))
    L += ["", "## 命中的是哪些信号", "",
          "| 检测器 | 命中条数 |", "|---|---|"]
    for k, v in s["signal_counts"].items():
        L.append("| `%s`%s | %d |" % (k, "（注入类）" if k in INJECTION_CODES else "", v))
    L += ["", "## 漏掉的样例（要人工看）", ""]
    for x in data["mistakes"]["misses"]:
        L += ["- `%s` ｜ %s ｜ 已有信号：%s" % (x["case_id"], x["category"],
                                          x["codes"] or "无"),
              "  - 攻击句：%s" % x["attacker_instruction"].replace("\n", " ")]
    L += ["", "## 干净对照上的误报样例", ""]
    for x in data["mistakes"]["false_positives"]:
        L += ["- `%s` ｜ 误报信号：%s" % (x["case_id"], x["codes"])]
    L += ["", "## 结论与边界（不粉饰）", "",
          "- 这个数**不是**端到端攻击成功率：本仓库的 Agent 是规则式的，"
          "把检测结果接进执行控制才谈得上拦住；这里量的是「检测件看见什么」。",
          "- 正则能认出**字面**注入指令（越权/角色切换/提示泄露/工具指令），"
          "认不出纯语义改写；`payload_recall` 与 `injection_recall` 的差距"
          "就是「埋进长返回后被稀释」的代价。",
          "- 干净对照的误报主要来自内容特征（PII/邮箱），说明把 `pii_*` 当"
          "「攻击命中」会同时抬高能力与误报，两侧都不可信。",
          "- 语料只用于对照评测，不随仓库分发；引用请遵循上游 MIT 许可与论文。",
          "", "## 复现", "", "```bash",
          "python scripts/import_external_attacks.py      # 拉取 + 核验 sha256",
          "python scripts/eval_external_attacks.py        # 出这份报告",
          "```", ""]
    return "\n".join(L)


def run(src_dir=DEFAULT_DIR, out_dir=DEFAULT_OUT):
    cases_path = os.path.join(src_dir, "cases.jsonl")
    if not os.path.isfile(cases_path):
        raise SystemExit("找不到语料：%s\n先跑 python scripts/import_external_attacks.py"
                         % cases_path)
    with open(os.path.join(src_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    cases = load_cases(cases_path)
    rows = evaluate(cases)
    data = {
        "version": 1,
        "entry_point": "security.guard_tool_output",
        "mode": "audit",
        "injection_codes": sorted(INJECTION_CODES),
        "benign_filler": BENIGN,
        "summary": summarize(rows),
        "mistakes": {"misses": samples(rows, cases, "miss"),
                     "false_positives": samples(rows, cases, "fp")},
        "rows": rows,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data, manifest))
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"corpus": manifest, "evaluator": {
            "entry_point": "security.guard_tool_output", "mode": "audit",
            "benign_filler": BENIGN, "injection_codes": sorted(INJECTION_CODES)}},
            f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="外部语料检测能力评测")
    ap.add_argument("--src", default=DEFAULT_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    d = run(args.src, args.out)
    s = d["summary"]
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("    注入特征命中 %s ｜ 任意信号 %s ｜ 干净对照误报 %s ｜ 只扫载荷 %s"
          % (s["injection_recall"], s["any_signal_rate"],
             s["control_fp_rate"], s["payload_recall"]))
    return d


if __name__ == "__main__":
    main()
