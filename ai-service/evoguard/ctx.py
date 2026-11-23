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

# runtime.XXX 时，这里的方法体读的是同一对象（函数对象/可变容器）。
from runtime_common import (_fault_specs, _has_module,  # noqa: E402
                            ToolError, Interrupted, _last_attempt,
                            SYSTEM_PROMPT, MAX_STEPS, TOKEN_BUDGET,
                            WRITE_TOOLS, AUTO_APPROVE, SIDE_EFFECT_COUNT,
                            TRANSIENT_FAULTS, REPAIR_COVERAGE, REPAIR_STRATEGIES,
                            STORE_BY_MODE, FORCED_SKILL)

# HARD-04-T1：从 runtime.py 原样搬运的 Ctx。
# 常量在此模块内有一份快照；Ctx 内的方法会在运行时优先读
# runtime 的同名属性，保证测试 monkeypatch 仍然生效。

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

# ARC-DEEP-01-T2：诊断块已拆出，re-export 保持 import 兼容
from legacy_diagnose import (diagnose, _diagnose_impl, _infer_agent_type,
    diagnose_by_invariants, _recompute, _overlap, _record_skill_id,
    INVARIANT_TO_CAUSE, STRATEGY_BY_CAUSE)

