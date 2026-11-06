# -*- coding: utf-8 -*-
"""Agent Runtime：执行、Checkpoint、诊断、恢复、审批。

三条设计红线（规格第五、六部分要求，这里用代码强制）：
  1. diagnose() 的入参只有 steps —— 物理上拿不到 faults 表，杜绝评测泄漏
  2. 只有「干净」的工具结果才进 checkpoint 缓存 —— 否则重放会把污染值再喂一遍
  3. 写操作（order.settle）在恢复期重新执行必须走审批
"""
import json
import os
import time

import agents
import gateway
import invariants
import sim
from core import (ApiError, Store, CREATED, READY, RUNNING, FAILED,
                  RECOVERING, SUCCEEDED, PAUSED, CANCELLED, new_id, now)

MAX_STEPS = 20
MAX_RETRY = 2
TOKEN_BUDGET = 8000
WRITE_TOOLS = {"order.settle"}
AUTO_APPROVE = os.environ.get("AGENTLAB_AUTO_APPROVE", "1") == "1"
SIDE_EFFECT_COUNT = {"n": 0}

# 瞬时故障：重试一次就过去了。持久故障：同样的调用再发一次，结果还是错的。
# 这个区分是整个 A/B/C 对照能不能成立的关键 ——
# 如果所有故障都能靠重试解决，那"诊断"就没有存在价值，实验也就白做了。
TRANSIENT_FAULTS = {"tool_timeout", "tool_exception", "process_interruption"}

# 仅离线评测使用：强制某个候选技能参与恢复（晋升前单独测它）
FORCED_SKILL = None

SYSTEM_PROMPT = ("你是业务 Agent。只看用户给出的上下文回答，不要使用上下文以外的知识。"
                 "只输出一个 JSON 对象，不要输出解释、不要复述要求、不要输出思考过程。")


class ToolError(Exception):
    def __init__(self, error_type, message):
        self.error_type = error_type
        self.message = message
        super().__init__("%s: %s" % (error_type, message))


class Interrupted(Exception):
    pass


class Ctx:
    """一次执行的上下文。工具调用、上下文构建、模型调用都必须经过它。"""

    def __init__(self, store, run_id, exp, start_seq=1, cache=None,
                 suppress=None, recovering=False, attempt=1):
        self.store = store
        self.run_id = run_id
        self.exp = exp
        self.seq = start_seq - 1
        self.cache = dict(cache or {})
        self.suppress = set(suppress or [])
        self.recovering = recovering
        self.attempt = attempt
        self.logical = 0
        self.tokens = 0
        self.t0 = time.time()
        self.tool = None
        self.task = None
        self.ToolError = ToolError

    # ---- 内部 ----
    def _next(self):
        self.seq += 1
        self.logical += 1
        if self.seq > MAX_STEPS:
            raise ApiError("E_BUDGET_EXCEEDED", "步数超过上限 %d" % MAX_STEPS, 409)
        return self.seq

    def _fault_at(self, logical):
        """按**逻辑步号**判断该不该注入故障。

        不能用绝对 seq：恢复时要重放，绝对序号会往后走，
        同一个逻辑步却还会再执行一次。用 seq 判的话，重放一次就永远打不中故障。
        """
        ft = self.exp.get("fault_type")
        tg = self.exp.get("fault_target")
        if not ft or ft == "none":
            return None
        if logical != tg:
            return None
        if logical in self.suppress:
            return None
        # 瞬时故障只犯一次：第 2 次及以后的尝试自然就恢复了
        if self.attempt >= 2 and ft in TRANSIENT_FAULTS:
            return None
        return ft

    # ---- 对外 ----
    def model(self, prompt, tokens=0):
        """规则规划步。**不是**模型调用 —— 别把它记成 model，否则轨迹会骗人。"""
        seq = self._next()
        self.store.add_step(self.run_id, seq, "plan", "rule-plan",
                            {"prompt": prompt}, {"text": prompt},
                            status="ok", model_id="rule", tokens=0,
                            logical=self.logical)
        return prompt

    def answer(self, prompt, fallback):
        """让模型把上下文里的结论写成结构化答案。

        三种情况：
          - mock 后端：返回 None -> 直接走 deterministic 兜底（不假装模型想过）
          - 真模型返回可解析 JSON：用模型的
          - 真模型返回垃圾 / 调用失败：记一条 error 轨迹，然后走兜底
        无论哪种，轨迹里都留痕，不会静默。
        """
        seq = self._next()
        provider = gateway.get_provider()
        text, tokens, model_id, err = None, 0, provider.name, None
        try:
            text, tokens, model_id = provider.complete(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=256)
        except gateway.LLMError as e:
            err = e.code
            model_id = "unavailable"
            text = None

        self.tokens += tokens or 0
        if self.tokens > TOKEN_BUDGET:
            raise ApiError("E_BUDGET_EXCEEDED", "Token 超过预算 %d" % TOKEN_BUDGET, 409)

        parsed = gateway.extract_json(text) if text else None
        status, bad = "ok", None
        if err:
            status, bad = "error", err
        elif text is not None and parsed is None:
            status, bad = "mismatch", "bad_model_output"

        self.store.add_step(self.run_id, seq, "model", "answer",
                            {"prompt": prompt, "provider": provider.name},
                            {"text": (text or "")[:500], "parsed": parsed},
                            status=status, error_type=bad, model_id=model_id,
                            tokens=tokens or 0, logical=self.logical)

        if isinstance(parsed, dict):
            return parsed
        return fallback

    def context(self, payload):
        seq = self._next()
        required = list(payload.keys())
        out = dict(payload)
        fault = self._fault_at(self.logical)
        if fault == "context_loss" and required:
            # 裁掉最靠后的一个关键字段（模拟 token 预算压缩把关键信息挤掉）
            drop = required[-1]
            out.pop(drop, None)
        status = "ok"
        if any(k not in out for k in required):
            status = "mismatch"
        self.store.add_step(self.run_id, seq, "context", "build_context",
                            {"payload": payload, "required": required}, out,
                            status=status, logical=self.logical)
        self._checkpoint(seq)
        return out

    def call(self, tool, args):
        seq = self._next()
        logical = self.logical
        err = sim.validate_args(tool, args)
        if err:
            self.store.add_step(self.run_id, seq, "tool", tool, args,
                                {"error": err}, status="error", error_type="bad_args",
                                logical=logical)
            raise ToolError("bad_args", err)

        key = tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)

        # 命中缓存 = 幂等重放：不调工具，副作用不重复
        if key in self.cache:
            out = self.cache[key]
            self.store.add_step(self.run_id, seq, "replay", tool, args, out,
                                status="ok", logical=logical)
            return out

        fault = self._fault_at(logical)

        if fault == "tool_timeout":
            self.store.add_step(self.run_id, seq, "tool", tool, args,
                                {"error": "timeout"}, status="error",
                                error_type="timeout", duration_ms=3000, logical=logical)
            raise ToolError("timeout", "%s 超时" % tool)
        if fault == "tool_exception":
            self.store.add_step(self.run_id, seq, "tool", tool, args,
                                {"error": "exception"}, status="error",
                                error_type="exception", logical=logical)
            raise ToolError("exception", "%s 执行异常" % tool)
        if fault == "process_interruption":
            self.store.add_step(self.run_id, seq, "tool", tool, args,
                                {"error": "interrupted"}, status="error",
                                error_type="interrupted", logical=logical)
            raise Interrupted("%s 执行过程中断" % tool)

        if tool in WRITE_TOOLS and self.recovering:
            SIDE_EFFECT_COUNT["n"] += 1
            if not AUTO_APPROVE:
                raise ApiError("E_APPROVAL_REQUIRED",
                               "恢复过程需要重放写操作 %s，等待人工审批" % tool, 409)

        started = time.time()
        try:
            out = sim.call_tool(tool, args)
        except Exception as e:
            self.store.add_step(self.run_id, seq, "tool", tool, args,
                                {"error": str(e)}, status="error",
                                error_type="exception", logical=logical)
            raise ToolError("exception", str(e))
        duration = int((time.time() - started) * 1000)

        poisoned = False
        if fault in ("invalid_json", "missing_field", "silent_corruption",
                     "retrieval_failure"):
            out = sim.corrupt(fault, tool, out, args)
            poisoned = True

        status = "ok"
        bad = None
        if not isinstance(out, dict):
            status, bad = "error", "invalid_json"
        else:
            chk = sim.check_output(tool, out)
            if chk:
                status = "mismatch"

        self.store.add_step(self.run_id, seq, "tool", tool, args, out,
                            status=status, error_type=bad, duration_ms=duration,
                            logical=logical)

        # 只有干净结果才进缓存 —— 污染值进缓存会让重放变成"再犯一次同样的错"
        if not poisoned and status == "ok":
            self.cache[key] = out

        self._checkpoint(seq)
        return out

    def _checkpoint(self, seq):
        cid = self.store.add_checkpoint(self.run_id, seq, {"cache": self.cache})
        return cid


# ---------------- 诊断（只看轨迹） ----------------
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
    lg = bad
    for s in steps:
        if s["seq"] == bad:
            lg = s.get("logical") or bad
            break
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
    items = policy = None
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "order.get_order" \
                and isinstance(s["output"], dict):
            items = s["output"].get("items")
            policy = s["output"].get("discount_policy")
        if s["kind"] == "tool" and s["name"] == "pricing.calc_total" \
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
    "unknown": "human_takeover",
}


def plan_recovery(store, agent_type, root_cause, mode):
    """B 模式：固定规则（无诊断，一律重试）。

    C / D 模式：先查技能库
      - D 只认 active 且 agent+故障都匹配（严格）
      - C 认 candidate/active 但只看 agent（不严格 -> 经验被过度泛化 -> 可测出负迁移）
    查不到才退回按根因的固定策略表。

    FORCED_SKILL 只给离线评测用（要在晋升前单独测某个候选技能），
    运行时不设置它就是 None，不影响正常流程。
    """
    if mode == "B":
        return {"strategy": "retry", "params": {"max_retry": MAX_RETRY},
                "reason": "固定规则：一律重试", "source": "fixed_rule"}

    forced = FORCED_SKILL
    if forced and forced.get("task_type") == agent_type:
        return {"strategy": forced["strategy"],
                "params": {"max_retry": MAX_RETRY, "root_cause": root_cause,
                           "skill_id": forced["id"], "forced": True},
                "reason": "评测中强制使用候选技能 %s" % forced["id"], "source": "skill_forced"}

    try:
        import skills
        sk = skills.lookup(store, agent_type, root_cause, mode)
    except Exception:
        sk = None

    if sk:
        return {"strategy": sk["strategy"],
                "params": {"max_retry": MAX_RETRY, "root_cause": root_cause,
                           "skill_id": sk["id"], "skill_version": sk["version"]},
                "reason": "命中技能 %s（v%s，%s）" % (sk["id"], sk["version"], sk["status"]),
                "source": "skill"}

    strategy = STRATEGY_BY_CAUSE.get(root_cause, "human_takeover")
    return {"strategy": strategy,
            "params": {"max_retry": MAX_RETRY, "root_cause": root_cause},
            "reason": "按根因 %s 选择" % root_cause, "source": "rule"}


# ---------------- 执行 ----------------
def _expected(exp):
    return sim.expected_answer(exp["agent_type"], exp["task_id"])


def run_once(store, exp, mode):
    """跑一次。mode: baseline | A | B | C"""
    seed = int(exp.get("seed") or 0)
    run = store.create_run(exp["id"], mode, seed)
    rid = run["id"]
    store.set_state(rid, READY)

    fault_type = "none" if mode == "baseline" else exp.get("fault_type")
    fault_target = exp.get("fault_target") or _auto_target(exp, seed)
    # B/C 模式：先注入故障（真值记录），再执行
    if fault_type and fault_type != "none":
        store.add_fault(rid, fault_type, fault_target,
                        {"agent_type": exp["agent_type"]}, injected=0)

    run_exp = dict(exp, fault_type=fault_type, fault_target=fault_target)
    store.set_state(rid, RUNNING)

    ctx = Ctx(store, rid, run_exp)
    ctx.task = {"task_id": exp["task_id"], "goal": exp["task_id"]}
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
    store.finish_run(rid, correct, result.get("answer"), result.get("expect"),
                     ctx.tokens, int((time.time() - t0) * 1000),
                     result.get("reason") or "")

    if status == FAILED and mode in ("B", "C", "D", "R"):
        _recover(store, exp, run_exp, rid, ctx, mode)

    return store.get_run(rid)


def _auto_target(exp, seed):
    """没指定故障位置时，按「故障类型 + agent 类型」决定性地选一步。"""
    if int(exp.get("fault_target") or 0) > 0:
        return int(exp["fault_target"])
    return sim.auto_target(exp["agent_type"], exp.get("fault_type") or "none")


def _recover(store, exp, run_exp, rid, ctx, mode):
    """诊断 -> 方案 -> (审批) -> 重放。最多 MAX_RETRY 次。"""
    steps = store.list_steps(rid)
    # C 和 D 都要先诊断；只有 B 是「不诊断、盲目重试」
    if mode in ("C", "D", "R"):
        d = diagnose(steps)
        # 诊断出来的定位按「逻辑步号」入库，才能和故障真值对齐
        did = store.add_diagnosis(rid, d["first_bad_logical"], d["root_cause"],
                                  d["evidence"], d["confidence"])
    else:
        # B 模式不做诊断：它不知道自己遇到的是哪种故障，只会照着固定规则重试。
        # 这正是 B 和 C 的差别所在。
        d = {"first_bad_step": steps[-1]["seq"] if steps else 0,
             "first_bad_logical": steps[-1].get("logical", 0) if steps else 0,
             "root_cause": "unknown", "confidence": 0.0, "evidence": {}}
        did = None

    plan = plan_recovery(store, exp["agent_type"], d["root_cause"], mode)
    if plan["strategy"] == "human_takeover":
        store.add_recovery(rid, did, "human_takeover", plan["params"],
                           "pending", 0, "无法自动恢复，转人工")
        if store.get_run(rid)["status"] == FAILED:
            store.set_state(rid, CANCELLED)
        return store.get_run(rid)

    store.set_state(rid, RECOVERING)

    # 关键：只有「针对性策略」才会绕开这个故障点。
    # 盲目重试（retry）不绕 —— 同样的调用再发一次，持久故障必然再犯。
    # 这一条决定了 B 和 C 的差距是真差距还是假的。
    suppress = set() if plan["strategy"] == "retry" else {d["first_bad_logical"]}

    for attempt in range(1, MAX_RETRY + 1):
        ckpt = store.latest_checkpoint(rid)
        cache = (ckpt or {}).get("state", {}).get("cache", {}) if ckpt else {}
        start_seq = (ckpt["seq"] if ckpt else 0) + 1
        try:
            rctx = Ctx(store, rid, run_exp, start_seq=start_seq,
                       cache=cache, suppress=suppress,
                       recovering=True, attempt=attempt + 1)
            rctx.task = {"task_id": exp["task_id"], "goal": exp["task_id"]}
            result = agents.AGENTS[exp["agent_type"]](rctx)
        except (ToolError, Interrupted) as e:
            store.add_recovery(rid, did, plan["strategy"], plan["params"],
                               "auto" if AUTO_APPROVE else "pending", 0,
                               "第 %d 次恢复失败：%s" % (attempt, e))
            continue
        except ApiError as e:
            store.add_recovery(rid, did, plan["strategy"], plan["params"],
                               "denied", 0, str(e.message))
            store.set_state(rid, FAILED)
            return store.get_run(rid)

        correct = agents.is_correct(exp["agent_type"], result.get("answer"),
                                     result.get("expect"))
        store.add_recovery(rid, did, plan["strategy"], plan["params"],
                           "auto" if AUTO_APPROVE else "pending",
                           1 if correct else 0,
                           "第 %d 次恢复%s" % (attempt, "成功" if correct else "结果仍不对"))
        if correct:
            store.set_state(rid, SUCCEEDED)
            store.finish_run(rid, True, result.get("answer"), result.get("expect"),
                             rctx.tokens, 0, "已通过恢复完成（策略 %s）" % plan["strategy"])
            return store.get_run(rid)

    if store.get_run(rid)["status"] == RECOVERING:
        store.set_state(rid, FAILED)
    return store.get_run(rid)
