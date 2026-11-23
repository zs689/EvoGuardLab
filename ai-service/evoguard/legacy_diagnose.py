# -*- coding: utf-8 -*-
"""ARC-DEEP-01-T2：从 runtime.py 拆出的规则诊断与恢复计划（re-export 兼容）。"""

import json
import re

import core
import invariants
import security
import sim
import skills
import trajectory_diagnose as _td

from runtime_common import (MAX_STEPS, MAX_RETRY, MAX_CHAIN, TRANSIENT_FAULTS,
    _last_attempt, SYSTEM_PROMPT, ToolError, Interrupted)

def _recompute(items, policy):
    subtotal = sum(float(i["price"]) * int(i["qty"]) for i in items)
    total = subtotal * (1 - float(policy["rate"])) if subtotal >= float(policy["threshold"]) else subtotal
    return round(subtotal, 2), round(total, 2)


def diagnose(steps):
    """诊断入口。只接收 steps，**不能**访问 faults 表。

    分层：第一层确定性规则 -> 第二层业务不变量 -> 未知（转人工）
    这里额外补一个 first_bad_logical：故障真值按「逻辑步号」记录，
    而轨迹里既有绝对 seq 也有 logical，报告两种都要对上才算定位正确。
    """
    d = _diagnose_impl(steps)

    if d["root_cause"] == "unknown":
        cause, ev = diagnose_by_invariants(steps, _infer_agent_type(steps))
        if cause:
            d = {"first_bad_step": ev.get("step_seq") or 0, "root_cause": cause,
                 "evidence": ev, "confidence": ev.get("confidence", 0.8)}

    bad = d.get("first_bad_step") or 0

    # 定位不到时，退化成「最后一个数据步骤」。
    # 为什么必须这么做：恢复期靠 first_bad_logical 决定抑制哪一步，
    # 如果是 0，就什么也抑制不掉，重放必然再犯同一个错 ——
    # 「未知故障永远救不回来」是个假象，其实是抑制点丢了。
    if not bad:
        tool_steps = [s for s in steps if s["kind"] in ("tool", "replay")]
        if tool_steps:
            bad = tool_steps[-1]["seq"]

    lg = bad
    for s in steps:
        if s["seq"] == bad:
            lg = s.get("logical") or bad
            break
    d["first_bad_step"] = bad
    d["first_bad_logical"] = lg
    return d


def _infer_agent_type(steps):
    """从轨迹里的工具名反推 agent 类型 —— 不变量需要知道适用哪套业务。"""
    names = {s.get("name") for s in steps if s.get("kind") in ("tool", "replay")}
    if "order.get_order" in names or "pricing.calc_total" in names:
        return "order"
    if "kb.search" in names:
        return "kb"
    if "task.make_plan" in names:
        return "planner"
    return "unknown"


def _diagnose_impl(steps):
    """只接收 steps 列表。**不能**访问 faults 表，否则评测就作弊了。

    返回 {first_bad_step, root_cause, evidence, confidence}
    """
    if not steps:
        return {"first_bad_step": 0, "root_cause": "unknown",
                "evidence": {"reason": "没有轨迹"}, "confidence": 0.0}

    # 规则 0a：工具契约错配（选错工具）—— 输出字段凑齐了**另一个**工具的契约
    for s in steps:
        if s["kind"] in ("tool", "replay") and isinstance(s["output"], dict):
            cur = sim.TOOLS.get(s["name"], {})
            req = set(cur.get("out_required") or [])
            keys = set(s["output"].keys())
            if req and not (req & keys):
                for other, spec in sim.TOOLS.items():
                    if other != s["name"] and set(spec.get("out_required") or []) <= keys:
                        return {"first_bad_step": s["seq"], "root_cause": "wrong_tool",
                                "evidence": {"step": s["seq"], "invoked": s["name"],
                                             "output_matches": other},
                                "confidence": 0.9}

    # 规则 0b：资源滥用 —— 同工具同参数被重复调用
    seen = {}
    for s in steps:
        if s["kind"] == "tool":
            key = (s["name"], core.dumps(s["input"] or {}))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= 2:
                return {"first_bad_step": s["seq"], "root_cause": "resource_abuse",
                        "evidence": {"step": s["seq"], "tool": s["name"],
                                     "times": seen[key]}, "confidence": 0.85}

    # 规则 0c：过早终止 —— 出现 early_exit 步但没走到答案步
    for s in steps:
        if s["kind"] == "stop" and s["name"] == "early_exit":
            return {"first_bad_step": s["seq"], "root_cause": "premature_termination",
                    "evidence": {"step": s["seq"]}, "confidence": 0.9}

    # 规则 1：显式错误
    for s in steps:
        if s["status"] == "error":
            et = s.get("error_type") or "exception"
            cause = {"timeout": "tool_timeout", "exception": "tool_exception",
                     "interrupted": "process_interruption",
                     "invalid_json": "invalid_json",
                     "bad_args": "unknown"}.get(et, "unknown")
            return {"first_bad_step": s["seq"], "root_cause": cause,
                    "evidence": {"step": s["seq"], "tool": s["name"],
                                 "error_type": et},
                    "confidence": 0.95}

    # 规则 2：结构不合法（工具返回的不是对象）
    for s in steps:
        if s["kind"] == "tool" and not isinstance(s["output"], dict):
            return {"first_bad_step": s["seq"], "root_cause": "invalid_json",
                    "evidence": {"step": s["seq"], "raw": str(s["output"])[:80]},
                    "confidence": 0.9}

    # 规则 3：字段缺失（按工具 schema 校验）
    for s in steps:
        if s["kind"] == "tool" and isinstance(s["output"], dict):
            chk = sim.check_output(s["name"], s["output"])
            if chk:
                return {"first_bad_step": s["seq"], "root_cause": "missing_field",
                        "evidence": {"step": s["seq"], "tool": s["name"],
                                     "problem": chk}, "confidence": 0.85}

    # 规则 4：上下文被裁剪（声明的字段没进上下文）
    for s in steps:
        if s["kind"] == "context":
            required = (s["input"] or {}).get("required") or []
            got = s["output"] if isinstance(s["output"], dict) else {}
            lost = [k for k in required if k not in got]
            if lost:
                return {"first_bad_step": s["seq"], "root_cause": "context_loss",
                        "evidence": {"step": s["seq"], "lost_fields": lost},
                        "confidence": 0.8}

    # 规则 5：静默篡改 —— 用轨迹里的原始明细独立验算，不信计价工具的返回
    # 注意必须认 replay：恢复重放时命中缓存的上游步骤是 replay 而不是 tool，
    # 只认 tool 的话第二轮诊断会瞎掉，复合故障永远拆不干净。
    items = policy = None
    for s in steps:
        if s["kind"] in ("tool", "replay") and s["name"] == "order.get_order" \
                and isinstance(s["output"], dict):
            items = s["output"].get("items")
            policy = s["output"].get("discount_policy")
        if s["kind"] in ("tool", "replay") and s["name"] == "pricing.calc_total" \
                and isinstance(s["output"], dict) and items and policy:
            try:
                _, expect_total = _recompute(items, policy)
            except Exception:
                continue
            got = s["output"].get("total")
            if isinstance(got, (int, float)) and abs(got - expect_total) > 0.02:
                return {"first_bad_step": s["seq"], "root_cause": "silent_corruption",
                        "evidence": {"step": s["seq"], "got": got,
                                     "recomputed": expect_total,
                                     "basis": "按 get_order 明细独立重算"},
                        "confidence": 0.9}

    # 规则 5b：取错对象 —— 入参要求的标识与出参返回的标识不一致
    for s in steps:
        if s["kind"] == "tool" and isinstance(s["output"], dict):
            inp = s["input"] or {}
            for k, v in inp.items():
                if k in s["output"]:
                    av, bv = str(v), str(s["output"][k])
                    if isinstance(v, (str, int, float)) and av != bv:
                        return {"first_bad_step": s["seq"], "root_cause": "retrieval_failure",
                                "evidence": {"step": s["seq"], "field": k,
                                             "requested": av, "returned": bv},
                                "confidence": 0.85}

    # 规则 7：证据陈旧 —— 返回的文档版本低于业务要求的最低版本
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "kb.search" and isinstance(s["output"], dict):
            for d in s["output"].get("docs") or []:
                ver = d.get("version")
                if isinstance(ver, int) and ver < sim.MIN_DOC_VERSION:
                    return {"first_bad_step": s["seq"], "root_cause": "stale_data",
                            "evidence": {"step": s["seq"], "doc": d.get("doc_id"),
                                         "version": ver, "required": sim.MIN_DOC_VERSION},
                            "confidence": 0.9}

    # 规则 6：检索失败 —— 返回文档与 query 字面完全不重合
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "kb.search" \
                and isinstance(s["output"], dict):
            query = str((s["input"] or {}).get("query") or "")
            docs = s["output"].get("docs") or []
            blob = " ".join((d.get("title", "") + d.get("text", "")) for d in docs)
            if query and docs and not _overlap(query, blob):
                return {"first_bad_step": s["seq"], "root_cause": "retrieval_failure",
                        "evidence": {"step": s["seq"], "query": query,
                                     "returned": [d.get("doc_id") for d in docs]},
                        "confidence": 0.6}

    # 规则 8：格式违规 —— 答案步有内容但不是纯 JSON（TRAIL: instruction non-compliance）
    for s in steps:
        if s["kind"] == "model" and s["name"] == "answer":
            out = s["output"] if isinstance(s["output"], dict) else {}
            text = (out.get("text") or "").strip()
            if text and not out.get("parsed"):
                return {"first_bad_step": s["seq"], "root_cause": "format_violation",
                        "evidence": {"step": s["seq"], "head": text[:60]},
                        "confidence": 0.8}

    # 规则 9：工具输出幻觉 —— 答案里的数字在任何工具返回里都找不到
    tool_nums = set()
    for s in steps:
        if s["kind"] in ("tool", "replay") and isinstance(s["output"], dict):
            for m in re.findall(r"\d+(?:\.\d+)?", core.dumps(s["output"])):
                tool_nums.add(m)
    for s in steps:
        if s["kind"] == "model" and s["name"] == "answer" and isinstance(s["output"], dict):
            parsed = s["output"].get("parsed")
            if not isinstance(parsed, dict):
                continue
            for m in re.findall(r"\d+(?:\.\d+)?", core.dumps(parsed)):
                if m not in tool_nums:
                    return {"first_bad_step": s["seq"], "root_cause": "tool_hallucination",
                            "evidence": {"step": s["seq"], "number": m,
                                         "known": sorted(tool_nums)[:6]},
                            "confidence": 0.75}

    return {"first_bad_step": 0, "root_cause": "unknown",
            "evidence": {"reason": "所有规则都没命中"}, "confidence": 0.0}


# ---------- 第二层诊断：业务不变量（借鉴 AgentRx 思路，自研实现） ----------
# 顺序放在「确定性规则」之后、「未知」之前：
# 前面 6 条管「数据长什么样」，这一层管「业务上说不说得通」。
INVARIANT_TO_CAUSE = {
    "INV-ORDER-TOTAL": "silent_corruption",
    "INV-SETTLE-MATCH": "silent_corruption",
    "INV-NO-DUP-WRITE": "duplicate_execution",
    "INV-KB-CITED": "retrieval_failure",
    "INV-PLAN-SANE": "incorrect_planning",
    "INV-PLAN-EMPTY": "incorrect_planning",
    "INV-PLAN-DUP": "incorrect_planning",
    "INV-PLAN-ORDER": "incorrect_planning",
}


def diagnose_by_invariants(steps, agent_type):
    """跑不变量，返回 (root_cause, evidence) 或 (None, None)。"""
    for v in invariants.check_all(steps, agent_type):
        cause = INVARIANT_TO_CAUSE.get(v["rule_id"])
        if not cause:
            continue
        return cause, {"layer": "invariant", "rule": v["rule_id"],
                       "desc": v["desc"], "step_seq": v["step_seq"],
                       "observed": v["observed"], "expected": v["expected"],
                       "confidence": 0.88}
    return None, None


def _overlap(query, text, n=2):
    """查询与文本是否有 n-gram 重合（只用于判断"答非所问"）。"""
    q = "".join(ch for ch in query if "\u4e00" <= ch <= "\u9fff")
    t = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    if len(q) < n:
        return True
    grams = {q[i:i + n] for i in range(len(q) - n + 1)}
    return any(g in t for g in grams)


# ---------------- 恢复策略 ----------------
STRATEGY_BY_CAUSE = {
    # 瞬时故障：重试就够（这类问题原样再发一次就过去了）
    "tool_timeout": "retry",
    "tool_exception": "retry",
    "process_interruption": "replay_checkpoint",
    # 持久故障：重试没用，必须换一条路
    #   invalid_json / missing_field —— 上游返回就坏了，重发还是会坏，
    #   要换成「按 schema 修复 + 换数据源重取」
    "invalid_json": "fallback_tool",
    "missing_field": "fallback_tool",
    "retrieval_failure": "fallback_tool",
    #   silent_corruption —— 值错了但结构合法，只能交叉验算
    "silent_corruption": "cross_verify",
    #   context_loss —— 状态本身错了，要从检查点重建上下文
    "context_loss": "replay_checkpoint",
    # TRAIL / MAST 补进来的五类
    # 计划类故障：空计划/重复步骤/顺序颠倒。**不自动重规划** ——
    # 自动改计划等于让系统自己决定做什么，风险高于收益，一律转人工。
    # （runtime 侧用的是 .get(root_cause, "human_takeover")，行为一致；
    #   这里显式列出，是为了让「诊断 -> 策略」链路在表上完整可见，
    #   而不是靠默认值兜住一个没人注意的缺口。）
    "incorrect_planning": "human_takeover",
    "format_violation": "reformat",
    "wrong_tool": "fallback_tool",
    "tool_hallucination": "cross_verify",
    "resource_abuse": "replay_checkpoint",
    "premature_termination": "replay_checkpoint",
    "duplicate_execution": "replay_checkpoint",
    "unknown": "human_takeover",
}


def _record_skill_id(store, run_id, skill_id):
    """把「本次运行实际生效的 skill」写回 runs，供线上归因精确统计。

    失败不影响主流程（归因缺失最多让监控回退到时间窗近似），
    但不能静默：记一条 step 让轨迹里能看到。
    """
    if not run_id or not skill_id:
        return
    try:
        with store.conn() as c:
            c.execute("UPDATE runs SET skill_id=? WHERE id=? "
                      "AND (skill_id IS NULL OR skill_id=?)",
                      (skill_id, run_id, skill_id))
    except Exception:                                      # noqa: BLE001
        try:
            store.add_step(run_id, 0, "security", "skill_attribution",
                           {"skill_id": skill_id}, {"error": "write_failed"},
                           status="error")
        except Exception:                                  # noqa: BLE001
            pass

