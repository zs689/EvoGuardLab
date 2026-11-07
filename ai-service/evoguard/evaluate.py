# -*- coding: utf-8 -*-
"""评测：A/B/C 三组对照 + 指标 + Markdown 报告。

这是**唯一**允许读 faults 表（故障真值）的模块。
诊断模块 runtime.diagnose() 拿不到它，所以指标算不算得准，取决于这里有没有认真比对。
"""
import os
import statistics

import agents
import runtime
import sim
from core import FAILED, SUCCEEDED, now

# 四组对照：A 无恢复 / B 固定规则 / C 不严格验证的经验 / D 完整进化闭环
MODES = ("baseline", "A", "B", "C", "D")


def build_cases(limit=None):
    """构造测试用例：3 个 agent × 各自任务 × 8 类故障 × 1 个 seed。"""
    cases = []
    for at in sim.AGENT_TYPES:
        for task in sim.TASKS[at]:
            for ft in sim.CASE_FAULTS[at]:
                cases.append({
                    "agent_type": at,
                    "task_id": task["task_id"],
                    "fault_type": ft,
                    "fault_target": 0,     # 0 = 按 seed 自动选一步
                    "seed": 7,
                    "split": sim.split_of(at, task["task_id"]),
                })
    if limit:
        cases = cases[:limit]
    return cases


def run_case(store, case, name_prefix=""):
    """跑一个用例的 4 种模式，返回原始记录。"""
    exp = store.create_experiment(
        name="%s%s/%s/%s" % (name_prefix, case["agent_type"], case["task_id"],
                             case["fault_type"]),
        agent_type=case["agent_type"], task_id=case["task_id"],
        fault_type=case["fault_type"], fault_target=case["fault_target"],
        seed=case["seed"], recovery_mode="C")

    out = {"case": case, "experiment_id": exp["id"], "runs": {}}
    for mode in MODES:
        run = runtime.run_once(store, exp, mode)
        out["runs"][mode] = run
    return out


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def _pctl(values, p):
    if not values:
        return 0
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
    return values[k]


def aggregate(store, results):
    """把一个用例集的原始记录聚合成指标。"""
    m = {}
    n = len(results)
    m["cases"] = n

    for mode in MODES:
        ok = sum(1 for r in results if r["runs"][mode]["success"] == 1)
        m["success_%s" % mode] = ok
        m["rate_%s" % mode] = _pct(ok, n)

    # ---- 故障检测（只看 C 模式，与真值比对）----
    tp = fp = fn = 0
    loc_ok = loc_n = 0
    for r in results:
        rid = r["runs"]["C"]["id"]
        faults = store.get_faults(rid)
        truth = faults[0]["fault_type"] if faults else None
        target = faults[0]["target_seq"] if faults else None
        dia = store.get_diagnosis(rid)
        detected = dia is not None and dia["root_cause"] not in ("unknown",)
        if truth and detected:
            if dia["root_cause"] == truth:
                tp += 1
            else:
                fp += 1
        elif truth and not detected:
            fn += 1
        if dia and target:
            loc_n += 1
            if dia["first_bad_step"] == target:
                loc_ok += 1

    m["detect_tp"], m["detect_fp"], m["detect_fn"] = tp, fp, fn
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    m["detect_precision"] = round(prec, 3)
    m["detect_recall"] = round(rec, 3)
    m["detect_f1"] = round(2 * prec * rec / (prec + rec), 3) if (prec + rec) else 0.0
    m["localize_accuracy"] = round(loc_ok / loc_n, 3) if loc_n else 0.0
    m["localize_n"] = loc_n

    # ---- 恢复 ----
    a_failed = [r for r in results if r["runs"]["A"]["success"] == 0]
    m["a_failed"] = len(a_failed)
    for mode in ("B", "C", "D"):
        rec_ok = sum(1 for r in a_failed if r["runs"][mode]["success"] == 1)
        m["recover_%s" % mode] = rec_ok
        m["recover_rate_%s" % mode] = _pct(rec_ok, len(a_failed))

    # 误恢复：A 本来就成功，却在 B/C 里变失败（说明恢复动作伤了它）
    false_recovery = 0
    for r in results:
        if r["runs"]["A"]["success"] == 1:
            for mode in ("B", "C", "D"):
                if r["runs"][mode]["success"] == 0:
                    false_recovery += 1
    m["false_recovery"] = false_recovery
    m["false_recovery_rate"] = _pct(false_recovery, n * 2)

    # ---- 成本与延迟 ----
    for mode in MODES:
        lat = [r["runs"][mode]["latency_ms"] or 0 for r in results]
        tok = [r["runs"][mode]["tokens"] or 0 for r in results]
        m["latency_p50_%s" % mode] = _pctl(lat, 50)
        m["latency_p95_%s" % mode] = _pctl(lat, 95)
        m["tokens_avg_%s" % mode] = round(statistics.mean(tok), 1) if tok else 0

    # ---- 步骤数 ----
    for mode in ("A", "C"):
        cnt = [len(store.list_steps(r["runs"][mode]["id"])) for r in results]
        m["steps_avg_%s" % mode] = round(statistics.mean(cnt), 1) if cnt else 0

    # ---- 副作用（幂等性证据）----
    m["side_effects_total"] = runtime.SIDE_EFFECT_COUNT["n"]

    # ---- 失败案例（必须列出来，不许只报成功）----
    fails = []
    for r in results:
        if r["runs"]["C"]["success"] == 0:
            dia = store.get_diagnosis(r["runs"]["C"]["id"])
            fails.append({
                "agent": r["case"]["agent_type"],
                "task": r["case"]["task_id"],
                "fault": r["case"]["fault_type"],
                "root_cause": (dia or {}).get("root_cause") or "-",
                "correct_located": ((dia or {}).get("first_bad_step") or 0),
                "note": r["runs"]["C"].get("note") or "",
            })
    m["failures"] = fails
    m["failures_n"] = len(fails)
    return m


def to_markdown(store, experiment_ids, metrics, title="AgentLab 实验结果"):
    L = ["# %s" % title, ""]
    L.append("> 生成时间 %s ｜ 用例数 %d ｜ 实验数 %d"
             % (now(), metrics["cases"], len(experiment_ids)))
    L.append("")
    L.append("## 一、任务成功率")
    L.append("")
    L.append("| 模式 | 含义 | 成功 | 成功率 |")
    L.append("|---|---|---|---|")
    L.append("| baseline | 无故障 | %d | %s%% |" % (metrics["success_baseline"], metrics["rate_baseline"]))
    L.append("| A | 有故障、不恢复 | %d | %s%% |" % (metrics["success_A"], metrics["rate_A"]))
    L.append("| B | 有故障、固定规则恢复 | %d | %s%% |" % (metrics["success_B"], metrics["rate_B"]))
    L.append("| C | 有故障、诊断+自适应恢复 | %d | %s%% |" % (metrics["success_C"], metrics["rate_C"]))
    L.append("")
    L.append("## 二、故障检测与定位")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append("| Precision | %s |" % metrics["detect_precision"])
    L.append("| Recall | %s |" % metrics["detect_recall"])
    L.append("| F1 | %s |" % metrics["detect_f1"])
    L.append("| TP / FP / FN | %d / %d / %d |" % (metrics["detect_tp"], metrics["detect_fp"], metrics["detect_fn"]))
    L.append("| 根因定位准确率 | %s（%d 个有真值样本） |" % (metrics["localize_accuracy"], metrics["localize_n"]))
    L.append("")
    L.append("## 三、恢复效果")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append("| A 模式失败用例数 | %d |" % metrics["a_failed"])
    L.append("| 固定规则恢复成功 | %d（%s%%） |" % (metrics["recover_B"], metrics["recover_rate_B"]))
    L.append("| 诊断式恢复成功 | %d（%s%%） |" % (metrics["recover_C"], metrics["recover_rate_C"]))
    L.append("| 误恢复数 | %d（%s%%） |" % (metrics["false_recovery"], metrics["false_recovery_rate"]))
    L.append("| 写操作重放次数（幂等性证据） | %d |" % metrics["side_effects_total"])
    L.append("")
    L.append("## 四、成本与延迟")
    L.append("")
    L.append("| 模式 | 平均 Token | P50 延迟(ms) | P95 延迟(ms) | 平均步骤数 |")
    L.append("|---|---|---|---|---|")
    for mode in MODES:
        steps = metrics.get("steps_avg_%s" % mode, "-")
        L.append("| %s | %s | %s | %s | %s |" % (
            mode, metrics["tokens_avg_%s" % mode], metrics["latency_p50_%s" % mode],
            metrics["latency_p95_%s" % mode], steps))
    L.append("")
    L.append("## 五、仍然失败的用例（%d 条，不隐藏）" % metrics["failures_n"])
    L.append("")
    if metrics["failures"]:
        L.append("| Agent | 任务 | 故障 | 诊断根因 | 定位步 | 备注 |")
        L.append("|---|---|---|---|---|---|")
        for f in metrics["failures"]:
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                f["agent"], f["task"], f["fault"], f["root_cause"],
                f["correct_located"], f["note"][:40]))
    else:
        L.append("无。")
    L.append("")
    L.append("## 六、怎么读这份报告")
    L.append("")
    L.append("1. **baseline 必须接近 100%%**，否则是任务本身有问题，不是恢复机制的问题。")
    L.append("2. **A 必须明显低于 baseline**，否则说明故障没注入进去，整套实验失效。")
    L.append("3. **C 应该高于 B**。如果 C ≈ B，说明诊断没带来额外价值，需要改策略表。")
    L.append("4. 失败案例比成功率更重要：静默篡改类故障如果大规模失败，说明验算规则覆盖不够。")
    return "\n".join(L)
