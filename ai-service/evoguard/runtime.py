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

MAX_STEPS = 20
MAX_RETRY = 2
MAX_CHAIN = 3              # 一条策略链最多几步，防止无限恢复
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

# 仅离线评测使用：按模式指定技能库。
# 为什么需要它：C 和 D 的差别本来只在「验证严不严」，但如果两边共用同一个库，
# 这个变量根本没被激活 —— 库是严格验证出来的，C 用不用严格验证都一样。
# 要测出治理价值，C 必须用**自己**那套放宽流程训出来的库（里面全是没验证过的经验）。
# 生产运行时不设置它，行为与以前完全一致（就用调用方传进来的 store）。
STORE_BY_MODE = {}

# 策略真实语义：哪些策略只是「重放」，哪些会**修复输出**。
# 这一层是关键 —— 如果所有策略都只靠 suppress 把故障点绕过去，
# 那"策略"就只是个名字，组合起来也不会有任何增量。
# replan 故意不在里面：计划顺序的正确性由「流程不变量」诊断，
# 修复靠抑制故障点后重放（工具返回自然就是干净的），
# 而不是拿一张动词词典去猜标准答案 —— 那等于把真值塞进恢复逻辑，是自欺。
REPAIR_STRATEGIES = {"cross_verify", "fallback_tool", "reformat"}

# 每个修复式策略真正能兜住的「诊断根因」。
# 只有根因落在覆盖范围内，才让故障照常注入、由策略修好；
# 否则沿用旧的抑制重放，保证单策略链的基线下限不被打破。
# 注意判定依据是**诊断结果**（只看轨迹），不是故障真值 —— 没有评测泄漏。
REPAIR_COVERAGE = {
    "cross_verify": {"silent_corruption"},
    "fallback_tool": {"missing_field", "invalid_json", "wrong_tool",
                      "retrieval_failure", "stale_data", "incorrect_planning"},
    "reformat": {"format_violation"},
}


def _has_module(name):
    """检查器是否真的可导入。不能只看配置里写没写名字 —— 那只是字符串。"""
    try:
        __import__(name)
        return True
    except Exception:                     # noqa: BLE001
        return False


def _fault_specs(run_exp):
    """把故障描述统一成 [{type,target}, ...]。

    exp 里可能是：
      - fault_type / fault_target：单故障（旧版全部用例走这条，保持兼容）
      - fault_chain：[{"type","target"}, ...]：复合故障，多个故障落在不同逻辑步
    """
    chain = run_exp.get("fault_chain")
    if chain:
        return [{"type": f["type"], "target": int(f.get("target") or 0)} for f in chain]
    ft = run_exp.get("fault_type")
    if ft and ft != "none":
        return [{"type": ft, "target": int(run_exp.get("fault_target") or 0)}]
    return []


def _last_attempt(steps):
    """只取「最后一次执行尝试」的轨迹做诊断。

    多轮恢复时，steps 里会累积前几轮的重放轨迹；若拿全量去诊断，
    重复的 tool 步会被规则 0b 误判成 resource_abuse，诊断就脏了。
    每次 Ctx 的 logical 都从 1 重新计数，所以最后一个 logical==1 就是本轮起点。
    """
    start = 0
    for i, s in enumerate(steps):
        if (s.get("logical") or 0) == 1:
            start = i
    return steps[start:] if start else steps

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
                 suppress=None, recovering=False, attempt=1, strategy=None,
                 round_root_cause=None, approval_gate=None,
                 forced_skill=None):
        self.store = store
        self.run_id = run_id
        self.exp = exp
        # 可恢复审批状态机（S04）。默认 None：历史实验行为完全不变，
        # 294 项回归与冻结数字仍然可比；只有真实副作用任务才显式挂上。
        self.approval_gate = approval_gate
        # WS-02-T2：候选技能是**本次执行**的状态，不再是进程级全局。
        # 全局 FORCED_SKILL 仅为旧脚本/测试保留（见 plan_recovery 的回退）。
        self.forced_skill = forced_skill
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
        self.fault_specs = _fault_specs(exp)
        # 本轮生效的恢复策略。只有恢复期才有值；A/B/C/D 的首次执行永远是 None，
        # 所以「策略」不会偷偷影响基线组，评测才干净。
        self.strategy = strategy
        # 本轮诊断出的根因：修复动作的**授权凭据**。
        # 没有它，某个策略会对沿途所有它认识的故障顺手全修，
        # 单策略就能解决复合故障，组合策略的增量是假的。
        self.round_root_cause = round_root_cause
        self.repairs = []          # 本轮真实做过的修复动作，写进轨迹备查
        # 降级记录：模型不可用/报错后仍给出答案，必须留痕。
        # 「跑完了、有答案、看起来成功」但其实是规则兜底 —— 这是最危险的一类静默失效。
        self.degraded = []
        # 统一执行入口的生效配置（评审 R01）。
        # legacy 是历史实验默认档：行为与此前完全一致，只在轨迹里标注配置；
        # controlled 会把「写操作无审批后端 / 身份未知 / 检查器缺失」变成启动期错误。
        self.exec_config = execution.build_config(
            os.environ,
            agent_type=exp.get("agent_type"),
            allowed_tools=security.allowed_tools(exp.get("agent_type")),
            write_tools=WRITE_TOOLS,
            has_approval_backend=approval_gate is not None,
            has_contract=_has_module("tool_contract"),
            has_intent_judge=_has_module("semantic_guard"))
        self.exec_config_export = execution.export(self.exec_config)
        # 本轮用户目标：意图门禁的判据。没有就别猜 —— 受控档会转复核。
        self.user_goal = (exp.get("user_goal") or "").strip()
        self.calls = []            # 每次调用的前置判断记录（含 call_id）

    # ---- 内部 ----
    def _next(self):
        self.seq += 1
        self.logical += 1
        # 预算按**本轮**的逻辑步数算，不能用绝对 seq：
        # 策略链每轮都追加到轨迹末尾，绝对 seq 会累积，
        # 用 seq 判会让第 3 轮一开局就「超预算」，链长永远上不去。
        if self.logical > MAX_STEPS:
            raise ApiError("E_BUDGET_EXCEEDED", "单轮步数超过上限 %d" % MAX_STEPS, 409)
        return self.seq

    def _fault_at(self, logical):
        """按**逻辑步号**判断该不该注入故障。

        不能用绝对 seq：恢复时要重放，绝对序号会往后走，
        同一个逻辑步却还会再执行一次。用 seq 判的话，重放一次就永远打不中故障。

        复合故障：多个故障可以落在不同逻辑步。suppress 是按逻辑步累积的，
        所以策略链每解决一个点，下一个故障会在下一轮重放中暴露出来 ——
        这正是「单策略救不回、组合策略能救回」的物理基础。
        """
        for spec in self.fault_specs:
            ft = spec["type"]
            if not ft or ft == "none" or spec["target"] != logical:
                continue
            if logical in self.suppress:
                continue
            # 瞬时故障只犯一次：第 2 次及以后的尝试自然就恢复了
            if self.attempt >= 2 and ft in TRANSIENT_FAULTS:
                continue
            return ft
        return None

    def _repair(self, fault, tool, args, out):
        """策略的**真实动作**：不是绕开故障，而是把坏输出修成好输出。

        三种可验证的修复，都只依赖轨迹里看得见的信息，绝不读 expected_answer：
          - cross_verify：数值结果用上游明细独立重算覆盖（治 silent_corruption）
          - fallback_tool：结构损坏时按 schema 从同一批数据重建（治 missing_field / invalid_json）
          - replan：计划步骤按「先准备后收尾」的通用约束重排（治 incorrect_planning）
        其余策略不改变输出，只靠重放 —— 如实记录，不假装做了修复。

        注意：修复只在恢复期、且策略匹配时生效。首次执行（A 组）永远不修。
        """
        if not self.recovering or not self.strategy or not fault:
            return out
        # 授权：本轮诊断出的根因必须落在该策略的可修复范围内。
        # 否则策略只是「路过」，不能顺手把别的故障也修了 ——
        # 那会让单策略等价于组合策略，实验结论不成立。
        s = self.strategy
        if self.round_root_cause not in REPAIR_COVERAGE.get(s, set()):
            return out
        if s == "cross_verify" and tool == "pricing.calc_total" \
                and isinstance(out, dict) and "total" in out:
            # 从轨迹里找上游订单明细（这是诊断也能看到的合法证据）。
            # 注意不能一遇到没有 items 的步就 break：故障超时的那一步
            # 输出是 {"error": "timeout"}，它排在最前面，break 会让验算永远失效。
            for step in reversed(self.store.list_steps(self.run_id)):
                if step.get("name") != "order.get_order" or not isinstance(step.get("output"), dict):
                    continue
                items = step["output"].get("items")
                pol = step["output"].get("discount_policy")
                if not items or not isinstance(pol, dict):
                    continue
                try:
                    _, expect = _recompute(items, pol)
                    if abs(float(out["total"]) - expect) > 0.02:
                        fixed = dict(out)
                        fixed["total"] = expect
                        self.repairs.append({"strategy": s, "tool": tool,
                                             "action": "recompute_total",
                                             "from": out["total"], "to": expect})
                        return fixed
                except Exception:
                    pass
                break
        if s == "fallback_tool" and fault in REPAIR_COVERAGE["fallback_tool"]:
            # 换一条取数路径：重新调一次「干净」的同一工具，绕过被污染的返回。
            # 这一步不读真值 —— 只是「换数据源重取」，真实系统里就是这么做的。
            try:
                clean = sim.call_tool(tool, args)
                if isinstance(clean, dict) and not sim.check_output(tool, clean):
                    self.repairs.append({"strategy": s, "tool": tool,
                                         "action": "refetch_clean", "fault": fault})
                    return clean
            except Exception:
                pass
        return out

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
                temperature=0.0, max_tokens=1500)
        except gateway.LLMError as e:
            err = e.code
            model_id = "unavailable"
            text = None
            self.degraded.append(err)

        self.tokens += tokens or 0
        if self.tokens > TOKEN_BUDGET:
            raise ApiError("E_BUDGET_EXCEEDED", "Token 超过预算 %d" % TOKEN_BUDGET, 409)

        # ---- 安全层：模型输出扫描 ----
        # 只记账，不改文本：答案还要被解析成 JSON 参与判定，
        # 在这里改写会让「格式违规」变成「内容被转义」，评测结论会失真。
        # 真正面向浏览器的编码在 API 边界（Content-Type + CSP + 转义）做。
        if text:
            out_sec = security.guard_model_output(text, "text", "model_output")
            if out_sec["findings"]:
                security.record(self.store, self.run_id, "model_output", out_sec)
            # guard 返回的编码文本在本链路**没有被采用**（答案要参与 JSON 判定），
            # 所以后果如实记为未执行；面向页面的编码在 API 边界另做。
            security.record_effect(
                self.store, self.run_id, "model_output", out_sec,
                override={"decision": security.DECISION_REPORT,
                          "attempted": False, "executed": False,
                          "effect": (security.EFFECT_UNCHANGED
                                     if out_sec["detected"] else security.EFFECT_NONE),
                          "reason": ("运行库内部不改写答案：guard 的编码结果未采用；"
                                     "面向页面的编码在 API 边界做，本链路未执行"
                                     if out_sec["detected"] else "未命中特征")})

        fault = self._fault_at(self.logical)

        if fault == "premature_termination":
            self.store.add_step(self.run_id, seq, "stop", "early_exit",
                                {"prompt": prompt[:80]}, {"reason": "没拿到结果就宣布完成"},
                                status="ok", logical=self.logical)
            return {}

        if fault == "format_violation":
            text = "好的，我来回答：%s" % core.dumps(fallback)
            parsed = None
            # reformat：不绕开故障，而是把散文里的 JSON 结构重新抽出来。
            # 这是「修复式恢复」——故障照常发生了，被策略修好，而不是被假装没发生。
            if self.recovering and self.strategy == "reformat" and \
                    self.round_root_cause in REPAIR_COVERAGE["reformat"]:
                m = re.search(r"\{.*\}", text, re.S)
                cand = gateway.extract_json_strict(m.group(0)) if m else None
                if isinstance(cand, dict):
                    parsed = cand
                    self.repairs.append({"strategy": "reformat", "tool": "answer",
                                         "action": "extract_json_from_prose"})
            self.store.add_step(self.run_id, seq, "model", "answer",
                                {"prompt": prompt[:120], "provider": provider.name,
                                 "repairs": self.repairs[-3:]},
                                {"text": text[:500], "parsed": parsed},
                                status="mismatch" if parsed is None else "ok",
                                error_type="format_violation" if parsed is None else None,
                                model_id=model_id, tokens=tokens or 0, logical=self.logical)
            # 关键：这里**不能**无条件返回 fallback。模型没按格式给出结构，
            # Agent 实际上拿不到答案；只有 reformat 真修出来了才给结构化结果，
            # 否则返回正确兜底会把「格式违规」洗成成功，A 组就永远测不出这一类。
            return parsed if isinstance(parsed, dict) else {}

        parsed = gateway.extract_json_strict(text) if text else None
        if fault == "tool_hallucination":
            # mock 后端没有模型输出可篡改，就拿兜底答案开刀 —— 语义一样：
            # 答案里出现了一个任何工具都没返回过的数字
            if not isinstance(parsed, dict):
                parsed = dict(fallback) if isinstance(fallback, dict) else {}
            for k, v in list(parsed.items()):
                if isinstance(v, (int, float)):
                    parsed[k] = round(v + 777.0, 2)
                elif isinstance(v, str):
                    # 文字答案就改数字：只追加一句话不会影响「必须包含某片段」的判定
                    parsed[k] = re.sub(r"\d+", lambda m: str(int(m.group(0)) + 7), v)
                elif isinstance(v, list):
                    parsed[k] = list(v) + ["补充步骤 777"]
            parsed = parsed or {}
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

        # ---- 安全层：最小权限 ----
        # 每个 Agent 只允许调它声明过的工具。未知 Agent 只记 warn 不拦，
        # 否则新增一个 Agent 忘了登记就被全线挡住 —— 那会变成可用性问题。
        deny = security.check_tool(self.exp.get("agent_type"), tool)
        if deny:
            blocked = security.enforcing() and deny["severity"] == "error"
            sec = security.contract(
                {"text": str(tool),
                 "findings": [dict(deny, where="tool_call", match=str(tool))],
                 "action": "blocked" if blocked else "reported"},
                decision=security.DECISION_DENY if blocked else security.DECISION_REPORT,
                attempted=False, executed=False, effect=security.EFFECT_UNCHANGED,
                reason="检测到越权工具；是否拒绝由下方真实执行点决定")
            security.record(self.store, self.run_id, "tool_call", sec)
            if blocked:
                self.store.add_step(self.run_id, seq, "security", tool, args,
                                    {"error": deny["code"]}, status="error",
                                    error_type=deny["code"], logical=logical)
                security.record_effect(
                    self.store, self.run_id, "tool_call", sec, tool=tool,
                    override={"decision": security.DECISION_DENY,
                              "attempted": True, "executed": True,
                              "effect": security.EFFECT_CALL_STOPPED,
                              "reason": "enforce：越权工具在调用前被拒绝，"
                                        "未执行 sim.call_tool"})
                raise ToolError(deny["code"], deny["detail"])
            security.record_effect(
                self.store, self.run_id, "tool_call", sec, tool=tool,
                override={"attempted": False, "executed": False,
                          "effect": security.EFFECT_REPORTED_ONLY,
                          "reason": "audit 模式只记账：工具照常执行，未拦截"})

        # ---- 安全层：参数级拦截（执行前）----
        # 危险命令与外带一旦执行，事后只能看到结果，数据已经出门了。
        arg_sec = security.guard_tool_args(self.exp.get("agent_type"), tool, args)
        if arg_sec["findings"]:
            security.record(self.store, self.run_id, "tool_args:%s" % tool, arg_sec)
        if arg_sec["action"] == "blocked":
            codes = sorted({f["code"] for f in arg_sec["findings"]})
            self.store.add_step(self.run_id, seq, "security", tool, args,
                                {"error": codes}, status="error",
                                error_type="security_blocked", logical=logical)
            security.record_effect(
                self.store, self.run_id, "tool_args:%s" % tool, arg_sec, tool=tool,
                override={"decision": security.DECISION_DENY,
                          "attempted": True, "executed": True,
                          "effect": security.EFFECT_CALL_STOPPED,
                          "reason": "enforce：参数命中高危特征，调用前拒绝，工具未执行"})
            raise ToolError("security_blocked", "参数被安全策略拦截：%s" % "、".join(codes))
        elif arg_sec["findings"]:
            security.record_effect(
                self.store, self.run_id, "tool_args:%s" % tool, arg_sec, tool=tool,
                override={"attempted": False, "executed": False,
                          "effect": security.EFFECT_REPORTED_ONLY,
                          "reason": "audit 模式只记账：参数原样传入，工具照常执行"})

        err = sim.validate_args(tool, args)
        if err:
            self.store.add_step(self.run_id, seq, "tool", tool, args,
                                {"error": err}, status="error", error_type="bad_args",
                                logical=logical)
            raise ToolError("bad_args", err)

        # ---- 统一执行入口（评审 R01）----
        # 此前 tool_contract.authorize 与 security.guard_tool_intent 都写好了却
        # 一次都没被真实调用：把两者替成恒定 deny，工具照样执行 —— 模块通过不等于生效。
        # 现在两者都在**真实执行点**之前跑，且 stop 时真的不调用 sim.call_tool。
        cid = execution.call_id(self.run_id, seq, tool, args)
        pre = execution.preflight(
            self.exec_config, agent_type=self.exp.get("agent_type"),
            tool=tool, args=args, user_goal=self.user_goal, cid=cid)
        self.calls.append(pre)
        security.record(
            self.store, self.run_id, "exec_preflight:%s" % tool,
            {"call_id": cid, "detected": pre["decision"] in execution.STOP,
             "findings": [], "decision": pre["decision"],
             "attempted": True, "executed": False,
             "effect": security.EFFECT_UNCHANGED, "reason": pre["reason"],
             "checks": pre["checks"]})
        if pre["stop"]:
            code = ("security_deny" if pre["decision"] == execution.DENY
                    else "security_review")
            self.store.add_step(
                self.run_id, seq, "security", tool, args,
                {"error": code, "call_id": cid, "decision": pre["decision"],
                 "checks": pre["checks"]},
                status="error", error_type=code, logical=logical)
            # 关键：这里没有走到 sim.call_tool，工具调用次数为 0。
            security.record_effect(
                self.store, self.run_id, "exec_preflight:%s" % tool, {}, tool=tool,
                override={"detected": True, "decision": security.DECISION_DENY,
                          "action": "blocked", "attempted": True,
                          "executed": True, "effect": security.EFFECT_CALL_STOPPED,
                          "call_id": cid,
                          "reason": "统一执行入口判定 %s：%s；未执行 sim.call_tool"
                                    % (pre["decision"], pre["reason"])})
            raise ToolError(code, "调用前门禁未通过：%s" % pre["reason"])

        # 归一化只做一次：之后的审批摘要、缓存键、真实执行都用这个对象。
        # 不这么做的话，「授权看 amount、执行读 total」的错位还会回来。
        if isinstance(pre.get("verified_args"), dict):
            if pre["verified_args"] != args:
                self.store.add_step(
                    self.run_id, seq, "security", tool, args,
                    {"aliases_folded": True, "verified_args": pre["verified_args"],
                     "call_id": cid},
                    status="ok", logical=logical)
                args = pre["verified_args"]

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

        # ---- 统一写操作门禁（S04）：首次写入与恢复期重放写入走同一条路 ----
        # 默认 approval_gate=None，历史实验行为完全不变；真实副作用任务显式挂上
        # ApprovalGate 后，两种写入都必须经过可暂停、可恢复、一次性消费的审批。
        if self.approval_gate is not None and tool in WRITE_TOOLS:
            gate = self.approval_gate
            write_kind = "首次写入" if not self.recovering else "恢复期重放写入"
            try:
                dec = gate.require(
                    self.run_id, tool, args,
                    allowed_tools=security.allowed_tools(self.exp.get("agent_type")),
                    tool_contract={tool: {"side_effect": True}},
                    first_write=not self.recovering)
            except approval.ApprovalRequired as e:
                self.store.add_step(
                    self.run_id, seq, "security", tool, args,
                    {"error": "approval_required", "request_id": e.request_id,
                     "checkpoint_id": e.checkpoint_id},
                    status="error", error_type="approval_required", logical=logical)
                security.record_effect(
                    self.store, self.run_id, "approval_gate", {}, tool=tool,
                    override={"detected": True, "decision": security.DECISION_DENY,
                              "action": "blocked", "attempted": True,
                              "executed": True,
                              "effect": security.EFFECT_CALL_STOPPED,
                              "reason": "统一门禁：%s 暂停等待审批 request=%s，"
                                        "工具未执行" % (write_kind, e.request_id)})
                raise
            except approval.ApprovalDenied as e:
                self.store.add_step(
                    self.run_id, seq, "security", tool, args,
                    {"error": "approval_invalid", "request_id": e.request_id},
                    status="error", error_type="approval_invalid", logical=logical)
                raise ApiError("E_APPROVAL_INVALID", e.reason, 409)
            security.record_effect(
                self.store, self.run_id, "approval_gate", {}, tool=tool,
                override={"detected": True, "decision": security.DECISION_ALLOW,
                          "action": "approved", "attempted": True,
                          "executed": True, "effect": "approval_consumed",
                          "reason": "统一门禁：审批 %s 已一次性消费，允许执行%s"
                                    % (dec["request_id"], write_kind)})

        elif tool in WRITE_TOOLS and self.recovering:
            SIDE_EFFECT_COUNT["n"] += 1
            # enforce 模式下，AUTO_APPROVE 不再能默默放行写操作 ——
            # 否则「审批」只是一句配置，跟没有一样。
            allow_auto = AUTO_APPROVE and (
                not security.enforcing()
                or os.environ.get("AGENTLAB_ALLOW_AUTO_APPROVE") == "1")
            if not allow_auto:
                req = security.contract(
                    {"text": str(tool),
                     "findings": [{"code": "approval_required", "severity": "error",
                                  "where": "tool_approval", "match": tool,
                                  "detail": "写操作 %s 在恢复期重放，未经审批" % tool,
                                  "false_positive": ""}],
                     "action": "blocked"},
                    decision=security.DECISION_DENY, attempted=False,
                    executed=False, effect=security.EFFECT_UNCHANGED,
                    reason="写操作需要审批；是否阻断由下方真实执行点决定")
                security.record(self.store, self.run_id, "tool_approval", req)
                self.store.add_step(self.run_id, seq, "security", tool, args,
                                    {"error": "approval_required"}, status="error",
                                    error_type="approval_required", logical=logical)
                security.record_effect(
                    self.store, self.run_id, "tool_approval", req, tool=tool,
                    override={"attempted": True, "executed": True,
                              "effect": security.EFFECT_CALL_STOPPED,
                              "reason": "恢复期写操作未经审批：抛出 "
                                        "E_APPROVAL_REQUIRED，未执行 sim.call_tool"})
                raise ApiError("E_APPROVAL_REQUIRED",
                               "恢复过程需要重放写操作 %s，等待人工审批" % tool, 409)
            if AUTO_APPROVE:
                # 基准实验默认自动放行，但必须留痕：报告里要能看到「审批被绕过多少次」
                by = security.contract(
                    {"text": str(tool),
                     "findings": [{"code": "approval_bypassed", "severity": "warn",
                                  "where": "tool_approval", "match": tool,
                                  "detail": "写操作 %s 自动放行（AGENTLAB_AUTO_APPROVE）" % tool,
                                  "false_positive": ""}],
                     "action": "reported"},
                    decision=security.DECISION_ALLOW, attempted=False,
                    executed=False, effect=security.EFFECT_UNCHANGED,
                    reason="自动放行：审批被绕过，写操作照常执行")
                security.record(self.store, self.run_id, "tool_approval", by)
                security.record_effect(
                    self.store, self.run_id, "tool_approval", by, tool=tool,
                    override={"attempted": False, "executed": False,
                              "effect": security.EFFECT_UNCHANGED,
                              "reason": "AGENTLAB_AUTO_APPROVE：审批被绕过，"
                                        "写操作照常执行，未拦截"})

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
        # 这里必须列全：漏一个，corrupt() 就永远不会被调用，故障等于没注入。
        if fault in ("invalid_json", "missing_field", "silent_corruption",
                     "retrieval_failure", "stale_data", "incorrect_planning",
                     "wrong_tool"):
            out = sim.corrupt(fault, tool, out, args)
            poisoned = True
            # 策略的真实动作：故障照样发生，但恢复策略能把它修回来。
            # 单策略链下这个点通常已被 suppress，走不到这里；
            # 复合故障里「第二个故障点」没被抑制，修复就会真正生效 ——
            # 这是策略不只是标签、组合确实有增量的可观测证据。
            repaired = self._repair(fault, tool, args, out)
            if repaired is not out:
                out = repaired
                poisoned = False      # 已修好的值可以进缓存，重放不会再把脏值喂一遍

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

        # ---- 安全层：工具输出扫描 ----
        # 轨迹里仍存原始输出（评测要看真值），发现单独记账；
        # enforce 下才把可疑内容包成不可信数据再交给 Agent。
        out_sec = security.guard_tool_output(tool, out)
        if out_sec["findings"]:
            security.record(self.store, self.run_id, "tool_output:%s" % tool, out_sec)
        before = out
        wrapped = (security.enforcing() and security.wrapping()
                   and any(f["severity"] == "error" for f in out_sec["findings"]))
        if wrapped:
            out = security.mark_payload(out, source=tool)
        # S01：只有**真的改了交给 Agent 的对象**才算内容隔离生效。
        # guard 报的 blocked 只是检测建议，不能拿来统计拦截成功。
        changed = out != before
        security.record_effect(
            self.store, self.run_id, "tool_output:%s" % tool, out_sec, tool=tool,
            override={
                "decision": (security.DECISION_WRAP if changed
                             else out_sec["decision"]),
                "attempted": wrapped and bool(out_sec["detected"]),
                "executed": changed,
                "effect": (security.EFFECT_BODY_MARKED if changed
                           else security.EFFECT_UNCHANGED if out_sec["detected"]
                           else security.EFFECT_NONE),
                "reason": ("enforce + WRAP：工具结果对象已标记不可信，"
                           "交给 Agent 的内容确实变了" if changed else
                           "enforce 但 AGENTLAB_SECURITY_WRAP 未开启：工具结果"
                           "对象逐字未改、原样进入 Agent；blocked 仅为检测建议"
                           if out_sec["detected"] else
                           "未命中特征，工具结果原样进入 Agent"),
            })

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
    "format_violation": "reformat",
    "wrong_tool": "fallback_tool",
    "tool_hallucination": "cross_verify",
    "resource_abuse": "replay_checkpoint",
    "premature_termination": "replay_checkpoint",
    "duplicate_execution": "replay_checkpoint",
    "unknown": "human_takeover",
}


def plan_recovery(store, agent_type, root_cause, mode, scope="single",
                  forced_skill=None):
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
        chain = core.as_chain(forced["strategy"])
        return pack(chain, {"max_retry": MAX_RETRY, "root_cause": root_cause,
                            "skill_id": forced["id"], "forced": True, "chain": chain},
                    "评测中强制使用候选技能 %s" % forced["id"], "skill_forced")

    try:
        import skills
        sk_store = STORE_BY_MODE.get(mode) or store
        sk = skills.lookup(sk_store, agent_type, root_cause, mode, scope=scope)
    except Exception:
        sk = None

    if sk:
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
                         scope=scope,
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
