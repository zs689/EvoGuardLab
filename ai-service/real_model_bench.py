# -*- coding: utf-8 -*-
"""真实模型全量评测：204 单故障 × 4 模式 + 30 复合 × 4 模式 = 936 个 run。

跑法（在 EvoGuardLab 目录下）：
    set AGENTLAB_LLM=ollama
    set AGENTLAB_LLM_MODEL=qwen3:4b
    python ai-service/real_model_bench.py

设计要点：
  1. **断点续跑**：每完成一个 run 立刻追加一行 JSONL，重跑时跳过已完成的 key。
     几百个 run 的任务中断一次不该从头再来。
  2. **不覆盖 mock 结果**：输出全在 experiments/real_model/ 下，
     mock 的 evolution_report.md / metrics.json 一个字节都不动。
  3. **版本可追溯**：模型名、后端参数、prompt 指纹、故障集版本、代码版本都写进 meta。
  4. **失败不吞**：模型调用错误按 code 分类统计，写进报告，不把 LLMError 当"答案错"糊过去。
  5. **独立 DB**：用 data/real_model.db，避免和 mock 闭环的经验/技能库串味。
     D 模式需要的技能从 mock 库复制过来 —— 这是「技能迁移」实验，报告里会说明。

可选参数：
    --backend ollama | ollama_raw | local | mock
    --limit N            只跑前 N 个（调试用）
    --suite single|composite|all
    --fresh              清空检查点重跑
    --no-grammar-note    不跑无约束对照（默认会跑一个小样本）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "evoguard"))

import core        # noqa: E402
import gateway     # noqa: E402
import runtime     # noqa: E402
import sim         # noqa: E402
import skills      # noqa: E402
import experience  # noqa: E402

ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "experiments", "real_model")
CHECKPOINT = os.path.join(OUT, "runs.jsonl")
REPORT = os.path.join(OUT, "report.md")
METRICS = os.path.join(OUT, "metrics.json")
DB = os.path.join(ROOT, "data", "real_model.db")
MOCK_DB = os.path.join(ROOT, "data", "evoguard.db")
LIB_D = os.path.join(ROOT, "data", "library_d.db")
LIB_C = os.path.join(ROOT, "data", "library_c.db")
MODES = ("A", "B", "C", "D")


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


# ---------------- 裁决：这一条 run 该不该进成功率分母 ----------------
# 为什么单独做这件事：把「模型答错」和「模型服务根本没给出答案」混在一个成功率里，
# 会让基础设施抖动看起来像 Agent 质量下降。实测踩过：Ollama 挂掉那一次，
# grammar 对照 6 次调用全是 E_LLM_UNREACHABLE，却被写成「0/6 严格 JSON」。
# 治理体系里对这个的标准做法是「证据不足 → 标不可评，出分母」，
# 而不是判失败（淘宝 CNT 那套的 CTX 门控层就是这个意思）。
PASSED, FAILED, UNJUDGEABLE = "passed", "failed", "unjudgeable"


def judge_run(r):
    """给 run 定性：passed / failed / unjudgeable。

    **只有「框架异常」算不可评**（`error` 非空 = run 根本没跑完，无法评判）。

    刻意**不**把 E_LLM_* 算进来。这一版改过两次，原因是实测数据打脸：
    有 15 条失败伴随模型报错，但逐条看轨迹后发现它们**都有答案**、
    且失败原因在注入的故障上（比如 `incorrect_planning` 让工具返回倒序计划，
    规则表又没有对应策略 → human_takeover；模型那步报没报错，结果都一样）。
    把这类剔出分母，等于用「服务抖了一下」洗白真实失败 —— 这是最典型的
    指标自欺，比不修口径坏得多。

    模型报错的数量单独统计（`failures_with_llm_error`）供审计，但**不动分母**。
    `bad_model_output`（模型吐出非 JSON）同样计 failed，那是模型质量问题。
    """
    if r.get("success"):
        return PASSED
    if r.get("error"):
        return UNJUDGEABLE
    return FAILED


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def build_cases(suite):
    out = []
    if suite in ("single", "all"):
        for at in sim.AGENT_TYPES:
            for t in sim.test_tasks(at):
                for ft in sim.CASE_FAULTS[at]:
                    out.append({"suite": "single", "agent_type": at,
                                "task_id": t["task_id"], "fault_type": ft,
                                "seed": 7})
    if suite in ("composite", "all"):
        for c in sim.composite_cases("test"):
            out.append(dict(c, suite="composite"))
    if suite in ("hard", "all"):
        # 难度组：多步计算 + 跨文档冲突。工具只给原料，答案要模型自己算/自己比。
        # 单独成组而不是改现有 204 例 —— 改了就没法和历史数字比。
        for c in sim.hard_cases():
            out.append(dict(c, suite="hard"))
    return out


def case_key(c, mode):
    return "%s|%s|%s|%s|%s" % (c["suite"], c["agent_type"], c["task_id"],
                                c["fault_type"], mode)


def load_checkpoint(fresh):
    if fresh and os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)
    done = {}
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    done[r["key"]] = r
                except Exception:
                    continue      # 半行（写到一半被杀）直接忽略，重跑这个 key
    return done


def prepare_db(db_path=None):
    """准备评测用的技能库，两种设置，报告里必须写清楚用的是哪一套。

    设置 A（双库，`real_model_train.py` 跑过就用这个）：
        C 查 data/library_c.db（放宽入库，没验证过的经验全在里面）
        D 查 data/library_d.db（独立验证通过才入库）
        —— 只有这样，「要不要严格验证」这个变量才真的被激活。
        上一轮 C/D 共用一套 mock 迁移来的严格库，所以 C = D = 100% 什么也说明不了。

    设置 B（单库回退，没跑过训练时用）：
        从 mock 库复制 active 技能到一个库，C/D 共用。
        这时 C 与 D 的差别只剩「匹配条件宽不宽」，不是「验证严不严」。
    """
    db_path = db_path or DB
    if os.path.exists(db_path):
        os.remove(db_path)
    store = core.Store(db_path, fast=True)
    experience.ensure_schema(store)
    skills.ensure_schema(store)

    if os.path.exists(LIB_D) and os.path.exists(LIB_C):
        d_store = core.Store(LIB_D)
        c_store = core.Store(LIB_C)
        n_d = count_skills(d_store)
        n_c = count_skills(c_store)
        runtime.STORE_BY_MODE = {"C": c_store, "D": d_store}
        return store, {"kind": "two_libs", "d": n_d, "c": n_c,
                       "d_path": os.path.basename(LIB_D),
                       "c_path": os.path.basename(LIB_C)}

    copied = 0
    if os.path.exists(MOCK_DB):
        src = core.Store(MOCK_DB)
        skills.ensure_schema(src)
        with src.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM skills WHERE status='active'").fetchall()]
        with store.conn() as c:
            for r in rows:
                # 正文要整份复制：以前只带 10 列，连 scope 都丢了，
                # 导出库里的技能根本没法按版本还原行为。
                c.execute("INSERT OR REPLACE INTO skills(id,name,task_type,fault_type,"
                          "strategy,required_tools,safety,status,version,created_at,"
                          "scope,active_version,decision_params) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (r["id"], r["name"], r["task_type"], r["fault_type"],
                           r["strategy"], r["required_tools"], r["safety"],
                           r["status"], r["version"], r["created_at"],
                           r.get("scope") or "single", r.get("active_version"),
                           r.get("decision_params")))
                copied += 1
    return store, {"kind": "shared_mock_migration", "shared": copied}


def count_skills(store):
    with store.conn() as c:
        return c.execute("SELECT COUNT(*) FROM skills WHERE status='active'").fetchone()[0]


def run_one(store, c, mode):
    exp = store.create_experiment(
        name="real/%s/%s/%s/%s" % (c["suite"], c["agent_type"],
                                   c["task_id"], c["fault_type"]),
        agent_type=c["agent_type"], task_id=c["task_id"],
        fault_type=c["fault_type"], fault_target=0, seed=c.get("seed", 7),
        recovery_mode=mode)
    if c.get("fault_chain"):
        exp["fault_chain"] = [dict(f) for f in c["fault_chain"]]
    runtime.FORCED_SKILL = None
    t0 = time.time()
    err = None
    try:
        run = runtime.run_once(store, exp, mode)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, str(e)[:200])
        return {"success": False, "status": "ERROR", "error": err,
                "latency_ms": int((time.time() - t0) * 1000)}
    rid = run["id"]
    steps = store.list_steps(rid)
    model_steps = [s for s in steps if s.get("kind") == "model" and s.get("name") == "answer"]
    llm_errors = [s.get("error_type") for s in model_steps
                  if (s.get("error_type") or "").startswith("E_LLM_")]
    bad_output = [s for s in model_steps
                  if s.get("error_type") == "bad_model_output"]
    diagnoses = store.list_diagnoses(rid)
    recoveries = store.list_recoveries(rid)
    return {
        "success": bool(run["success"] == 1),
        "status": run["status"],
        "error": err,
        "latency_ms": int((time.time() - t0) * 1000),
        "tokens": run.get("tokens") or 0,
        "n_steps": len(steps),
        "n_model_calls": len(model_steps),
        "llm_errors": llm_errors,
        "bad_model_output": len(bad_output),
        "root_causes": [d["root_cause"] for d in diagnoses],
        "recovery_chain": [r["strategy"] for r in recoveries],
        "answer": core.loads(run.get("answer"), None) if isinstance(run.get("answer"), str) else run.get("answer"),
        "expect": core.loads(run.get("expect"), None) if isinstance(run.get("expect"), str) else run.get("expect"),
    }


def meta_info(args, provider):
    sys_prompt = runtime.SYSTEM_PROMPT
    code_files = ["runtime.py", "gateway.py", "sim.py", "agents.py",
                  "invariants.py", "experience.py", "skills.py"]
    h = hashlib.sha256()
    for fn in code_files:
        p = os.path.join(HERE, "evoguard", fn)
        if os.path.exists(p):
            with open(p, "rb") as f:
                h.update(f.read())
    return {
        "model": getattr(provider, "model", None) or "auto",
        "backend": args.backend,
        "provider_name": provider.name,
        "json_grammar": bool(getattr(provider, "json_mode", False)),
        "think_disabled": isinstance(provider, gateway.OllamaNativeProvider),
        "base": getattr(provider, "base", None),
        "system_prompt": sys_prompt,
        "system_prompt_sha256": hashlib.sha256(sys_prompt.encode()).hexdigest()[:16],
        "code_sha256": h.hexdigest()[:16],
        "ollama_version": "0.40.15",
        "dataset": sim.dataset_stats(),
        "modes": list(MODES),
        "started_at": core.now(),
    }


def grammar_control(args):
    """无 grammar 对照：测模型自然格式违规率。

    这一组不能省。grammar 约束会把「模型自然输出散文」这个错误源抹掉，
    不测它就没法说明 format_violation 类故障在真实模型下到底多常见。
    """
    sys.path.insert(0, os.path.join(HERE, "evoguard"))
    os.environ["AGENTLAB_LLM"] = "ollama_raw"
    gateway.reset_cache()
    p = gateway.get_provider("ollama_raw")
    SYS = runtime.SYSTEM_PROMPT
    prompts = [
        "上下文：订单 A1001，计价工具返回总额 504.0。请输出 JSON：{\"order_id\": \"订单号\", \"total\": 数字}",
        "上下文证据：自签收之日起 7 个工作日内可无理由退货。问题：退货要几天内 请输出 JSON：{\"text\": 结论原文, \"cited\": [文档号列表]}",
        "上下文步骤：收集本周销售数据 / 核对异常订单 / 生成周报草稿 / 提交审核 请输出 JSON：{\"steps\": [\"步骤1\", ...]}",
        "上下文：订单 A1002，计价工具返回总额 899.0。请输出 JSON：{\"order_id\": \"订单号\", \"total\": 数字}",
        "上下文证据：退货审核通过后 3 个工作日内原路退回。问题：退款多久到账 请输出 JSON：{\"text\": 结论原文, \"cited\": [文档号列表]}",
        "上下文步骤：冻结代码 / 跑回归测试 / 生成变更说明 / 灰度发布 请输出 JSON：{\"steps\": [\"步骤1\", ...]}",
    ]
    out = {"n": 0, "strict_ok": 0, "errors": {}, "latency_ms_total": 0, "samples": []}
    for u in prompts:
        t = time.time()
        try:
            text, tok, mid = p.complete(
                [{"role": "system", "content": SYS}, {"role": "user", "content": u}],
                temperature=0.0, max_tokens=1500)
            parsed = gateway.extract_json_strict(text)
            ok = parsed is not None
            head = text[:70]
        except gateway.LLMError as e:
            ok = False
            head = e.code
            out["errors"][e.code] = out["errors"].get(e.code, 0) + 1
        lat = int((time.time() - t) * 1000)
        out["n"] += 1
        out["strict_ok"] += 1 if ok else 0
        out["latency_ms_total"] += lat
        out["samples"].append({"strict": ok, "latency_ms": lat, "head": head})
    out["avg_latency_ms"] = round(out["latency_ms_total"] / max(1, out["n"]), 1)
    # 调用失败 ≠ 输出不严格。服务挂掉时「0/6 严格 JSON」是个假结论。
    # 如果**全部**调用都失败，这一批就根本没有数据，不能输出任何比率（输出 0% 会被当成结论）。
    call_err = sum(out["errors"].values())
    out["call_errors"] = call_err
    out["judgeable"] = out["n"] - call_err
    out["valid"] = out["judgeable"] > 0
    out["strict_rate"] = pct(out["strict_ok"], out["judgeable"]) if out["valid"] else None
    out["strict_rate_all"] = pct(out["strict_ok"], out["n"])
    out["error_rate"] = pct(call_err, out["n"])
    return out


def aggregate(done, cases, suite):
    res = {}
    for m in MODES:
        rows = [done[case_key(c, m)] for c in cases if case_key(c, m) in done]
        n = len(rows)
        ok = sum(1 for r in rows if r["success"])
        verdicts = [judge_run(r) for r in rows]
        unj = sum(1 for v in verdicts if v == UNJUDGEABLE)
        fail = sum(1 for v in verdicts if v == FAILED)
        # 审计用：失败里有多少条伴随模型报错。**不影响分母** ——
        # 只要 run 跑完了、有答案，就得算失败（见 judge_run 的注释）。
        with_llm = sum(1 for r in rows
                       if judge_run(r) == FAILED and r.get("llm_errors"))
        res[m] = {
            "n": n,
            "ok": ok,
            "failed": fail,
            "unjudgeable": unj,
            "failures_with_llm_error": with_llm,
            # 主口径：只在「可评」的样本上算成功率。
            # rate_raw 是老口径（ok/n），保留下来做对比，让读者自己看到差异有多大。
            "rate": pct(ok, ok + fail),
            "rate_raw": pct(ok, n),
            "judgeable_rate": pct(ok + fail, n),
        }
    # 错误分类
    all_rows = [done[case_key(c, m)] for c in cases for m in MODES
                if case_key(c, m) in done]
    errs, llm, bad, lat, tok, calls = {}, {}, 0, [], [], 0
    for r in all_rows:
        if r.get("error"):
            errs[r["error"][:60]] = errs.get(r["error"][:60], 0) + 1
        for e in r.get("llm_errors") or []:
            llm[e] = llm.get(e, 0) + 1
        bad += r.get("bad_model_output") or 0
        lat.append(r.get("latency_ms") or 0)
        tok.append(r.get("tokens") or 0)
        calls += r.get("n_model_calls") or 0
    # 按故障类型统计 D 的失败（不隐藏）；同样用可评口径，否则某一类故障的
    # 服务抖动会被算成「这类故障治不好」。
    by_fault = {}
    for c in cases:
        k = case_key(c, "D")
        if k not in done:
            continue
        f = c["fault_type"]
        b = by_fault.setdefault(f, {"n": 0, "ok": 0, "failed": 0, "unjudgeable": 0})
        r = done[k]
        b["n"] += 1
        b["ok"] += 1 if r["success"] else 0
        v = judge_run(r)
        if v == UNJUDGEABLE:
            b["unjudgeable"] += 1
        elif v == FAILED:
            b["failed"] += 1
    fails = [{"agent": c["agent_type"], "task": c["task_id"], "fault": c["fault_type"],
              "verdict": judge_run(done[case_key(c, "D")]),
              "root_causes": done[case_key(c, "D")].get("root_causes"),
              "chain": done[case_key(c, "D")].get("recovery_chain"),
              "answer": str(done[case_key(c, "D")].get("answer"))[:90]}
             for c in cases if case_key(c, "D") in done
             and not done[case_key(c, "D")]["success"]]
    n_unj = sum(1 for r in all_rows if judge_run(r) == UNJUDGEABLE)
    return {"suite": suite, "cases": len(cases), "modes": res,
            "judgeable_rate": pct(len(all_rows) - n_unj, len(all_rows)),
            "unjudgeable": n_unj,
            "errors": errs, "llm_errors": llm,
            "bad_model_output": bad, "model_calls": calls,
            "avg_latency_ms": round(sum(lat) / max(1, len(lat)), 1),
            "total_latency_s": round(sum(lat) / 1000, 1),
            "avg_tokens": round(sum(tok) / max(1, len(tok)), 1),
            "by_fault": by_fault, "D_failures": fails}


def write_hard_report(meta, hard, done, elapsed):
    """难度组单独出报告。

    为什么不复用主报告的排版：主报告是「单故障 / 复合 / 难度」三段的固定结构，
    只跑难度组时前两段全是 0 用例，会写成「0.0%」，读者会当成坏结果。
    """
    base = [r for k, r in done.items() if k.split("|")[3] == "none"]
    base_ok = sum(1 for r in base if r["success"])
    L = ["# 难度组评测报告（工具只给原料）", ""]
    L.append("> 由 `ai-service/real_model_bench.py --suite hard` 生成。")
    L.append("> 模型 `%s` ｜ 后端 `%s` ｜ 代码指纹 `%s`" % (
        meta["model"], meta["backend"], meta["code_sha256"]))
    L.append("")
    L.append("## 一、这一组为什么存在")
    L.append("")
    L.append("原来的 204 例是**抽取式**任务：工具把答案算好了（`pricing.calc_total` 直接返回总额），")
    L.append("模型只要搬运，所以 936 次调用 0 次答错 —— 那测的是恢复机制，不是模型。")
    L.append("这一组把工具从「给答案」改成「给原料」：")
    L.append("")
    L.append("- `calc`：只返回小计、满减规则、会员折扣率，**不返回总额**。")
    L.append("  模型必须自己判断是否达门槛，并按「先满减、再乘折扣」的顺序算 —— 顺序反了结果不同。")
    L.append("- `xref`：同一主题返回**三份不同版本**的文档，真值只认版本号最高的那份。")
    L.append("  不比较版本就会答成旧条款。")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 用例数 | %d |" % hard.get("cases", 0))
    L.append("| 完成 run 数 | %d |" % len(done))
    L.append("| 总耗时 | %.1f 秒 |" % elapsed)
    L.append("| 模型调用 | %d |" % hard.get("model_calls", 0))
    L.append("")
    L.append("## 二、纯能力基线：没有任何故障时，模型能不能做对")
    L.append("")
    L.append("这是最关键的对照。基线就不是 100% 的话，说明题目确实难住了模型 ——")
    L.append("后面所有恢复机制的数字都要放在这个天花板下看。")
    L.append("")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append("| 无故障用例 | %d |" % len(base))
    L.append("| 做对 | **%d**（%s%%） |" % (base_ok, pct(base_ok, len(base))))
    L.append("")
    L.append("## 三、四组对照（%d 用例）" % hard.get("cases", 0))
    L.append("")
    L.append("| 模式 | 成功/可评 | 不可评 | 成功率 |")
    L.append("|---|---|---|---|")
    for m in MODES:
        r = hard["modes"][m]
        L.append("| %s | %d/%d | %d | **%s%%** |"
                 % (m, r["ok"], r["ok"] + r["failed"], r["unjudgeable"], r["rate"]))
    L.append("")
    L.append("> 这一组**没有 mock 基线**：mock 后端不会做算术、也不会比版本号，")
    L.append("> 跑出来必然是 0%，拿它当基线只会误导。")
    L.append("")
    by = hard.get("by_fault") or {}
    if by:
        L.append("## 四、按故障类型（D 模式）")
        L.append("")
        L.append("| 故障 | 成功/可评 | 成功率 |")
        L.append("|---|---|---|")
        for f, b in sorted(by.items()):
            judgeable = b["ok"] + b.get("failed", 0)
            L.append("| %s | %d/%d | %s%% |"
                     % (f, b["ok"], judgeable, pct(b["ok"], judgeable)))
        L.append("")
    L.append("## 五、读这组数字的规矩")
    L.append("")
    L.append("1. **先看基线**。基线是天，恢复机制只能把结果拉向基线，不可能越过它。")
    L.append("2. **C 与 D 在这一组打平不代表治理无效**：难度组的 agent（`calc` / `xref`）没有对应技能，")
    L.append("   两套库里都查不到，所以 C/D 实际都退化成规则表 —— 这是设计使然，要如实写。")
    L.append("3. **故障类型之间不可比**：`retrieval_failure` 有恢复策略，`silent_corruption` 在数值场景下")
    L.append("   只能靠独立验算，两者天花板本来就不同。")
    L.append("")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def write_outputs(meta, single, composite, control, done, elapsed, hard=None,
                  hard_only=False):
    n_total = sum(len([1 for k in done if k.endswith("|" + m)]) for m in MODES)
    metrics = {
        "meta": meta,
        "finished_at": core.now(),
        "elapsed_s": round(elapsed, 1),
        "runs_completed": len(done),
        "single": single,
        "composite": composite,
        "hard": hard,
        "grammar_control": control,
        "mock_baseline": {
            "source": "experiments/metrics.json（mock 闭环）",
            "four_groups": {"A": 0.0, "B": 26.5, "C": 100.0, "D": 100.0},
            "composite": {"single": 0.0, "chain": 100.0, "D": 100.0},
        },
    }
    with open(METRICS, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    if hard_only:
        write_hard_report(meta, hard or {}, done, elapsed)
        return metrics

    L = ["# 真实模型全量评测报告", ""]
    L.append("> 由 `ai-service/real_model_bench.py` 自动生成，数字全部来自本次运行。")
    L.append("> 模型 `%s` ｜ 后端 `%s` ｜ grammar=%s ｜ 代码指纹 `%s`" % (
        meta["model"], meta["backend"], meta["json_grammar"], meta["code_sha256"]))
    L.append("")
    L.append("## 一、实验配置（可复现）")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 模型 | %s |" % meta["model"])
    L.append("| 服务 | %s（Ollama %s） |" % (meta["base"], meta["ollama_version"]))
    L.append("| 后端 | %s / provider=%s |" % (meta["backend"], meta["provider_name"]))
    L.append("| 关闭思考 | %s |" % meta["think_disabled"])
    L.append("| JSON grammar 约束 | %s |" % meta["json_grammar"])
    L.append("| System prompt 指纹 | `%s` |" % meta["system_prompt_sha256"])
    L.append("| 代码指纹 | `%s` |" % meta["code_sha256"])
    sk = meta.get("skillset") or {}
    if sk.get("kind") == "two_libs":
        L.append("| 技能库 | **双库**：D 严格库 %d 条 ｜ C 放宽库 %d 条 |"
                 % (sk.get("d"), sk.get("c")))
    else:
        L.append("| 技能库 | 单库（mock 迁移 %d 条，C/D 共用） |" % sk.get("shared", 0))
    if single["cases"]:
        L.append("| 单故障用例 | %d |" % single["cases"])
    if composite["cases"]:
        L.append("| 复合故障用例 | %d |" % composite["cases"])
    if hard:
        L.append("| 难度组用例 | %d |" % hard["cases"])
    L.append("| 完成 run 数 | %d |" % len(done))
    L.append("| 总耗时 | %.1f 秒 |" % elapsed)
    _calls = single["model_calls"] + composite["model_calls"] + (hard["model_calls"] if hard else 0)
    L.append("| 模型调用次数 | %d |" % _calls)
    L.append("")
    has_llm_fail = sum(
        (x["modes"][m].get("failures_with_llm_error") or 0)
        for x in [single, composite] + ([hard] if hard else []) for m in MODES)
    L.append("## 二、四组对照（单故障，%d 用例）" % single["cases"])
    L.append("")
    L.append("| 模式 | 成功/可评 | 不可评 | 成功率 | 老口径(成功/总数) | mock 基线 | 差 |")
    L.append("|---|---|---|---|---|---|---|")
    mk = metrics["mock_baseline"]["four_groups"]
    for m in MODES:
        r = single["modes"][m]
        L.append("| %s | %d/%d | %d | **%s%%** | %s%% | %s%% | %+s pp |" % (
            m, r["ok"], r["ok"] + r["failed"], r["unjudgeable"], r["rate"],
            r["rate_raw"], mk[m], round(r["rate"] - mk[m], 1)))
    L.append("")
    L.append("> **口径**：只有**框架异常**（run 根本没跑完）才判不可评、剔出分母。")
    L.append("> 模型报错（`E_LLM_*`）**不剔** —— 只要 run 跑完、有答案，就计失败。")
    L.append("> 这一条是查出来的，不是拍出来的：本轮 15 条失败伴随模型报错，逐条看轨迹后发现它们全都有答案，")
    L.append("> 失败原因在注入的故障上（如 `incorrect_planning` 让工具返回倒序计划、规则表又无策略 → 转人工），")
    L.append("> 模型那步报不报错结果都一样。把这类剔出分母，等于用「服务抖了一下」洗白真实失败。")
    L.append("> 伴随模型报错的失败条数单独记在 `failures_with_llm_error`（本轮 D 为 %d 条），**不动分母**。"
             % single["modes"]["D"].get("failures_with_llm_error", 0))
    L.append("> `bad_model_output`（模型吐出非 JSON）一律计分 —— 那是模型质量问题，不许剔。")
    L.append("> 全量可评率：单故障 **%s%%**、复合 **%s%%**（本轮无框架异常）。" % (
        single.get("judgeable_rate"), composite.get("judgeable_rate")))
    L.append("")
    L.append("## 三、复合故障（%d 用例）" % composite["cases"])
    L.append("")
    L.append("| 模式 | 成功/可评 | 不可评 | 成功率 | 老口径 |")
    L.append("|---|---|---|---|---|")
    for m in MODES:
        r = composite["modes"][m]
        L.append("| %s | %d/%d | %d | **%s%%** | %s%% |" % (
            m, r["ok"], r["ok"] + r["failed"], r["unjudgeable"], r["rate"], r["rate_raw"]))
    L.append("")
    L.append("> mock 闭环在复合故障上只测过 **A / 最优单策略 / 两步链 / D** 四组")
    L.append("> （0% / 0% / 100% / 100%，见 `experiments/evolution_report.md`），")
    L.append("> **没有**测过复合的 A/B/C/D 四组对照，所以这里不填 mock 基线 —— ")
    L.append("> 填 0 会被误读成「mock 测出来是 0%」。")
    L.append("")
    if hard:
        L.append("## 三b、难度组（%d 用例）：工具只给原料，答案要模型自己算" % hard["cases"])
        L.append("")
        L.append("前两组是**抽取式**任务：工具把答案算好了，模型只要搬运，所以在它们上面")
        L.append("模型几乎不会答错 —— 那测的是恢复机制，不是模型能力。这一组把工具从")
        L.append("「给答案」改成「给原料」：")
        L.append("")
        L.append("- `calc`：只返回小计/满减规则/会员折扣率，**不返回总额**；模型必须按")
        L.append("  「先满减、再乘折扣」的顺序自己算（顺序反了结果不同）。")
        L.append("- `xref`：同一主题返回三份不同版本的文档，真值只认**版本号最高**的那份；")
        L.append("  不比较版本就会答成 7 天或 15 天。")
        L.append("")
        L.append("| 模式 | 成功/可评 | 不可评 | 成功率 |")
        L.append("|---|---|---|---|")
        for m in MODES:
            r = hard["modes"][m]
            L.append("| %s | %d/%d | %d | **%s%%** |"
                     % (m, r["ok"], r["ok"] + r["failed"], r["unjudgeable"], r["rate"]))
        L.append("")
        L.append("> 这一组**没有 mock 基线**：mock 后端不会做算术、也不会比版本号，")
        L.append("> 跑出来必然是 0%，拿它当基线只会误导。")
        L.append("")
        by = hard["by_fault"]
        if by:
            L.append("按故障类型（D）：")
            L.append("")
            L.append("| 故障 | 成功/可评 | 成功率 |")
            L.append("|---|---|---|")
            for f, b in sorted(by.items()):
                judgeable = b["ok"] + b.get("failed", 0)
                L.append("| %s | %d/%d | %s%% |"
                         % (f, b["ok"], judgeable, pct(b["ok"], judgeable)))
            L.append("")
    L.append("## 四、模型行为（这才是真实模型评测的重点）")
    L.append("")
    L.append("- 单故障：`bad_model_output` %d 次，LLM 错误 %s" % (
        single["bad_model_output"], single["llm_errors"] or "无"))
    L.append("- 复合故障：`bad_model_output` %d 次，LLM 错误 %s" % (
        composite["bad_model_output"], composite["llm_errors"] or "无"))
    L.append("- 平均延迟 %s ms ｜ 平均 token %s ｜ 总耗时 %s 秒" % (
        single["avg_latency_ms"], single["avg_tokens"], single["total_latency_s"]))
    L.append("")
    if control:
        L.append("### 无 grammar 约束对照（%d 次独立调用）" % control["n"])
        L.append("")
        if control.get("valid") is False:
            L.append("**本批次无效**：%d 次调用全部失败（%s）。"
                     "不能据此得出「严格 JSON 成功率 0%%」—— 那是服务没起来，不是模型不守格式。"
                     % (control["n"], control["errors"]))
        else:
            L.append("| 指标 | 值 |")
            L.append("|---|---|")
            L.append("| 严格 JSON 成功率（可评口径） | **%s%%**（%d/%d） |" % (
                control["strict_rate"], control["strict_ok"],
                control.get("judgeable", control["n"])))
            L.append("| 调用失败（不计入分母） | %d 次 %s |" % (
                control.get("call_errors", 0), control["errors"] or ""))
            L.append("| 老口径（成功/总调用） | %s%% |" % control.get("strict_rate_all",
                                                                    control["strict_rate"]))
            L.append("| 平均延迟 | %s ms |" % control["avg_latency_ms"])
        L.append("")
        L.append("> 这一组是必需的：`format=json` 的 grammar 约束**从模型侧消除了自然格式违规**，")
        L.append("> 所以主实验里 `format_violation` 只能靠注入产生，不能声称测到了模型真实犯错率。")
        L.append("")
    L.append("## 五、D 模式仍然失败的用例（不隐藏）")
    L.append("")
    fails = single["D_failures"] + composite["D_failures"]
    if fails:
        L.append("| Agent | 任务 | 故障 | 定性 | 诊断链 | 恢复链 | 答案 |")
        L.append("|---|---|---|---|---|---|---|")
        for f in fails[:40]:
            L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                f["agent"], f["task"], f["fault"], f.get("verdict", "-"),
                " → ".join(f["root_causes"] or []) or "-",
                " → ".join(f["chain"] or []) or "-",
                str(f["answer"]).replace("|", "/")[:60]))
        if len(fails) > 40:
            L.append("")
            L.append("（仅列前 40 条，共 %d 条，完整数据在 metrics.json）" % len(fails))
    else:
        L.append("无。")
    L.append("")
    L.append("## 六、按故障类型看 D 成功率（可评口径）")
    L.append("")
    L.append("| 故障 | 成功/可评 | 不可评 | 成功率 |")
    L.append("|---|---|---|---|")
    for f, b in sorted(single["by_fault"].items()):
        judgeable = b["ok"] + b.get("failed", 0)
        L.append("| %s | %d/%d | %d | %s%% |" % (
            f, b["ok"], judgeable, b.get("unjudgeable", 0), pct(b["ok"], judgeable)))
    L.append("")
    L.append("## 七、必须讲清楚的取舍与局限")
    L.append("")
    L.append("1. **速度换来的代价**：为把 936 个 run 从 5 小时压到可接受时间，")
    L.append("   用了原生 `/api/chat` + `think:false` + `format=json`。实测单次 14s → 0.5s。")
    L.append("   代价是 grammar 约束消除了模型自然产生的格式违规，第四节给了无约束对照数据。")
    L.append("2. **技能库来源**：%s" % (
        "D 用严格库（独立验证通过才入库）、C 用放宽库（探索成功就入库、不验证），"
        "两套库都由 `real_model_train.py` 用同一个真实模型训出来 —— "
        "这才是「验证严不严」这个变量的真正对照。"
        if (meta.get("skillset") or {}).get("kind") == "two_libs" else
        "C 与 D 共用一套从 mock 闭环迁移来的严格库。"
        "这时候两者的差别只剩「匹配条件宽不宽」，**不是**「验证严不严」，"
        "所以这轮不能用来证明治理价值；要测治理先跑 real_model_train.py。"))
    L.append("3. **故障仍是注入的**：不是真实事故里自然共现的错误。")
    L.append("4. **只有一次运行**：没有多次 seed 取均值，模型随机性未量化。")
    L.append("5. `format_violation` 类故障由代码注入，不能当作模型真实犯错率。")
    L.append("6. **「不可评」的边界是人为定的**：只认框架异常和 `E_LLM_*` 导致的空答案。"
             "这条规则可以被滥用成洗白（把失败说成服务问题就能剔出分母），"
             "所以报告同时给出 `unjudgeable` 绝对数与 `judgeable_rate`，"
             "不允许只看成功率；`bad_model_output` 一律计分。")
    L.append("")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=os.environ.get("AGENTLAB_LLM", "ollama"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--suite", default="all",
                    choices=["single", "composite", "hard", "all"])
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--skip-control", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.makedirs(OUT, exist_ok=True)

    # 难度组单独落盘、单独建库。
    # 为什么必须隔离：write_outputs 会把 metrics.json 整个重写，跑子集 suite 时
    # 单故障/复合两段会变成 n=0，把主结果写坏；而且 prepare_db 会清库，
    # 把之前 936 个 run 的明细（attribution 要读）一起清掉。
    hard_only = args.suite == "hard"
    if hard_only:
        global CHECKPOINT, REPORT, METRICS
        hdir = os.path.join(OUT, "hard")
        os.makedirs(hdir, exist_ok=True)
        CHECKPOINT = os.path.join(hdir, "runs.jsonl")
        REPORT = os.path.join(hdir, "report.md")
        METRICS = os.path.join(hdir, "metrics.json")
    db_path = os.path.join(ROOT, "data", "real_model_hard.db") if hard_only else DB

    os.environ["AGENTLAB_LLM"] = args.backend
    os.environ.setdefault("AGENTLAB_LLM_MODEL", "qwen3:4b")
    gateway.reset_cache()
    provider = gateway.get_provider(args.backend)

    try:
        models = provider.list_models()
    except AttributeError:
        models = ["mock"]          # mock 后端没有 list_models，冒烟要能跑
    except Exception as e:
        print("[×] 模型服务不可用：%s" % e, flush=True)
        print("    先启动：cd /f/ollama && OLLAMA_MODELS='F:\\Qwen3' ./ollama.exe serve", flush=True)
        return 1
    print("[√] 服务可达，模型：%s ｜ 后端 %s" % (models, provider.name), flush=True)

    meta = meta_info(args, provider)
    # 护栏：换了技能库设置就不能续跑。
    # 检查点按 key（suite|agent|task|fault|mode）去重，不含库信息 ——
    # 单库/双库两种设置会得到同一个 key，混在一张表里算出来的就是垃圾。
    want_kind = "two_libs" if (os.path.exists(LIB_D) and os.path.exists(LIB_C)) \
        else "shared_mock_migration"
    old = _read_json(METRICS, {})
    old_kind = ((old or {}).get("meta") or {}).get("skillset", {}).get("kind")
    if old_kind and old_kind != want_kind and not args.fresh:
        print("[×] 技能库设置变了（检查点里是 %s，现在是 %s），不能续跑。"
              % (old_kind, want_kind), flush=True)
        print("    先把旧结果归档，再加 --fresh 从零跑：", flush=True)
        print("    mv experiments/real_model/{runs.jsonl,metrics.json,report.md} "
              "experiments/real_model/archive_runN/", flush=True)
        return 2

    store, skillset = prepare_db(db_path)
    meta["skillset"] = skillset
    if skillset["kind"] == "two_libs":
        print("[√] 双库就绪：D 用严格库 %d 条（%s）｜ C 用放宽库 %d 条（%s）"
              % (skillset["d"], skillset["d_path"], skillset["c"], skillset["c_path"]),
              flush=True)
    else:
        print("[√] 单库就绪，从 mock 迁移已晋升技能 %d 条（C/D 共用，变量未激活）"
              % skillset.get("shared", 0), flush=True)

    cases = build_cases(args.suite)
    if args.limit:
        cases = cases[:args.limit]
    done = load_checkpoint(args.fresh)
    todo = [(c, m) for c in cases for m in MODES if case_key(c, m) not in done]
    print("[√] 用例 %d × 4 模式 = %d 个 run；已完成 %d，待跑 %d"
          % (len(cases), len(cases) * 4, len(done), len(todo)), flush=True)

    if todo:
        # 先热一次模型，把加载时间算在总耗时之外单独标注
        t_warm = time.time()
        try:
            provider.complete([{"role": "user", "content": "输出 {\"ok\":1}"}],
                              temperature=0.0, max_tokens=64)
            print("[√] 模型预热 %.2fs" % (time.time() - t_warm), flush=True)
        except Exception as e:
            print("[!] 预热失败（继续跑）：%s" % e, flush=True)

    t_start = time.time()
    fout = open(CHECKPOINT, "a", encoding="utf-8")
    try:
        for i, (c, m) in enumerate(todo, 1):
            try:
                r = run_one(store, c, m)
            except Exception as e:
                r = {"success": False, "status": "ERROR",
                     "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "latency_ms": 0}
                traceback.print_exc()
            rec = {"key": case_key(c, m), "suite": c["suite"],
                   "agent": c["agent_type"], "task": c["task_id"],
                   "fault": c["fault_type"], "mode": m, "ts": core.now()}
            rec.update(r)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()
            done[rec["key"]] = rec
            if i % 50 == 0 or i == len(todo):
                el = time.time() - t_start
                rate = i / el if el else 0
                left = (len(todo) - i) / rate if rate else 0
                ok = sum(1 for x in done.values() if x["success"])
                print("  [%d/%d] %.0fs 已完成 %d/%d run，成功 %d，预计剩余 %.0fs"
                      % (i, len(todo), el, len(done), len(cases) * 4,
                         ok, left), flush=True)
    finally:
        fout.close()
        store.close()

    single_cases = [c for c in cases if c["suite"] == "single"]
    comp_cases = [c for c in cases if c["suite"] == "composite"]
    hard_cases = [c for c in cases if c["suite"] == "hard"]
    single = aggregate(done, single_cases, "single")
    composite = aggregate(done, comp_cases, "composite")
    hard = aggregate(done, hard_cases, "hard") if hard_cases else None

    control = None
    if not args.skip_control:
        print("[..] 跑无 grammar 约束对照…", flush=True)
        control = grammar_control(args)

    elapsed = time.time() - t_start
    m = write_outputs(meta, single, composite, control, done, elapsed, hard,
                      hard_only=hard_only)
    print("== 真实模型全量评测完成 ==", flush=True)
    rows = [("单故障", single), ("复合故障", composite)]
    if hard:
        rows.append(("难度组", hard))
    for suite, ag in rows:
        print("  %s：" % suite + " / ".join(
            "%s %s%%" % (k, ag["modes"][k]["rate"]) for k in MODES), flush=True)
    print("  run 数 %d ｜ 耗时 %.1fs ｜ 模型调用 %d"
          % (len(done), elapsed,
             single["model_calls"] + composite["model_calls"]
             + (hard["model_calls"] if hard else 0)), flush=True)
    print("  报告 %s" % REPORT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
