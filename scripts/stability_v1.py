# -*- coding: utf-8 -*-
"""最后一轮：阈值版本登记 + 多 seed 稳定性 + 跨 Agent 迁移。

三件事都刻意分开报，不混成一个分数：
  A. 阈值版本登记：把 `_operability` 写死的 20% 抽到版本化配置，
     记录开发集分布与指纹，报告里可追溯。
  B. 多 seed：同一批任务跑 5 个 seed，看结果是否稳定、方差来自哪里。
  C. 跨 Agent 迁移：先测「架构现在允不允许」，再测「假设允许，会不会负迁移」。

产物：`experiments/stability_v1/`。
"""
import argparse
import glob
import hashlib
import json
import os
import statistics
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

import core        # noqa: E402
import runtime     # noqa: E402
import sim         # noqa: E402

OUT = os.path.join(ROOT, "experiments", "stability_v1")
CONFIG = os.path.join(ROOT, "config", "thresholds_v1.json")
SEEDS = (7, 11, 23, 41, 59)


def _sha(obj):
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def development_distribution():
    """从开发用轨迹夹具统计分布；不写死数字，也不碰最终测试集。"""
    files = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures",
                                         "trace_*.json")))
    spans = traces = err = 0
    per_file = {}
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:                    # noqa: BLE001
            continue
        ss = [s for rs in d.get("resourceSpans", [])
              for sc in rs.get("scopeSpans", []) for s in sc.get("spans", [])]
        e = sum(1 for s in ss
                if str((s.get("status") or {}).get("code", "")).upper()
                in ("ERROR", "2") or s.get("status") == "error")
        n = len(ss)
        per_file[os.path.basename(p)] = {"spans": n, "error_spans": e}
        traces += 1
        spans += n
        err += e
    return {
        "source": "tests/fixtures/trace_*.json（开发用轨迹夹具，非最终测试集）",
        "files": len(files), "traces": traces, "spans": spans,
        "error_spans": err,
        "observed_error_rate": round(err / float(spans), 4) if spans else None,
        "per_file": per_file,
        "note": "只记录开发集分布，不在测试集上反复调阈值追求通过；"
                "20% 仍是工程经验值，不是从这批数据拟合出来的。",
    }


def register_thresholds():
    dist = development_distribution()
    cfg = {
        "version": "thresholds-v1",
        "applies_to": "evoguard_inspect.assurance._operability / model_error_rate",
        "model_error_fail": 0.2,
        "model_error_any": 0.0,
        "development_set": dist,
        "status": "已登记版本与适用场景；阈值本身尚未用独立开发集标定",
        "rationale": "20% 是工程经验值。改它必须先记录开发集分布与误报案例，"
                     "不允许在最终测试集上反复调阈值追求通过。",
    }
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(CONFIG, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    return {"config_path": "config/thresholds_v1.json",
            "config_sha256": sha, "fingerprint": _sha(cfg),
            "development_traces": dist["traces"],
            "observed_error_rate": dist["observed_error_rate"]}


def _run_case(store, agent, task, fault, seed, mode="D", skill=None):
    exp = store.create_experiment(
        name="stability/%s/%s/%s/%s" % (agent, task, fault, seed),
        agent_type=agent, task_id=task, fault_type=fault, fault_target=0,
        seed=seed, recovery_mode=mode)
    runtime.FORCED_SKILL = skill
    try:
        run = runtime.run_once(store, exp, mode)
    finally:
        runtime.FORCED_SKILL = None
    return bool(run["success"] == 1)


def _cases(n_tasks=3, n_faults=3):
    out = []
    for agent in sim.AGENT_TYPES:
        tasks = [t["task_id"] for t in sim.dev_tasks(agent)[:n_tasks]]
        faults = sim.CASE_FAULTS[agent][:n_faults]
        for t in tasks:
            for f in faults:
                out.append((agent, t, f))
    return out


def multi_seed(store):
    cases = _cases()
    per_seed, per_case = {}, []
    for seed in SEEDS:
        results = [_run_case(store, a, t, f, seed) for a, t, f in cases]
        per_seed[str(seed)] = {
            "cases": len(results),
            "success": sum(results),
            "rate": round(sum(results) / float(len(results)), 3),
        }
    for i, (a, t, f) in enumerate(cases):
        vals = []
        # 每个 seed 单独算，避免同一 store 里的运行互相影响判定
        for seed in SEEDS:
            vals.append(_run_case(store, a, t, f, seed))
        per_case.append({"agent": a, "task": t, "fault": f,
                         "successes": sum(vals), "seeds": len(SEEDS),
                         "stable": len(set(vals)) == 1})
    rates = [v["rate"] for v in per_seed.values()]
    return {
        "seeds": list(SEEDS), "cases_per_seed": len(cases),
        "per_seed": per_seed,
        "mean_rate": round(statistics.mean(rates), 3),
        "stdev": round(statistics.pstdev(rates), 3),
        "stable_cases": sum(c["stable"] for c in per_case),
        "total_cases": len(per_case),
        "recheck_cases": len(cases),
        "interpretation": "仿真器的故障注入是确定性的（seed 只登记在 manifest，"
                          "不改变落点），所以方差为 0 是设计结果，"
                          "不能据此宣称真实模型下也稳定。",
    }


def cross_agent_transfer(store):
    """先测架构允许不允许，再测假设允许时的增益/负迁移。"""
    # 一份「来自 order 的技能」：策略链来自 order 常见的恢复动作
    order_skill = {"id": "sk_transfer_order", "task_type": "order",
                   "fault_type": None, "strategy": ["retry", "recompute"]}
    cases = []
    for agent in ("kb", "planner"):
        tasks = [t["task_id"] for t in sim.dev_tasks(agent)[:3]]
        faults = sim.CASE_FAULTS[agent][:3]
        cases.extend((agent, t, f) for t in tasks for f in faults)

    architecture, paired = [], []
    for a, t, f in cases:
        base = _run_case(store, a, t, f, 7)
        # 原样注入：runtime 要求 task_type == agent_type，预期**不生效**
        as_is = _run_case(store, a, t, f, 7, skill=dict(order_skill))
        architecture.append({"agent": a, "task": t, "fault": f,
                             "baseline": base, "with_order_skill": as_is,
                             "skill_applied": as_is != base})
        # 模拟「如果真把经验迁过去」：把 task_type 改成目标 agent
        moved = dict(order_skill, task_type=a, id="sk_transfer_%s" % a)
        after = _run_case(store, a, t, f, 7, skill=moved)
        paired.append({"key": "%s/%s/%s" % (a, t, f),
                       "before": base, "after": after})

    gains = [int(y["after"]) - int(y["before"]) for y in paired]
    regressions = sum(1 for y, g in zip(paired, gains)
                      if y["before"] and not y["after"])
    return {
        "architecture_allows_transfer": any(x["skill_applied"] for x in architecture),
        "architecture_note": "runtime._decide 要求 skill.task_type == agent_type；"
                             "order 的技能直接用到 kb/planner 时不会被采用。",
        "architecture_cases": len(architecture),
        "simulated_transfer": {
            "cases": len(paired),
            "gain": round(sum(gains) / float(len(gains)), 3) if gains else None,
            "negative_transfer_rate": round(regressions / float(len(paired)), 3)
            if paired else None,
            "regressions": regressions,
            "verdict": ("PROMOTE" if (gains and sum(gains) > 0 and regressions == 0)
                        else "REJECT"),
            "note": "把 task_type 改成目标 agent 是**模拟**迁移，不等于架构已支持；"
                    "业务收益不能抵消负迁移。",
        },
    }


def _markdown(data):
    t, s, x = data["thresholds"], data["multi_seed"], data["cross_agent"]
    L = ["# 最后一轮：阈值版本 / 多 seed / 跨 Agent 迁移", "",
         "## 一、阈值版本登记", "",
         "| 项 | 值 |", "|---|---|",
         "| 配置 | `%s` |" % t["config_path"],
         "| sha256 | `%s` |" % t["config_sha256"][:16],
         "| 开发集轨迹 | %d 条（夹具，非最终测试集） |" % t["development_traces"],
         "| 观测报错率 | %s |" % t["observed_error_rate"],
         "| 阈值 | fail > 20%%，risk > 0%%（**工程经验值**，未标定） |",
         "",
         "`assurance.build()` 现在把 `thresholds.version / fingerprint / source` "
         "写进报告；配置文件缺失或非法时回退内置值并标 `source=builtin`，"
         "不静默换口径。**不在最终测试集上反复调阈值追求通过。**", "",
         "## 二、多 seed", "",
         "| seed | 用例 | 成功率 |", "|---|---|---|"]
    for k, v in s["per_seed"].items():
        L.append("| %s | %d | %s |" % (k, v["cases"], v["rate"]))
    L += ["", "均值 %s ｜ 总体标准差 %s ｜ 稳定用例 %d/%d"
          % (s["mean_rate"], s["stdev"], s["stable_cases"], s["total_cases"]),
          "", "> %s" % s["interpretation"], "",
          "## 三、跨 Agent 迁移", "",
          "- 架构现状：**%s**（%s）"
          % ("允许" if x["architecture_allows_transfer"] else "不允许",
             x["architecture_note"]),
          "- 模拟迁移（把 `task_type` 改成目标 agent）：%d 条成对用例，"
          "增益 %s、负迁移率 %s、判定 **%s**"
          % (x["simulated_transfer"]["cases"], x["simulated_transfer"]["gain"],
             x["simulated_transfer"]["negative_transfer_rate"],
             x["simulated_transfer"]["verdict"]),
          "- %s" % x["simulated_transfer"]["note"], "",
          "## 边界", "",
          "- 三件事分开报，不合成一个分数。",
          "- 多 seed 与迁移实验都在临时 SQLite 库上跑，不碰仓库 `data/`。",
          "- 仿真的确定性不代表真实模型稳定；迁移实验是模拟，不是已打通的能力。", ""]
    return "\n".join(L)


def run(out=OUT):
    os.makedirs(out, exist_ok=True)
    t = register_thresholds()
    with tempfile.TemporaryDirectory(prefix="stability_") as tmp:
        store = core.Store(os.path.join(tmp, "s.db"), fast=True)
        for mod in ("experience", "skills"):
            __import__(mod).ensure_schema(store)
        s = multi_seed(store)
        x = cross_agent_transfer(store)
        store.close()
    data = {"version": 1, "thresholds": t, "multi_seed": s, "cross_agent": x}
    for name, payload in (("thresholds.json", t), ("multi_seed.json", s),
                          ("cross_agent.json", x)):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="阈值登记 / 多 seed / 跨 Agent 迁移")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    d = run(args.out)
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("    阈值 sha %s ｜ 多 seed 标准差 %s ｜ 架构允许迁移 %s ｜ 模拟迁移 %s"
          % (d["thresholds"]["config_sha256"][:12], d["multi_seed"]["stdev"],
             d["cross_agent"]["architecture_allows_transfer"],
             d["cross_agent"]["simulated_transfer"]["verdict"]))
    return d


if __name__ == "__main__":
    main()
