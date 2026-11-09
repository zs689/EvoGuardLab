# -*- coding: utf-8 -*-
"""在标注集上跑规则，算检出率/误报率/不可判定。

跑法：
    python scripts/eval_labeled.py
产物：
    experiments/labeled_eval/{report.md,result.json}

这一步的用途是**回归护栏**：改规则后跑一遍，看误伤有没有变多、漏报有没有变多。
它不是「准确率证明」—— 标签来自注入状态，只覆盖已知故障。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import config, coverage, labels, loaders  # noqa: E402
from evoguard_inspect.rules import registry, trace_rules           # noqa: E402

LABELS = os.path.join(ROOT, "tests", "fixtures", "labeled", "labels.json")
OUT = os.path.join(ROOT, "experiments", "labeled_eval")


def main():
    doc = json.load(open(LABELS, encoding="utf-8"))
    records = []
    skipped = []
    for e in doc["entries"]:
        path = os.path.join(ROOT, e["file"])
        try:
            traces = loaders.load_file(path)
        except Exception as ex:
            skipped.append({"key": e["key"], "error": "%s: %s" % (type(ex).__name__, ex)})
            continue
        if not traces:
            skipped.append({"key": e["key"], "error": "解析出 0 条轨迹"})
            continue
        tr = traces[0]
        # 白名单按该轨迹实际用到的工具给，否则「越权」那条会假阳性
        from evoguard_inspect.config import DEFAULTS
        cfg = dict(DEFAULTS)
        cfg["allowed_tools"] = sorted({s.tool or s.name for s in tr.tools()})
        cfg = config.Config(**cfg)
        issues = trace_rules.check_all([tr], cfg, registry.select())
        records.append(labels.make_record(e["key"], e["label"], tr, issues, cfg))

    result = labels.summarize(records)
    result["protocol"] = doc.get("protocol")
    result["backend"] = doc.get("backend")
    result["skipped"] = skipped
    result["limits"] = doc.get("limits", [])
    result["records"] = records

    os.makedirs(OUT, exist_ok=True)
    json.dump(result, open(os.path.join(OUT, "result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    open(os.path.join(OUT, "report.md"), "w", encoding="utf-8").write(render(result))

    c = result["confusion"]
    print("轨迹 %d 条（可判定 %d，不可判定 %d）"
          % (result["n_total"], result["n_judged"], result["n_unjudgeable"]))
    ca = result["confusion_alert"]
    print("  确诊(error) %d/%d recall=%s precision=%s ｜ 提示(warn+) %d/%d recall=%s"
          % (c["tp"], c["tp"] + c["fn"], result["recall"], result["precision"],
             ca["tp"], ca["tp"] + ca["fn"], result["recall_alert"]))
    print("  误报：error 级 %d / warn+ 级 %d（good 共 %d 条）"
          % (c["fp"], ca["fp"], c["tn"] + c["fp"]))
    print("  产物：%s" % os.path.join(OUT, "report.md"))


def render(r):
    c = r["confusion"]
    L = ["# 标注集评测（规则回归护栏）", "",
         "> 标签来自执行时的注入状态，协议 `%s`，后端 `%s`。" % (r["protocol"], r["backend"]),
         "> 它衡量「规则在已知故障上的行为」，**不是**真实分布下的准确率。", "",
         "## 一、总览", "",
         "| 项 | 值 |", "|---|---|",
         "| 轨迹总数 | %d |" % r["n_total"],
         "| 可判定 | %d |" % r["n_judged"],
         "| 不可判定（无 error 级规则可跑） | %d |" % r["n_unjudgeable"],
         "| 检出（failed 里报了 error） | %d / %d |" % (c["tp"], c["tp"] + c["fn"]),
         "| 误报（clean/recovered 里报了 error） | %d / %d |"
         % (c["fp"], c["fp"] + c["tn"]),
         "", "**两档口径**（很多规则按设计就是 warn，只用 error 算召回会全算漏报）：", "",
         "| 口径 | 检出 | recall | 误报 | precision |", "|---|---|---|---|---|",
         "| 确诊（error 级） | %d/%d | %s | %d | %s |"
         % (c["tp"], c["tp"] + c["fn"], r["recall"], c["fp"], r["precision"]),
         "| 提示（warn 级及以上） | %d/%d | %s | %d | %s |"
         % (r["confusion_alert"]["tp"], r["confusion_alert"]["tp"] + r["confusion_alert"]["fn"],
            r["recall_alert"], r["confusion_alert"]["fp"], r["precision_alert"]),
         ""]
    L += ["## 二、分标签", "", "| 标签 | 条数 | 报 error | 比率 |", "|---|---|---|---|"]
    for name in ("failed", "clean", "recovered"):
        d = r["by_label"][name]
        rate = d.get("recall") if name == "failed" else d.get("false_alarm_rate")
        L.append("| %s | %d | %d | %s |" % (name, d["n"], d["flagged"], rate))
    L += ["", "`recovered` 单列，是因为「已恢复的故障被报成错误」是这类工具最典型的误伤。", ""]
    L += ["## 三、failed 上命中的规则（召回的下限，不是全部）", ""]
    if r["rules_hit_on_failed"]:
        L += ["| 规则 | 命中条数 |", "|---|---|"]
        for rid, n in r["rules_hit_on_failed"].items():
            L.append("| `%s` | %d |" % (rid, n))
    else:
        L.append("（无）")
    L += ["", "## 四、不可判定", ""]
    if r["unjudgeable_keys"]:
        L.append("这些轨迹上没有任何 error 级规则具备输入条件，已剔出分母：")
        L += ["- `%s`" % k for k in r["unjudgeable_keys"]]
    else:
        L.append("无。")
    if r["skipped"]:
        L += ["", "解析失败的条目："] + ["- `%s`：%s" % (s["key"], s["error"])
                                     for s in r["skipped"]]
    L += ["", "## 六、这份评测的边界", ""]
    L += ["- %s" % x for x in r["limits"]]
    L += ["", "因此引用时要写「在 49 条构造集上 recall=… 」，不能写「准确率 …」。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
