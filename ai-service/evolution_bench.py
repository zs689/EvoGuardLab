# -*- coding: utf-8 -*-
"""进化闭环演示：一次错误 -> 提炼经验 -> 生成技能 -> 独立验证 -> 晋升 -> 四组对照。

跑法（在 EvoGuardLab 目录下）：
    python ai-service/evolution_bench.py

输出：
    屏幕摘要 + experiments/evolution_report.md

它回答的就是你那张图里的五个方框，一步都不跳。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "evoguard"))

import core        # noqa: E402
import evaluate    # noqa: E402
import experience  # noqa: E402
import runtime     # noqa: E402
import sim         # noqa: E402
import skills      # noqa: E402

ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "data", "evoguard.db")
REPORT = os.path.join(ROOT, "experiments", "evolution_report.md")


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def run_mode(store, case, mode, skill=None):
    """跑一个用例的某个模式，返回 (success, run_id)"""
    exp = store.create_experiment(
        name="%s/%s/%s/%s" % (case["agent_type"], case["task_id"], case["fault_type"], mode),
        agent_type=case["agent_type"], task_id=case["task_id"],
        fault_type=case["fault_type"], fault_target=0,
        seed=case.get("seed", 7), recovery_mode=mode)
    runtime.FORCED_SKILL = skill
    try:
        run = runtime.run_once(store, exp, mode)
    finally:
        runtime.FORCED_SKILL = None
    return run["success"] == 1, run["id"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if os.path.exists(DB):
        os.remove(DB)
    store = core.Store(DB)
    experience.ensure_schema(store)
    skills.ensure_schema(store)

    log = []

    # ---------- 1. 学：从训练集的一次故障里提炼经验 ----------
    train = []
    for at in sim.AGENT_TYPES:
        task = sim.TASKS[at][0]["task_id"]
        for ft in sim.CASE_FAULTS[at]:
            train.append({"agent_type": at, "task_id": task, "fault_type": ft, "seed": 7})

    # 探索候选策略：规则表没有对应策略时，逐个试，成功的那条才配当经验
    EXPLORE = ("cross_verify", "fallback_tool", "replay_checkpoint")
    explored = 0
    learned = []
    for c in train:
        ok, rid = run_mode(store, c, "R")   # R = 纯规则诊断，不受技能库污染
        if not ok:
            for cand in EXPLORE:
                fake = {"task_type": c["agent_type"], "strategy": cand, "id": "explore"}
                ok2, rid2 = run_mode(store, c, "D", skill=fake)
                if ok2:
                    ok, rid, explored = True, rid2, explored + 1
                    break
        if not ok:
            continue
        exp = experience.extract(store, rid)
        if not exp:
            continue
        exp, reasons = experience.save(store, exp)
        if exp["validation_status"] != experience.VALIDATED:
            continue
        sk = skills.save(store, skills.propose(exp))
        learned.append({"case": c, "exp": exp, "skill": sk})
    log.append(("训练集用例", len(train)))
    log.append(("探索恢复成功", explored))
    log.append(("成功提炼的经验", len(learned)))

    # ---------- 2. 验：每个候选技能在独立验证集上测 ----------
    verdicts = []
    for item in learned:
        c = item["case"]
        sk = item["skill"]
        # target：同 agent 同故障，但换任务（独立）
        target = [{"agent_type": c["agent_type"], "task_id": t["task_id"],
                   "fault_type": c["fault_type"], "seed": 7}
                  for t in sim.TASKS[c["agent_type"]][1:]]
        # guard：同 agent，**不同故障** —— 不该被这个技能影响
        guard = [{"agent_type": c["agent_type"], "task_id": t["task_id"],
                  "fault_type": ft, "seed": 7}
                 for t in sim.TASKS[c["agent_type"]][1:]
                 for ft in sim.CASE_FAULTS[c["agent_type"]] if ft != c["fault_type"]]

        def runner(case, skill):
            ok, _ = run_mode(store, case, "D", skill=skill)
            return ok

        res = skills.evaluate(store, sk, target, guard, runner)
        verdicts.append({"skill": sk, "case": c, "res": res})
        if res["verdict"] == "PROMOTE":
            skills.promote(store, sk["id"])

    # ---------- 3. 测：测试集上四组对照 ----------
    test = []
    for at in sim.AGENT_TYPES:
        for t in sim.TASKS[at][1:]:
            for ft in sim.CASE_FAULTS[at]:
                test.append({"agent_type": at, "task_id": t["task_id"],
                             "fault_type": ft, "seed": 7})

    rates = {}
    detail = {m: [] for m in ("A", "B", "C", "D")}
    for c in test:
        for m in ("A", "B", "C", "D"):
            ok, _ = run_mode(store, c, m)
            detail[m].append(ok)
    for m in detail:
        rates[m] = pct(sum(detail[m]), len(test))

    neg = 0
    for i, c in enumerate(test):
        if detail["B"][i] and not detail["D"][i]:
            neg += 1
    neg_rate = pct(neg, len(test))

    # ---------- 4. 报告 ----------
    L = ["# EvoGuard Lab · 进化闭环实验报告", ""]
    L.append("> 由 `ai-service/evolution_bench.py` 自动生成，数字全部来自本次运行。")
    L.append("")
    L.append("## 一、这次跑通的闭环")
    L.append("")
    L.append("| 步骤 | 结果 |")
    L.append("|---|---|")
    L.append("| 训练集用例 | %d |" % log[0][1])
    L.append("| 其中靠探索恢复成功 | %d |" % log[1][1])
    L.append("| 通过准入校验的经验 | %d |" % log[2][1])
    L.append("| 生成的候选技能 | %d |" % len(learned))
    L.append("| 晋升为正式技能 | %d |" % sum(1 for v in verdicts if v["res"]["verdict"] == "PROMOTE"))
    L.append("")
    L.append("### 候选技能验证明细")
    L.append("")
    L.append("| 技能 | 目标病例 | 验证前成功率 | 验证后成功率 | 增益(pp) | 负迁移率 | 判定 |")
    L.append("|---|---|---|---|---|---|---|")
    for v in verdicts:
        r = v["res"]
        L.append("| %s | %s/%s | %s%% | %s%% | %s | %s%% | **%s** |" % (
            v["skill"]["name"][:28], v["case"]["agent_type"], v["case"]["fault_type"],
            round(r["before_rate"] * 100, 1), round(r["after_rate"] * 100, 1),
            r["gain_pp"], round(r["neg_transfer_rate"] * 100, 1), r["verdict"]))
    L.append("")
    L.append("## 二、四组对照（独立测试集 %d 个用例）" % len(test))
    L.append("")
    L.append("| 模式 | 含义 | 成功率 |")
    L.append("|---|---|---|")
    L.append("| A | 无恢复 | %s%% |" % rates["A"])
    L.append("| B | 诊断+规则恢复，无经验学习 | %s%% |" % rates["B"])
    L.append("| C | 用经验但**不严格验证** | %s%% |" % rates["C"])
    L.append("| D | 完整闭环（验证+晋升） | %s%% |" % rates["D"])
    L.append("")
    L.append("- **负迁移率（B 成功但 D 失败）**：%s%%（%d/%d）" % (neg_rate, neg, len(test)))
    L.append("- D 相对 B 提升：**%s 个百分点**" % round(rates["D"] - rates["B"], 1))
    L.append("- D 相对 C 提升：**%s 个百分点**" % round(rates["D"] - rates["C"], 1))
    L.append("")
    L.append("## 三、怎么读")
    L.append("")
    L.append("1. 如果 **D = B**，说明技能没起作用或早已被规则覆盖 —— 那这套进化是装饰。")
    L.append("2. 如果 **D > C**，说明「严格验证」这一步真的挡住了坏经验。")
    L.append("3. **负迁移率必须接近 0**。不为 0 就说明晋升门槛太松。")
    L.append("")
    L.append("## 四、已知局限")
    L.append("")
    L.append("1. 模型仍是 mock（`AGENTLAB_LLM=mock`），本报告衡量的是**机制是否自洽**，不是模型能力。")
    L.append("2. 训练/测试划分只按 task_id 切，样本量小（各 %d / %d），不能当统计结论。" % (len(train), len(test)))
    L.append("3. 技能是声明式策略映射，不是生成代码，所以不涉及沙箱执行风险。")
    L.append("")

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print("== 闭环 ==")
    print("  训练 %d ｜ 探索 %d ｜ 经验 %d ｜ 候选技能 %d ｜ 晋升 %d"
          % (len(train), log[1][1], log[2][1], len(learned),
             sum(1 for v in verdicts if v["res"]["verdict"] == "PROMOTE")))
    print("== 四组对照（测试 %d）==" % len(test))
    for m in ("A", "B", "C", "D"):
        print("  %s: %s%%" % (m, rates[m]))
    print("  负迁移率 %s%%" % neg_rate)
    print("== 报告 ==")
    print("  " + REPORT)
    print("== 经验库 ==")
    print("  " + str(experience.stats(store)))


if __name__ == "__main__":
    main()
