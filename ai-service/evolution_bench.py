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
METRICS = os.path.join(ROOT, "experiments", "metrics.json")


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def run_mode(store, case, mode, skill=None):
    """跑一个用例的某个模式，返回 (success, run_id)。

    fault_chain 不进 experiments 表（表结构保持兼容），只在内存里挂到 exp 上，
    由 runtime._normalize_faults 消费 —— 复合故障是评测层的概念，不是业务表字段。
    """
    exp = store.create_experiment(
        name="%s/%s/%s/%s" % (case["agent_type"], case["task_id"], case["fault_type"], mode),
        agent_type=case["agent_type"], task_id=case["task_id"],
        fault_type=case["fault_type"], fault_target=0,
        seed=case.get("seed", 7), recovery_mode=mode)
    if case.get("fault_chain"):
        exp["fault_chain"] = [dict(f) for f in case["fault_chain"]]
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
    # 这是「跑完就重建」的实验库，开 fast：不 fsync + 复用连接。
    # 生产/服务进程用 core.Store(path) 默认配置，行为不变。
    store = core.Store(DB, fast=True)
    experience.ensure_schema(store)
    skills.ensure_schema(store)

    def progress(msg):
        print("  [..] %s" % msg, flush=True)

    log = []

    # ---------- 1. 学：从训练集的一次故障里提炼经验 ----------
    train = []
    for at in sim.AGENT_TYPES:
        for t in sim.dev_tasks(at):
            for ft in sim.CASE_FAULTS[at]:
                train.append({"agent_type": at, "task_id": t["task_id"],
                              "fault_type": ft, "seed": 7})
    # 复合故障也进训练：不进去就永远学不出组合链，策略链只能是摆设
    comp_train = sim.composite_cases("dev")
    train += comp_train

    # 探索候选：先单策略，再组合链。
    # 顺序有意义 —— 单故障一步就够，不该硬套两步链；
    # 只有单策略全失败（复合故障）才升级成组合，这样「组合」是学出来的，不是硬编码的。
    EXPLORE_SINGLE = ("cross_verify", "fallback_tool", "replay_checkpoint", "reformat")
    EXPLORE_CHAIN = (
        ["fallback_tool", "cross_verify"],
        ["fallback_tool", "reformat"],
        ["fallback_tool", "replay_checkpoint"],
        ["retry", "cross_verify"],
        ["retry", "reformat"],
        ["cross_verify", "reformat"],
    )
    explored = 0
    explored_chain = 0
    learned = []
    for c in train:
        ok, rid = run_mode(store, c, "R")   # R = 纯规则诊断，不受技能库污染
        if not ok:
            for cand in EXPLORE_SINGLE:
                fake = {"task_type": c["agent_type"], "strategy": cand, "id": "explore"}
                ok2, rid2 = run_mode(store, c, "D", skill=fake)
                if ok2:
                    ok, rid, explored = True, rid2, explored + 1
                    break
        if not ok:
            for cand in EXPLORE_CHAIN:
                fake = {"task_type": c["agent_type"], "strategy": cand, "id": "explore"}
                ok2, rid2 = run_mode(store, c, "D", skill=fake)
                if ok2:
                    ok, rid, explored = True, rid2, explored + 1
                    explored_chain += 1
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
    log.append(("其中复合故障用例", len(comp_train)))
    log.append(("探索恢复成功", explored))
    log.append(("其中靠策略链救回", explored_chain))
    log.append(("成功提炼的经验", len(learned)))
    progress("训练完成：%d 用例，提炼 %d 条经验（其中组合链 %d）"
             % (len(train), len(learned), explored_chain))

    # ---------- 2. 验：每个候选技能在独立验证集上测 ----------
    verdicts = []
    for item in learned:
        c = item["case"]
        sk = item["skill"]
        # target：同 agent 同故障，但换任务（独立）。
        # 复合经验必须用复合用例做 target，否则验证集里根本没有两个坏点，
        # 组合技能会被当成「和单策略一样」而误判增益。
        if c.get("fault_chain"):
            target = [dict(cc) for cc in sim.composite_cases("test")
                      if cc["agent_type"] == c["agent_type"]
                      and cc["fault_type"] == c["fault_type"]][:3]
        else:
            target = [{"agent_type": c["agent_type"], "task_id": t["task_id"],
                       "fault_type": c["fault_type"], "seed": 7}
                      for t in sim.test_tasks(c["agent_type"])][:3]
        # guard：同 agent、**不同故障** —— 不该被这个技能影响
        # guard 只取前 4 个：测负迁移不需要全量，全量会让评测跑成几分钟
        guard = [{"agent_type": c["agent_type"], "task_id": t["task_id"],
                  "fault_type": ft, "seed": 7}
                 for t in sim.test_tasks(c["agent_type"])
                 for ft in sim.CASE_FAULTS[c["agent_type"]] if ft != c["fault_type"]][:4]

        def runner(case, skill):
            ok, _ = run_mode(store, case, "D", skill=skill)
            return ok

        res = skills.evaluate(store, sk, target, guard, runner)
        verdicts.append({"skill": sk, "case": c, "res": res})
        if res["verdict"] == "PROMOTE":
            skills.promote(store, sk["id"])
    n_promote = sum(1 for v in verdicts if v["res"]["verdict"] == "PROMOTE")
    progress("验证完成：%d 个候选技能，晋升 %d" % (len(verdicts), n_promote))

    # ---------- 3. 测：测试集上四组对照 ----------
    test = []
    for at in sim.AGENT_TYPES:
        for t in sim.test_tasks(at):
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
    progress("四组对照完成：A %s%% / B %s%% / C %s%% / D %s%%"
             % (rates["A"], rates["B"], rates["C"], rates["D"]))

    # ---------- 3b. 复合故障：单策略天花板 vs 组合策略 ----------
    # 这一节是「策略链不是装饰」的直接证据。
    # 对照组 SINGLE = 允许它挑**最好的一个**单策略（对单策略最有利的设定）；
    # 实验组 CHAIN = 一条两步链。若 SINGLE 能追平 CHAIN，就说明链没价值。
    comp_test = sim.composite_cases("test")
    SINGLE_POOL = ("retry", "cross_verify", "fallback_tool",
                   "reformat", "replay_checkpoint")
    BEST_CHAIN = ["fallback_tool", "cross_verify"]
    cs = {"A": 0, "single": 0, "chain": 0, "D": 0}
    for c in comp_test:
        if run_mode(store, c, "A")[0]:
            cs["A"] += 1
        if any(run_mode(store, c, "D", skill={"id": "probe",
                                              "task_type": c["agent_type"],
                                              "strategy": st})[0]
               for st in SINGLE_POOL):
            cs["single"] += 1
        if run_mode(store, c, "D", skill={"id": "probe",
                                          "task_type": c["agent_type"],
                                          "strategy": BEST_CHAIN})[0]:
            cs["chain"] += 1
        if run_mode(store, c, "D")[0]:
            cs["D"] += 1
    cn = len(comp_test)
    comp_rates = {k: pct(v, cn) for k, v in cs.items()}
    progress("复合故障完成：单策略 %s%% / 组合链 %s%% / 闭环D %s%%"
             % (comp_rates["single"], comp_rates["chain"], comp_rates["D"]))

    # ---------- 4. 报告 ----------
    L = ["# EvoGuard · 进化闭环实验报告", ""]
    L.append("> 由 `ai-service/evolution_bench.py` 自动生成，数字全部来自本次运行。")
    L.append("")
    L.append("## 一、这次跑通的闭环")
    L.append("")
    L.append("| 步骤 | 结果 |")
    L.append("|---|---|")
    for k, v in log:
        L.append("| %s | %s |" % (k, v))
    L.append("| 生成的候选技能 | %d |" % len(learned))
    L.append("| 晋升为正式技能 | %d |" % n_promote)
    L.append("")
    chain_skills = sum(1 for v in verdicts
                       if len(core.as_chain(v["skill"]["strategy"])) > 1)
    L.append("> 晋升的组合技能（链长 > 1）：**%d** 个。为 0 就说明策略链没被学到，" % chain_skills)
    L.append("> 不是「进化」，只是把单策略换了个名字。")
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
    L.append("## 三、复合故障：策略链到底有没有用（%d 个用例）" % cn)
    L.append("")
    L.append("单故障只需诊断一次、用一个策略，策略链和单策略在数字上没有区别，")
    L.append("所以必须换靶场：**同一次运行注入两个落在不同逻辑步的持久故障**。")
    L.append("对照组 SINGLE 允许从 5 个单策略里挑最好的一个（对单策略最有利的设定）。")
    L.append("")
    L.append("| 组 | 含义 | 成功 | 成功率 |")
    L.append("|---|---|---|---|")
    L.append("| A | 无恢复 | %d | %s%% |" % (cs["A"], comp_rates["A"]))
    L.append("| SINGLE | 最优单策略（5 选 1） | %d | %s%% |" % (cs["single"], comp_rates["single"]))
    L.append("| CHAIN | 两步策略链 %s | %d | %s%% |" % (
        " -> ".join(BEST_CHAIN), cs["chain"], comp_rates["chain"]))
    L.append("| D | 完整闭环（自动查已晋升技能） | %d | %s%% |" % (cs["D"], comp_rates["D"]))
    L.append("")
    L.append("- **组合相对单策略提升：%s 个百分点**（%s%% → %s%%）" % (
        round(comp_rates["chain"] - comp_rates["single"], 1),
        comp_rates["single"], comp_rates["chain"]))
    L.append("- 为什么单策略必然不够：诊断每轮只返回**一个**坏点；处理完重放，")
    L.append("  第二个坏点才暴露。链的第 2 轮会重新诊断、按新根因换策略。")
    L.append("- 修复动作受「本轮诊断根因」授权，策略不能顺手把沿途所有认识的故障都修了，")
    L.append("  否则单策略就能偷跑完两步，实验结论会是假的。")
    L.append("")
    L.append("## 四、怎么读")
    L.append("")
    L.append("1. 如果 **D = B**，说明技能没起作用或早已被规则覆盖 —— 那这套进化是装饰。")
    L.append("2. 如果 **D > C**，说明「严格验证」这一步真的挡住了坏经验。")
    L.append("3. **负迁移率必须接近 0**。不为 0 就说明晋升门槛太松。")
    L.append("4. 如果 **CHAIN = SINGLE**，说明策略链没有增量，不该写进论文/简历。")
    L.append("")
    L.append("## 五、已知局限（不粉饰）")
    L.append("")
    L.append("1. 模型仍是 mock（`AGENTLAB_LLM=mock`），本报告衡量的是**机制是否自洽**，不是模型能力。")
    L.append("2. 训练/测试划分只按 task_id 切，样本量小（各 %d / %d），不能当统计结论。" % (len(train), len(test)))
    L.append("3. 复合故障是**构造出来的**，不是真实事故里自然共现的错误；")
    L.append("   它证明的是「机制上限」，不是「真实分布下的收益」。")
    L.append("4. 流程不变量 `INV-PLAN-ORDER` 靠通用动作词判断首尾阶段，")
    L.append("   纯执行型流程（步骤里没有启动/收尾词）仍有检测盲区，见 `docs/taxonomy.md`。")
    L.append("5. 技能是声明式策略映射，不是生成代码，不涉及沙箱执行风险；")
    L.append("   但也意味着策略空间仍是**固定词表**，不是开放式规划。")
    L.append("")

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    # 机器可读指标：前端只读这个，不解析 Markdown，也不写死假数字。
    import json
    metrics = {
        "generated_at": core.now(),
        "dataset": sim.dataset_stats(),
        "closed_loop": {k: v for k, v in log},
        "candidates": len(learned),
        "promoted": n_promote,
        "chain_skills": chain_skills,
        "four_groups": {"n": len(test), "rates": rates,
                        "neg_transfer_rate": neg_rate},
        "composite": {"n": cn, "rates": comp_rates,
                      "counts": cs, "best_chain": BEST_CHAIN,
                      "single_pool": list(SINGLE_POOL)},
        "verdicts": [
            {"name": v["skill"]["name"], "agent": v["case"]["agent_type"],
             "fault": v["case"]["fault_type"],
             "chain": core.as_chain(v["skill"]["strategy"]),
             "gain_pp": v["res"]["gain_pp"],
             "neg_transfer_rate": v["res"]["neg_transfer_rate"],
             "verdict": v["res"]["verdict"]}
            for v in verdicts
        ],
        "skills": [
            {"id": sk["id"], "name": sk["name"], "task_type": sk["task_type"],
             "fault_type": sk["fault_type"],
             "chain": core.as_chain(sk["strategy"]),
             "status": sk["status"], "version": sk["version"]}
            for sk in skills.list_skills(store)
        ],
        "experiences": experience.stats(store),
    }
    with open(METRICS, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    print("== 闭环 ==")
    print("  训练 %d（复合 %d）｜ 探索 %d（靠链 %d）｜ 经验 %d ｜ 候选 %d ｜ 晋升 %d"
          % (len(train), len(comp_train), explored, explored_chain,
             len(learned), len(learned), n_promote))
    print("== 四组对照（单故障测试 %d）==" % len(test))
    for m in ("A", "B", "C", "D"):
        print("  %s: %s%%" % (m, rates[m]))
    print("  负迁移率 %s%%" % neg_rate)
    print("== 复合故障（%d）==" % cn)
    print("  A %s%% ｜ 最优单策略 %s%% ｜ 两步链 %s%% ｜ 闭环D %s%%"
          % (comp_rates["A"], comp_rates["single"],
             comp_rates["chain"], comp_rates["D"]))
    print("== 报告 ==")
    print("  " + REPORT)
    print("== 经验库 ==")
    print("  " + str(experience.stats(store)))
    store.close()


if __name__ == "__main__":
    main()
