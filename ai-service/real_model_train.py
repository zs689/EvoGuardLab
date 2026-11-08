# -*- coding: utf-8 -*-
"""真实模型自举闭环：让 Qwen3 自己学一遍，并且**训两套库**。

为什么要两套：C 和 D 的差别本来只是「要不要独立验证」。
如果两边共用同一套已经严格验证过的技能库，这个变量根本没被激活 ——
上一轮评测里 C = D = 100% 就是这么来的，跟治理没关系。

所以这里用同一批探索轨迹，分两个口子入库：
  - 严格库（给 D）：只有在独立验证集上 gain >= 门槛、且负迁移不超标才晋升
  - 放宽库（给 C）：只要这次恢复救回来了就入库，不测目标集、不测负迁移

数据划分（test 一律不碰，评估才用它）：
  dev task #1  → 探索（train）
  dev task #2  → 验证 target / guard（val）

跑法（在 EvoGuardLab 目录下，先起 Ollama）：
    python ai-service/real_model_train.py                # 全量
    python ai-service/real_model_train.py --limit 6      # 冒烟

产物默认写入 experiments/validation_runs/ 的新目录：
    train.db / library_d.db / library_c.db / train_report.md / validation_results.json
    --output-dir 可指定一个尚不存在的目录，不覆盖历史库。
"""
import argparse
import json
import os
import shutil
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "evoguard"))

import core        # noqa: E402
import evaluate    # noqa: E402
import experience  # noqa: E402
import gateway     # noqa: E402
import runtime     # noqa: E402
import sim         # noqa: E402
import skills      # noqa: E402

ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "experiments", "real_model")
TRAIN_DB = os.path.join(ROOT, "data", "real_model_train.db")
LIB_D = os.path.join(ROOT, "data", "library_d.db")
LIB_C = os.path.join(ROOT, "data", "library_c.db")
REPORT = os.path.join(OUT, "train_report.md")

# 探索候选：先单策略，失败再升级成组合链。
# 单故障一步就够，不该硬套两步链；只有单策略全失败（复合故障）才升级。
EXPLORE_SINGLE = ("cross_verify", "fallback_tool", "replay_checkpoint", "reformat",
                  "retry")
EXPLORE_CHAIN = (
    ["fallback_tool", "cross_verify"],
    ["fallback_tool", "reformat"],
    ["fallback_tool", "replay_checkpoint"],
    ["retry", "cross_verify"],
    ["retry", "reformat"],
    ["cross_verify", "reformat"],
)


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def run_case(store, case, mode, skill=None):
    """跑一个用例，返回 (success, run_id)。与 evolution_bench.run_mode 同语义。"""
    exp = store.create_experiment(
        name="train/%s/%s/%s" % (case["agent_type"], case["task_id"],
                                 case.get("fault_type") or "none"),
        agent_type=case["agent_type"], task_id=case["task_id"],
        fault_type=case.get("fault_type") or "none", fault_target=0,
        seed=case.get("seed", 7), recovery_mode=mode)
    if case.get("fault_chain"):
        exp["fault_chain"] = [dict(f) for f in case["fault_chain"]]
    runtime.FORCED_SKILL = skill
    try:
        run = runtime.run_once(store, exp, mode)
    finally:
        runtime.FORCED_SKILL = None
    return run["success"] == 1, run["id"]


def split_cases():
    """dev task #1 探索，dev task #2 验证。test 完全不碰。

    复合故障必须**按模式（fault_type）分组后再按任务切**：
    直接 `comp[:half]` 是按列表位置切，而列表是按模式排的 ——
    切完以后 order 的模式全在训练侧、验证侧一条都没有，
    于是 order 的复合技能「无 target 可验」直接被拒（表现为链学不出来）。
    这个坑踩过：第一版切完，order 的两条链全部 NO_TARGET。
    """
    train, val = [], []
    for at in sim.AGENT_TYPES:
        t = sim.dev_tasks(at)
        t_train, t_val = t[0], t[1] if len(t) > 1 else t[0]
        for ft in sim.CASE_FAULTS[at]:
            train.append({"agent_type": at, "task_id": t_train["task_id"],
                          "fault_type": ft, "seed": 7})
            val.append({"agent_type": at, "task_id": t_val["task_id"],
                        "fault_type": ft, "seed": 7})
    # 难度组的两个 agent 也要学技能，否则评测时 C/D 在它们身上都查不到技能、
    # 双双退化成规则表（那时 C = D 打平，四组对照少一半信息）。
    # 用的是 hard_train_tasks（T4xxx / KB-3x），**不是评测题**，不会泄漏。
    for at in sim.HARD_AGENTS:
        tt = sim.hard_train_tasks(at)
        if len(tt) < 2:
            continue
        for ft in sim.HARD_CASE_FAULTS[at]:
            train.append({"agent_type": at, "task_id": tt[0]["task_id"],
                          "fault_type": ft, "seed": 7})
            val.append({"agent_type": at, "task_id": tt[1]["task_id"],
                        "fault_type": ft, "seed": 7})
    by_pat = {}
    for c in sim.composite_cases("dev"):
        by_pat.setdefault(c["fault_type"], []).append(c)
    for pat, lst in by_pat.items():
        for i, c in enumerate(lst):
            (train if i % 2 == 0 else val).append(dict(c))
    return train, val


def build_library(src, dst, entries, note, strict=False):
    """把训练库里选中的技能导出成独立库。

    strict=True（D 严格库）：entries 是 (skill_id, evaluation_id)。必须先在源库
    调用 skills.promote 走完晋升门禁再导出；以前这里直接 `UPDATE ... SET
    status='active', version=2` 伪造「验证通过」，等于把 P0 03 门禁整个绕过。

    strict=False（C 放宽库）：这是**故意不验证**的对照组，仍以 active/v2 写入，
    代表「探索成功就入库」。它是实验变量，不是晋升链路的证据，报告里必须写清。

    version=2 是刻意的：模拟「晋升过一次」，让 C 的 `ORDER BY version DESC`
    一定出现并列 —— 上一轮就是在这里踩到「按扫描顺序取行」的坑。
    """
    if strict:
        # promote 会自己开连接写源库，必须在下面的 src.conn() 事务之外完成。
        for sid, eid in entries:
            skills.promote(src, sid, evaluation_id=eid)
        ids = [sid for sid, _ in entries]
    else:
        ids = list(entries)
    if os.path.exists(dst):
        os.remove(dst)
    store = core.Store(dst, fast=True)
    skills.ensure_schema(store)
    n = 0
    with src.conn() as c:
        for sid in ids:
            r = c.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
            if not r:
                continue
            row = dict(r)
            if strict:
                status, version = row["status"], row["version"]
                active_version = row.get("active_version") or version
            else:
                # 对照组不碰源库证据，只在目标库里模拟一次「未经验证的启用」。
                status, version, active_version = "active", 2, 2
            with store.conn() as c2:
                c2.execute("INSERT OR REPLACE INTO skills(id,name,task_type,fault_type,"
                           "strategy,required_tools,safety,status,version,created_at,scope,"
                           "active_version,promoted_from_evaluation,promoted_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (row["id"], row["name"], row["task_type"], row["fault_type"],
                            row["strategy"], row["required_tools"], row["safety"],
                            status, version, row["created_at"],
                            row.get("scope") or "single", active_version,
                            row.get("promoted_from_evaluation") if strict else None,
                            row.get("promoted_at") if strict else None))
            n += 1
    store.close()
    return n


def main():
    global OUT, TRAIN_DB, LIB_D, LIB_C, REPORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=os.environ.get("AGENTLAB_LLM", "ollama"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--guard-n", type=int, default=2, help="每个候选技能测几个 guard 用例")
    ap.add_argument("--output-dir", help="新的隔离输出目录；已存在时拒绝覆盖")
    args = ap.parse_args()
    if args.guard_n < 0 or args.limit < 0:
        ap.error("--guard-n and --limit must be nonnegative")
    OUT = os.path.abspath(args.output_dir or os.path.join(
        ROOT, "experiments", "validation_runs", "train_%s_%s" % (
            time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:8])))
    os.makedirs(OUT, exist_ok=False)
    TRAIN_DB = os.path.join(OUT, "train.db")
    LIB_D, LIB_C = os.path.join(OUT, "library_d.db"), os.path.join(OUT, "library_c.db")
    REPORT = os.path.join(OUT, "train_report.md")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(os.path.dirname(TRAIN_DB), exist_ok=True)

    os.environ["AGENTLAB_LLM"] = args.backend
    os.environ.setdefault("AGENTLAB_LLM_MODEL", "qwen3:4b")
    gateway.reset_cache()
    provider = gateway.get_provider(args.backend)
    try:
        models = provider.list_models()
    except AttributeError:
        models = ["mock"]          # mock 后端没有 list_models，冒烟测试要能跑
    except Exception as e:
        print("[×] 模型服务不可用：%s" % e)
        print("    先启动：cd /f/ollama && OLLAMA_MODELS='F:\\Qwen3' ./ollama.exe serve")
        return 1
    print("[√] 服务可达，模型 %s ｜ 后端 %s" % (models, provider.name))

    store = core.Store(TRAIN_DB, fast=True)
    experience.ensure_schema(store)
    skills.ensure_schema(store)

    train, val = split_cases()
    if args.limit:
        train, val = train[:args.limit], val[:args.limit]
    print("[√] 探索 %d 例 ｜ 验证 %d 例 ｜ test 一律不参与" % (len(train), len(val)))

    t0 = time.time()
    stats = {"train": len(train), "val": len(val), "ran": 0,
             "rule_ok": 0, "explored_ok": 0, "chain_used": 0,
             "exp_saved": 0, "exp_rejected": 0, "proposed": 0}

    # ---------- 1. 探索：规则救不回来的，才去找策略（单策略 → 组合链） ----------
    # 探索用的强制技能**故意不带 fault_type**。
    # runtime 的修复授权要求 `forced.fault_type == root_cause`，
    # 而复合故障的 fault_type 是 "missing_field+silent_corruption"、
    # 运行时诊断出来的 root_cause 只是 "missing_field" —— 带上它永远命中不了，
    # 所有候选都会被静默丢弃，最后表现为「一个链都没学出来」。
    # 这个坑真实踩过：第一次训练 33 个候选里链数是 0。
    proposed = []
    for i, c in enumerate(train, 1):
        ok, rid = run_case(store, c, "R")
        stats["ran"] += 1
        if ok:
            stats["rule_ok"] += 1
        else:
            for cand in EXPLORE_SINGLE:
                ok2, rid2 = run_case(store, c, "D", skill={
                    "id": "explore", "task_type": c["agent_type"], "strategy": cand})
                stats["ran"] += 1
                if ok2:
                    ok, rid, stats["explored_ok"] = True, rid2, stats["explored_ok"] + 1
                    break
            if not ok:
                for cand in EXPLORE_CHAIN:
                    ok2, rid2 = run_case(store, c, "D", skill={
                        "id": "explore", "task_type": c["agent_type"], "strategy": cand})
                    stats["ran"] += 1
                    if ok2:
                        ok, rid, stats["explored_ok"] = True, rid2, stats["explored_ok"] + 1
                        stats["chain_used"] += 1
                        break
        if not ok:
            continue
        exp = experience.extract(store, rid)
        if not exp:
            stats["exp_rejected"] += 1
            continue
        exp, reasons = experience.save(store, exp)
        if exp["validation_status"] != experience.VALIDATED:
            stats["exp_rejected"] += 1
            continue
        stats["exp_saved"] += 1
        sk = skills.save(store, skills.propose(exp))
        proposed.append({"skill": sk, "case": c})
        stats["proposed"] += 1
        if i % 5 == 0 or i == len(train):
            print("  [探索 %d/%d] %.0fs 规则救回 %d ｜ 探索救回 %d（链 %d）｜ 候选 %d"
                  % (i, len(train), time.time() - t0, stats["rule_ok"],
                     stats["explored_ok"], stats["chain_used"], stats["proposed"]),
                  flush=True)

    # ---------- 2. 严格验证（给 D 用）：target 增益 + guard 负迁移 ----------
    promotions, verdicts = [], []   # (skill_id, evaluation_id) —— 晋升必须绑定评估
    for j, item in enumerate(proposed, 1):
        sk, c = item["skill"], item["case"]
        # 技能的真实匹配面：运行时只按 root_cause 匹配，而复合故障的 root_cause
        # 就是它链路里的第一个故障类型 —— 所以 skills.propose() 存下来的键
        # 是「首个故障」，不是 "A+B"。验证集必须按同一个口径选，
        # 否则会出现「技能明明能用，验证时却一条 target 都选不到」。
        sk_fault = sk["fault_type"]

        def _in(case):
            if case.get("fault_chain"):
                return sk_fault in [f["type"] for f in case["fault_chain"]]
            return case.get("fault_type") == sk_fault

        # target 必须与「经验来源」同一类靶场，不能混。
        # 混在一起会稀释增益：单故障 val 用例本来就被规则表救回（before=100%），
        # 复合用例只有 1 条 —— 5 条 single + 1 条 composite 平均下来，
        # 一个真正有效的两步链增益只剩 100/6≈16pp，甚至被算成 0 而误杀。
        # 这个坑踩过一次：第一版训练把 5 条复合链全拒了，D 反而比迁移库更差。
        from_composite = bool(c.get("fault_chain"))
        target = [v for v in val
                  if v["agent_type"] == c["agent_type"] and _in(v)
                  and bool(v.get("fault_chain")) == from_composite]
        guard = [v for v in val if v["agent_type"] == c["agent_type"] and not _in(v)][
            :args.guard_n]
        def runner(case, skill):
            ok, rid = run_case(store, case, "D", skill=skill)
            run = store.get_run(rid)
            return {"success": ok, "run_id": rid,
                    "cost": {"reported_tokens": run["tokens"], "latency_ms": run["latency_ms"]}}

        res = skills.evaluate(store, sk, target, guard, runner, training_cases=train)
        stats["ran"] += len(target) * 2 + len(guard) * 2
        verdicts.append({"skill": sk, "res": res, "case": c})
        if res["verdict"] == "PROMOTE":
            promotions.append((sk["id"], res["id"]))
        print("  [验证 %d/%d] %s/%s %s gain=%spp neg=%s → %s"
              % (j, len(proposed), c["agent_type"], c.get("fault_type"),
                 core.as_chain(sk["strategy"]), res["gain_pp"],
                 ("%s%%" % round(res["neg_transfer_rate"] * 100, 1)
                  if res["neg_transfer_rate"] is not None else "N/A"), res["verdict"]), flush=True)

    # ---------- 3. 导出两套库 ----------
    all_ids = [p["skill"]["id"] for p in proposed]
    n_d = build_library(store, LIB_D, promotions, "严格：验证通过才入库", strict=True)
    n_c = build_library(store, LIB_C, all_ids, "放宽：探索成功就入库，不验证", strict=False)
    print("[门禁] D 库 %d 条均通过 promote 校验；C 库 %d 条为故意放宽的对照组"
          % (n_d, n_c))
    print("[√] 严格库 %d 条 → %s" % (n_d, LIB_D))
    print("[√] 放宽库 %d 条 → %s" % (n_c, LIB_C))

    elapsed = time.time() - t0
    write_report(stats, proposed, verdicts, n_d, n_c, elapsed, provider)
    with open(os.path.join(OUT, "validation_results.json"), "w", encoding="utf-8") as f:
        json.dump({"protocol_version": skills.VALIDATION_PROTOCOL, "backend": provider.name,
                   "stats": stats, "evaluations": [v["res"] for v in verdicts]},
                  f, ensure_ascii=False, indent=2)
    store.close()
    print("== 训练完成 == %.0fs ｜ run %d ｜ 候选 %d ｜ 严格晋升 %d ｜ 放宽入库 %d"
          % (elapsed, stats["ran"], stats["proposed"], n_d, n_c))
    print("   报告 %s" % REPORT)
    return 0


def write_report(stats, proposed, verdicts, n_d, n_c, elapsed, provider):
    chain_n = sum(1 for p in proposed
                  if len(core.as_chain(p["skill"]["strategy"])) > 1)
    vd = {}
    for v in verdicts:
        k = v["res"]["verdict"]
        vd[k] = vd.get(k, 0) + 1

    L = ["# 真实模型自举闭环：训练报告", "",
         "> 由 `ai-service/real_model_train.py` 自动生成。",
         "> 验证协议 `%s`；N/A 表示不可估计，样本不足转 NEEDS_REVIEW。" % skills.VALIDATION_PROTOCOL,
         "> 模型 `%s` ｜ 后端 `%s` ｜ 用时 %.0fs ｜ run %d 次"
         % (os.environ.get("AGENTLAB_LLM_MODEL", "?"), provider.name, elapsed, stats["ran"]), ""]
    L += ["## 一、划分（test 一律不参与）", "",
          "| 用途 | 来源 | 用例数 |", "|---|---|---|",
          "| 探索（train） | dev task #1（每 agent）／复合 dev 每个模式各 1 条 | %d |" % stats["train"],
          "| 验证（val） | dev task #2（每 agent）／复合 dev 每个模式另 1 条 | %d |" % stats["val"],
          "| 评测 | test（6 task/agent + 30 复合） | 234 |", ""]
    L += ["## 二、探索", "",
          "| 项 | 值 |", "|---|---|",
          "| 规则表直接救回（不需要新策略） | %d |" % stats["rule_ok"],
          "| 需要探索才救回 | %d |" % stats["explored_ok"],
          "| 其中靠组合链救回 | %d |" % stats["chain_used"],
          "| 提炼并入库的经验 | %d |" % stats["exp_saved"],
          "| 经验准入被拒 | %d |" % stats["exp_rejected"],
          "| 生成的候选技能 | %d |" % stats["proposed"],
          "| 其中组合技能（链长>1） | %d |" % chain_n, ""]
    L += ["## 三、两套库的差别（这就是被激活的变量）", "",
          "| 库 | 入库条件 | 条数 | 给谁用 |", "|---|---|---|---|",
          "| 严格库 | 独立验证集 gain 达标 且 负迁移不超标 | %d | D |" % n_d,
          "| 放宽库 | 本次恢复成功即入库，**不验证** | %d | C |" % n_c, "",
          "判定分布：" + json.dumps(vd, ensure_ascii=False), ""]
    L += ["## 四、逐条验证结果", "",
          "| 技能 | Agent | 故障 | 策略 | 增益(pp) | 负迁移 | 判定 | 原因 |",
          "|---|---|---|---|---|---|---|---|"]
    for v in verdicts:
        r, c, sk = v["res"], v["case"], v["skill"]
        L.append("| %s | %s | %s | %s | %s | %s | **%s** | %s |" % (
            core.as_chain(sk["strategy"]), c["agent_type"], c.get("fault_type"),
            "→".join(core.as_chain(sk["strategy"])), r.get("gain_pp"),
            ("%s%%" % round(r["neg_transfer_rate"] * 100, 1)
             if r["neg_transfer_rate"] is not None else "N/A"), r["verdict"],
            "; ".join(r.get("reasons", []))))
    L += ["", "## 五、诚实说明", "",
          "1. 本轮后端为 `%s`；mock 是机制冒烟，不能作为真实模型结论。" % provider.name,
          "2. 但任务本身简单：模型在答案步几乎不犯错，所以探索失败基本都来自故障机制，",
          "   不是模型推理错。技能学到的是「遇到这类故障该走哪几步」。",
          "3. dev 每 agent 只有 2 个 task，train/val 各 1 个，样本极小，不能当统计结论。",
          "4. 放宽库不验证就入库 —— 这是**故意**的，用来量出「不验证」的代价。", ""]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    sys.exit(main())
