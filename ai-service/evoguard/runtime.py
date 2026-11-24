# -*- coding: utf-8 -*-
"""Agent Runtime：执行、Checkpoint、诊断、恢复、审批。

三条设计红线（规格第五、六部分要求，这里用代码强制）：
  1. diagnose() 的入参只有 steps —— 物理上拿不到 faults 表，杜绝评测泄漏
  2. 只有「干净」的工具结果才进 checkpoint 缓存 —— 否则重放会把污染值再喂一遍
  3. 写操作（order.settle）在恢复期重新执行必须走审批
"""
import json
import os
import re
import time

import agents
import approval
import core
import execution
import gateway
import invariants
import security
import sim
from core import (ApiError, Store, CREATED, READY, RUNNING, FAILED,
                  RECOVERING, SUCCEEDED, PAUSED, CANCELLED, new_id, now)

# ARC-DEEP-01：常量/异常/小工具与诊断层共享**同一对象**。
# 此前这里有一份逐字重复的副本：ctx.py 递增 SIDE_EFFECT_COUNT、evaluate.py
# 读 runtime.SIDE_EFFECT_COUNT —— 两份 dict 导致 side_effects_total 永远是 0；
# FORCED_SKILL/STORE_BY_MODE 同样各改各的。ToolError 两份还会让 except 漏接。
from runtime_common import (
    MAX_STEPS, MAX_RETRY, MAX_CHAIN, TOKEN_BUDGET, WRITE_TOOLS, AUTO_APPROVE,
    SIDE_EFFECT_COUNT, TRANSIENT_FAULTS, FORCED_SKILL, STORE_BY_MODE,
    REPAIR_STRATEGIES, REPAIR_COVERAGE, _has_module, _fault_specs,
    _last_attempt, SYSTEM_PROMPT, ToolError, Interrupted)

# ARC-DEEP-01-T2：诊断块已拆出，re-export 保持 import 兼容
from legacy_diagnose import (diagnose, _diagnose_impl, _infer_agent_type,
    diagnose_by_invariants, _recompute, _overlap, _record_skill_id,
    INVARIANT_TO_CAUSE, STRATEGY_BY_CAUSE)

def plan_recovery(store, agent_type, root_cause, mode, scope="single",
                  forced_skill=None, run_id=None):
    """产出一条**策略链** list[str]，不再是一个孤立字符串。

    为什么必须是链：一次运行里可以同时存在多个故障点，
    单策略只能处理诊断到的第一个；处理完重放后第二个才暴露。
    「查表」永远给出一步，「规划」才能给出有顺序的多步。

    scope：这是第几轮恢复（single = 第一轮，multi = 第二轮起）。
    技能按作用域优先命中 —— 复合学到的两步链不能占住单故障的键位，
    否则单故障用例会被强行套上多出来的一步（实测打挂 6 个用例）。

    B 模式：固定规则（无诊断，一律重试），链长恒为 1。
    C / D 模式：先查技能库
      - D 只认 active 且 agent+故障都匹配（严格）
      - C 认 candidate/active 但只看 agent（不严格 -> 经验被过度泛化 -> 可测出负迁移）
    查不到才退回按根因的固定策略表。

    FORCED_SKILL 只给离线评测用（要在晋升前单独测某个候选技能），
    运行时不设置它就是 None，不影响正常流程。
    STORE_BY_MODE 同理：让 C / D 各查自己的库，才测得出「验证严不严」的差别。
    """
    def pack(chain, params, reason, source):
        chain = [str(x) for x in core.as_chain(chain) if x][:MAX_CHAIN]
        if not chain:
            chain = ["human_takeover"]
        return {"strategy": chain[0], "strategy_chain": chain,
                "params": params, "reason": reason, "source": source}

    if mode == "B":
        return pack(["retry"], {"max_retry": MAX_RETRY, "chain": ["retry"]},
                    "固定规则：一律重试", "fixed_rule")

    # 实例状态优先；全局仅作向后兼容回退（旧脚本/测试仍在用）。
    forced = forced_skill if forced_skill is not None else FORCED_SKILL
    # 候选技能只在**它自己声称的故障类型**上生效。
    # 不限制的话，评测时它会被强行套到同一个 Agent 的所有故障上，
    # guard 用例全被污染 -> 负迁移虚高 -> 真正有效的技能也会被拒。
    if forced and forced.get("task_type") == agent_type and \
            (not forced.get("fault_type") or forced.get("fault_type") == root_cause):
        _record_skill_id(store, run_id, forced["id"])
        chain = core.as_chain(forced["strategy"])
        return pack(chain, {"max_retry": MAX_RETRY, "root_cause": root_cause,
                            "skill_id": forced["id"], "forced": True, "chain": chain},
                    "评测中强制使用候选技能 %s" % forced["id"], "skill_forced")

    try:
        import skills
        sk_store = STORE_BY_MODE.get(mode) or store
        # 环境维度：从本次轨迹推断当前生效的 env 域，作为技能适用条件之一。
        # 取不到就不传（None = 不过滤），保持老行为。
        _active = None
        if run_id:
            try:
                import skill_synthesis as _ss
                _invs = _ss.extract_env_invariants(store.list_steps(run_id))
                _doms = {str(x.get("domain")) for x in _invs
                         if isinstance(x, dict) and x.get("domain")}
                _active = _doms or None
            except Exception:                              # noqa: BLE001
                _active = None
        sk = skills.lookup(sk_store, agent_type, root_cause, mode,
                           scope=scope, active_domains=_active)
    except Exception:
        sk = None

    if sk:
        _record_skill_id(store, run_id, sk["id"])
        chain = core.as_chain(sk["strategy"])
        return pack(chain, {"max_retry": MAX_RETRY, "root_cause": root_cause,
                            "skill_id": sk["id"], "skill_version": sk["version"],
                            "chain": chain},
                    "命中技能 %s（v%s，%s）" % (sk["id"], sk["version"], sk["status"]),
                    "skill")

    strategy = STRATEGY_BY_CAUSE.get(root_cause, "human_takeover")
    return pack([strategy], {"max_retry": MAX_RETRY, "root_cause": root_cause,
                             "chain": [strategy]},
                "按根因 %s 选择" % root_cause, "rule")


# ---------------- 执行 ----------------
def _expected(exp):
    return sim.expected_answer(exp["agent_type"], exp["task_id"])


def _normalize_faults(exp, mode):
    """把 exp 里的故障描述规范化成 [{type,target}, ...]。

    为什么要显式支持复合故障：真实事故很少是「恰好一个东西坏了」。
    单故障用例保持原样（fault_type/fault_target），复合用例用 fault_chain，
    每个故障独立定位到自己的逻辑步 —— 这样「一次只诊断出一个坏点」的
    单策略才会真的不够用，策略链才有可测的增量。
    """
    if mode == "baseline":
        return [], "none", 0
    chain = exp.get("fault_chain")
    if chain:
        out = []
        for f in chain:
            t = f["type"]
            tg = int(f.get("target") or 0) or sim.auto_target(exp["agent_type"], t)
            out.append({"type": t, "target": tg})
        return out, out[0]["type"], out[0]["target"]
    ft = exp.get("fault_type")
    if ft and ft != "none":
        tg = int(exp.get("fault_target") or 0) or _auto_target(exp, int(exp.get("seed") or 0))
        return [{"type": ft, "target": tg}], ft, tg
    return [], "none", 0


def run_once(store, exp, mode):
    """跑一次。mode: baseline | A | B | C | D | R"""
    seed = int(exp.get("seed") or 0)
    run = store.create_run(exp["id"], mode, seed)
    rid = run["id"]
    store.set_state(rid, READY)

    specs, fault_type, fault_target = _normalize_faults(exp, mode)
    # 每个故障都单独记一条真值（评测模块可读；诊断模块拿不到）
    for spec in specs:
        store.add_fault(rid, spec["type"], spec["target"],
                        {"agent_type": exp["agent_type"]}, injected=0)

    run_exp = dict(exp, fault_type=fault_type, fault_target=fault_target,
                   fault_chain=specs)
    store.set_state(rid, RUNNING)

    ctx = Ctx(store, rid, run_exp, forced_skill=run_exp.get("forced_skill"))
    ctx.task = {"task_id": exp["task_id"], "goal": exp["task_id"]}

    # ---- 安全层：进入 Agent 之前的输入校验 ----
    # 默认 audit 只记账不改行为；enforce 下命中高危直接判这次运行被拦。
    in_sec = security.guard_input(ctx.task.get("goal"), "user_input")
    if in_sec["findings"]:
        security.record(store, rid, "user_input", in_sec)
    if in_sec["action"] == "blocked":
        store.set_state(rid, FAILED)
        store.finish_run(rid, 0, None, _expected(exp), 0, 0,
                         "security_blocked:user_input")
        security.record_effect(
            store, rid, "user_input", in_sec,
            override={"decision": security.DECISION_DENY,
                      "attempted": True, "executed": True,
                      "effect": security.EFFECT_CALL_STOPPED,
                      "reason": "enforce：高危输入，运行被终止，Agent 未启动"})
        return store.get_run(rid)
    if in_sec["detected"]:
        security.record_effect(
            store, rid, "user_input", in_sec,
            override={"attempted": False, "executed": False,
                      "effect": security.EFFECT_REPORTED_ONLY,
                      "reason": "audit 模式只记账：输入原样交给 Agent"})

    t0 = time.time()
    died = None
    try:
        result = agents.AGENTS[exp["agent_type"]](ctx)
    except ToolError as e:
        result = {"success": False, "answer": None, "expect": _expected(exp),
                  "reason": "%s: %s" % (e.error_type, e.message)}
        died = e
    except Interrupted as e:
        result = {"success": False, "answer": None, "expect": _expected(exp),
                  "reason": "interrupted"}
        died = e
    except ApiError:
        raise

    correct = agents.is_correct(exp["agent_type"], result.get("answer"),
                                result.get("expect"))
    status = SUCCEEDED if correct else FAILED
    store.set_state(rid, status)
    reason = result.get("reason") or ""
    if ctx.degraded:
        # 不覆盖原有失败原因，追加即可 —— 两者可能同时成立
        mark = "degraded:" + ",".join(sorted(set(ctx.degraded)))
        reason = (reason + " | " if reason else "") + mark
    store.finish_run(rid, correct, result.get("answer"), result.get("expect"),
                     ctx.tokens, int((time.time() - t0) * 1000), reason)

    if status == FAILED and mode in ("B", "C", "D", "R"):
        _recover(store, exp, run_exp, rid, ctx, mode)

    return store.get_run(rid)


def _auto_target(exp, seed):
    """没指定故障位置时，按「故障类型 + agent 类型」决定性地选一步。"""
    if int(exp.get("fault_target") or 0) > 0:
        return int(exp["fault_target"])
    return sim.auto_target(exp["agent_type"], exp.get("fault_type") or "none")


def _recover(store, exp, run_exp, rid, ctx, mode):
    """诊断 -> 生成**策略链** -> 逐轮执行（每轮重新诊断）-> 逐步审计。

    与旧版的三点本质差别：
      1. 一轮只用一个策略，用完重新诊断，而不是拿一个策略盲试两次；
      2. 每轮的抑制点按**本轮诊断结果**追加，复合故障才会被逐个拔掉；
      3. 修复式策略在覆盖范围内**不抑制故障点**，让故障照常发生再修好，
         这样「策略」就不只是一个用来决定 suppress 与否的标签。
    """
    steps = store.list_steps(rid)
    # 这是第几轮恢复？之前已经诊断过 = 前面至少修过一次，说明坏点不止一个。
    # 只用「此刻已知的信息」判断作用域，不去偷看注入的故障链。
    prev_rounds = len(store.list_diagnoses(rid))
    scope = "multi" if prev_rounds >= 1 else "single"
    # C 和 D 都要先诊断；只有 B 是「不诊断、盲目重试」
    if mode in ("C", "D", "R"):
        d = diagnose(steps)
        did = store.add_diagnosis(rid, d["first_bad_logical"], d["root_cause"],
                                  d["evidence"], d["confidence"])
    else:
        d = {"first_bad_step": steps[-1]["seq"] if steps else 0,
             "first_bad_logical": steps[-1].get("logical", 0) if steps else 0,
             "root_cause": "unknown", "confidence": 0.0, "evidence": {}}
        did = None

    plan = plan_recovery(store, exp["agent_type"], d["root_cause"], mode,
                         scope=scope, run_id=rid,
                         forced_skill=exp.get("forced_skill"))
    chain = plan["strategy_chain"]
    if chain[0] == "human_takeover":
        store.add_recovery(rid, did, "human_takeover", plan["params"],
                           "pending", 0, "无法自动恢复，转人工")
        if store.get_run(rid)["status"] == FAILED:
            store.set_state(rid, CANCELLED)
        return store.get_run(rid)

    store.set_state(rid, RECOVERING)
    suppress = set()
    cur_d, cur_did = d, did

    for idx, strategy in enumerate(chain):
        # 第 2 轮起重新诊断：只看上一次重放的轨迹（_last_attempt 切掉历史轮次），
        # 否则前面轮次的重复 tool 步会被误判成 resource_abuse。
        if idx > 0:
            cur_d = diagnose(_last_attempt(store.list_steps(rid)))
            cur_did = store.add_diagnosis(
                rid, cur_d["first_bad_logical"], cur_d["root_cause"],
                cur_d["evidence"], cur_d["confidence"])

        # 修复式策略：根因落在它的覆盖范围内就不绕开，交给 _repair 真修。
        # 其余策略沿用「抑制故障点后重放」；retry 永远不抑制（B 的语义）。
        repairable = (strategy in REPAIR_STRATEGIES and
                      cur_d["root_cause"] in REPAIR_COVERAGE.get(strategy, set()))
        if strategy != "retry" and not repairable:
            suppress.add(cur_d["first_bad_logical"])

        params = dict(plan["params"])
        params.update({"chain_index": idx, "chain_len": len(chain),
                       "chain": chain, "repairable": repairable,
                       "round_root_cause": cur_d["root_cause"]})

        ckpt = store.latest_checkpoint(rid)
        cache = (ckpt or {}).get("state", {}).get("cache", {}) if ckpt else {}
        # 多轮恢复时每一轮都必须**追加**到轨迹末尾，不能从 checkpoint 的 seq
        # 重新排号 —— 否则两轮的绝对序号互相覆盖，_last_attempt 切分失效，
        # 诊断会把上一轮的重复 tool 步误判成 resource_abuse。
        all_steps = store.list_steps(rid)
        start_seq = (max((s["seq"] for s in all_steps), default=0)) + 1
        rctx = None
        try:
            rctx = Ctx(store, rid, run_exp, start_seq=start_seq,
                       forced_skill=run_exp.get("forced_skill"),
                       cache=cache, suppress=suppress,
                       recovering=True, attempt=idx + 2, strategy=strategy,
                       round_root_cause=cur_d["root_cause"])
            rctx.task = {"task_id": exp["task_id"], "goal": exp["task_id"]}
            result = agents.AGENTS[exp["agent_type"]](rctx)
        except (ToolError, Interrupted) as e:
            params["repairs"] = rctx.repairs if rctx is not None else []
            store.add_recovery(rid, cur_did, strategy, params,
                               "auto" if AUTO_APPROVE else "pending", 0,
                               "链第 %d/%d 步 %s 失败：%s" % (idx + 1, len(chain), strategy, e))
            continue
        except ApiError as e:
            store.add_recovery(rid, cur_did, strategy, params, "denied", 0, str(e.message))
            store.set_state(rid, FAILED)
            return store.get_run(rid)

        params["repairs"] = rctx.repairs
        correct = agents.is_correct(exp["agent_type"], result.get("answer"),
                                     result.get("expect"))
        store.add_recovery(rid, cur_did, strategy, params,
                           "auto" if AUTO_APPROVE else "pending",
                           1 if correct else 0,
                           "链第 %d/%d 步 %s%s%s" % (
                               idx + 1, len(chain), strategy,
                               "（修复式）" if repairable else "（抑制重放）",
                               "成功" if correct else "结果仍不对"))
        if correct:
            store.set_state(rid, SUCCEEDED)
            store.finish_run(rid, True, result.get("answer"), result.get("expect"),
                             rctx.tokens, 0,
                             "策略链 %s 恢复完成" % " -> ".join(chain))
            return store.get_run(rid)

    if store.get_run(rid)["status"] == RECOVERING:
        store.set_state(rid, FAILED)
    return store.get_run(rid)


# HARD-04-T1：Ctx 拆到 ctx.py。原来在模块末尾直接 `from ctx import Ctx`，
# 只要有人先 `import ctx` 就会触发循环（ctx 顶部 from runtime import 常量，
# 而 runtime 又回头 import ctx）—— 实测直接 import ctx 会 ImportError。
# PEP 562 延迟导入：`from runtime import Ctx` 仍然可用，但两个模块都能独立导入。
# HARD-04-T1：Ctx 拆到 ctx.py。ctx 从 runtime_common 取共享对象，
# 不再回头 import runtime，因此这里可以安全地模块级导入（测试也能 monkeypatch）。
from ctx import Ctx  # noqa: E402,F401
