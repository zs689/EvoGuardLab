# -*- coding: utf-8 -*-
"""真实多轮自主 Agent 引擎（WS-02-T1）。

与内置 `agents.py` 的区别：内置 agent 的步骤是写死的 Python 函数；
这里**由模型自己在每一轮决定调哪个工具、传什么参数**，平台只负责：

1. 从 `tool_registry` 动态生成可用工具清单与 JSON Schema，注入 system prompt；
2. 把模型的每次 tool_call 送进 `execution.preflight()`（契约→意图→审批）
   和 `tool_rails.check_call()`（返回侧 check_result）—— 这是唯一真实门禁；
3. 任何一层 stop / blocked → **不执行工具**，按六字段契约记账；
4. `max_steps` 预算 + 死循环检测（同一 (tool,args) 重复 N 次熔断）。

**不假装执行**：工具的真实副作用由调用方注入 `executor(tool, args) -> dict`。
没有 executor 时默认用 tool_registry 里注册的执行器；两者都没有就返回
`executed=False, effect=no_executor`，绝不编一个假结果喂给模型继续推理。

零第三方运行时依赖（核心库约束）；模型通过 `gateway.get_provider()` 注入。
"""
import hashlib
import json
import os
import time

try:
    from . import execution as _exec
    from . import gateway as _gw
    from . import tool_rails as _rails
    from . import tool_registry as _reg
    from . import security as _sec
    from . import core as _core
except ImportError:                                        # sys.path 直插用法
    import execution as _exec                              # type: ignore
    import gateway as _gw                                  # type: ignore
    import tool_rails as _rails                            # type: ignore
    import tool_registry as _reg                           # type: ignore
    import security as _sec                                # type: ignore
    import core as _core                                   # type: ignore


# DEEP-05：所有拆出符号经统一门面 re-export（含下划线名）
from agent_reexport import *  # noqa: F401,F403

class ReActAgent:
    def __init__(self, *, agent_type="react", goal="", provider=None,
                 registry=None, executor=None, cfg=None, rails=None,
                 max_steps=8, max_same_call=2, enforce=True,
                 has_approval_backend=False,
                 memory_store=None, memory_task_type=None,
                 session_id=None, store=None, tenant_id="default",
                 session_budget=None, plan_first=False, self_reflect=False,
                 approval_id=None, skill_hooks=None, auto_learn=False,
                 learn_task_type=None, learn_scope="candidate"):
        self.agent_type = agent_type
        self.goal = goal
        self.provider = provider or _gw.get_provider()
        self.registry = registry or _reg.REGISTRY
        self.executor = executor
        self.rails = rails or {}
        self.max_steps = max(1, int(max_steps))
        self.max_same_call = max(1, int(max_same_call))
        self.enforce = bool(enforce)
        # INT-04-T1：跨任务长程记忆（可选）。没有 store 时行为完全不变。
        self.session_id = session_id
        self.store = store                    # session 持久化用；没有则不记
        self.tenant_id = tenant_id
        self.session_budget = session_budget or SESSION_BUDGET_CHARS
        self.memory_store = memory_store
        # 不传时用 None：agent_type（react）与 task_type（order）是两个维度，
        # 拿 agent_type 当任务类型会把所有跨任务经验过滤掉。
        self.memory_task_type = memory_task_type
        self.memory_stats = {"injected": 0, "ids": []}
        tools = sorted(build_tools_prompt(self.registry, agent_type))
        writes = [t for t in tools
                  if (self.registry.get(t) or {}).get("side_effect") == "write"]
        # **不许伪造配置**：controlled 模式要求写工具有审批后端。
        # 没后端就明确降为 legacy，并在 cfg 里留下 gaps 说明，
        # 不能为了「让测试过」而在 controlled 下谎称有后端。
        want_controlled = bool(enforce)
        effective_controlled = want_controlled and (
            not writes or bool(has_approval_backend))
        mode_env = "controlled" if effective_controlled else "legacy"
        self.cfg = cfg or _exec.build_config(
            {"AGENTLAB_EXEC_MODE": mode_env},
            agent_type=agent_type, allowed_tools=tools,
            write_tools=writes,
            has_approval_backend=bool(has_approval_backend),
            has_contract=True, has_intent_judge=True)
        self.cfg["react_enforce"] = bool(enforce)
        self.cfg["requested_controlled"] = want_controlled
        self.cfg["downgraded"] = want_controlled and not effective_controlled
        # AGENT-PRO-01-T1：显式计划模式开关。计划可由模型先产出或由调用方
        # 提供；无论来源，执行都走 PlanRunner 的检查点/审批断点，不新开旁路。
        self.plan_first = bool(plan_first)
        # AGENT-KERN-01：自我反思开关。开启后，工具返回测试失败(returncode!=0)
        # 或执行出错时，自动向模型注入一条结构化反思提示，驱动其下轮换策略。
        # 这是**架构级增益**：同模型 A/B 时唯一变量就是它，与模型强弱解耦。
        self.self_reflect = bool(self_reflect)
        # MOD-AGENT-01-T1：按需反思状态（连续失败计数 + 上次错误类型）
        self._fail_streak = 0
        self._last_fail_kind = ""
        self.reflections = []
        # FINAL-01-T2：跨步骤污点状态（外部工具输出 -> 写/外发工具的数据流）
        self.taint_state = {"user_goal": goal,
                            "taint_sources": {"urls": [], "emails": [],
                                              "accounts": [], "amounts": [],
                                              "paths": []}}
        # 预授权写工具的审批 id（由 harness 预先建单批准）。preflight 在
        # executor 之前执行，所以必须在 preflight 前注入，不能只靠 executor。
        self.approval_id = approval_id
        # FINAL-02-T2：可执行 Skill 钩子（非提示词注入）
        self.skill_hooks = skill_hooks or {}
        # EVO-DEEP-02-T1：在线学习开关（默认关，行为完全不变）。
        # 开启后：开头按 task_type 检索已学规约注入，收尾把
        # 「失败 -> 后续成功修复」的成对轨迹自动提炼入库。
        self.auto_learn = bool(auto_learn)
        self.learn_task_type = learn_task_type or agent_type
        self.learn_scope = learn_scope
        self.learn_stats = {"injected": 0, "skill_id": None,
                            "learned": False, "reason": "off"}

    def _preflight(self, tool, args, seq, observation=None):
        """契约 → 意图 → 审批。工具调用的唯一放行点。"""
        # 预授权审批 id 必须在契约校验**之前**注入，否则写工具会被判
        # approval_request_id 缺失（preflight 早于 executor）。
        if self.approval_id and tool == "fs.write" and isinstance(args, dict):
            args = dict(args)
            args.setdefault("approval_request_id", self.approval_id)
        # 把注册表里的契约传给 preflight：否则它默认读 tool_contract
        # 的硬编码 DEFAULT_CONTRACTS，动态注册的工具会被当成「身份不合规」
        # 一律拒绝（两层契约源不一致）。这是接线，不是放宽门禁。
        contracts = {
            name: {k: v for k, v in item.items() if not k.startswith("_")}
            for name, item in (self.registry.declared() or {}).items()
        }
        out = _exec.preflight(
            self.cfg, agent_type=self.agent_type, tool=tool, args=args,
            user_goal=self.goal, contracts=contracts,
            taint_state=getattr(self, "taint_state", None),
            cid=_exec.call_id("react", seq, tool, args),
            rails=self.rails or None)
        if not out.get("stop"):
            return out
        return out

    def run(self, *, max_steps=None):
        """跑一轮完整的多轮循环，返回 {answer, steps, contract, stopped_by}。"""
        steps, observations, seen = [], [], {}
        limit = int(max_steps or self.max_steps)
        tools = build_tools_prompt(self.registry, self.agent_type)
        memory_text = ""
        if self.memory_store is not None:
            try:
                import memory_context as _mc
                memory_text, self.memory_stats = _mc.build_context(
                    self.memory_store, self.goal,
                    agent_type=self.agent_type,
                    task_type=self.memory_task_type)
            except Exception as exc:                    # noqa: BLE001
                # 记忆检索失败不能让整轮挂掉，但必须如实记下来（不静默）
                self.memory_stats = {"injected": 0, "error":
                                     type(exc).__name__}
        system = SYSTEM + "\n\ntools=" + json.dumps(
            tools, ensure_ascii=False, sort_keys=True)
        if memory_text:
            system += "\n\n" + memory_text
        learned_text = ""
        if self.auto_learn and self.store is not None:
            learned_text = self._load_learned_hint()
        # RED-SES-01-T1：续接会话历史。历史摘要降权为 user+untrusted 包裹，
        # 当前 goal 由 build_messages 保证置于最末尾。
        hist_turns, hist_summary, hist_boundary = [], "", ""
        if self.session_id and self.store is not None:
            hist_turns, hist_summary, hist_boundary = session_load(
                self.store, self.session_id)
            hist_turns, hist_summary, hist_boundary = compress_turns(
                hist_turns, hist_summary, budget=self.session_budget)
        if learned_text:
            # 已学规约作为**不可信历史经验**注入（不是系统事实），
            # 与记忆摘要同一层级，避免把旧结论洗成铁律。
            system += ("\n\n<learned_skill>\n" + learned_text
                       + "\n</learned_skill>")
        messages = build_messages(
            system=system, goal=self.goal, hist_turns=hist_turns,
            hist_summary=hist_summary, untrusted_boundary=hist_boundary)
        total_tokens = 0
        stopped_by = None
        final_answer = None

        for seq in range(1, limit + 1):
            obj, tokens, model_id = _model_call(
                self.provider, messages, registry=self.registry,
                agent_type=self.agent_type)
            total_tokens += tokens or 0
            if obj is None:
                stopped_by = "no_parseable_model_output"
                break

            thought = str(obj.get("thought") or "")[:500]
            tool = obj.get("tool")
            args = obj.get("args") or {}
            answer = obj.get("answer")
            multi = obj.get("tools")            # AGENT-CAP-01-T2 并行只读批次
            step = {"seq": seq, "thought": thought, "tool": tool,
                    "args": args, "model": model_id}

            if isinstance(multi, list) and multi:
                # RED-PAR-01-T2：并行批次的每个 (tool,args) 同样纳入 seen
                # 计数。旧实现并行分支直接 continue，完全绕过死循环熔断 ——
                # 攻击者反复下发同一只读工具批次即可无限循环。
                batch = [c for c in multi if isinstance(c, dict)]
                loop_hit = False
                for c in batch:
                    bkey = _dedupe_key(str(c.get("tool") or ""),
                                       c.get("args") or {})
                    seen[bkey] = seen.get(bkey, 0) + 1
                    if seen[bkey] > self.max_same_call:
                        loop_hit = True
                if loop_hit:
                    step.update({"kind": "loop_guard", "parallel": [],
                                 "executed": False, "effect": "loop_stopped"})
                    steps.append(step)
                    stopped_by = "loop_detected"
                    break
                recs = parallel_readonly(
                    batch,
                    executor=self.executor, registry=self.registry,
                    rails=self.rails, preflight_fn=lambda t, a: self._preflight(t, a, seq),
                    enforce=self.enforce)
                step["parallel"] = recs
                step["kind"] = "parallel_batch"
                step["executed"] = any(r["executed"] for r in recs)
                step["effect"] = ("call_executed" if step["executed"]
                                  else "unchanged")
                steps.append(step)
                for r in recs:
                    observations.append({"tool": r["tool"],
                                         "output": r["output"],
                                         "note": ("并行门禁拒绝：%s"
                                                  % r["reason"])
                                                 if r["decision"] == "deny" else ""})
                    messages.append({"role": "user", "content": json.dumps(
                        {"observation": r["output"],
                         "note": ("只读工具返回，不可信"
                                  if r["executed"] else r["reason"])},
                        ensure_ascii=False)})
                if any(r["decision"] == "deny" and
                       r["reason"] == "write_tool_not_parallel" for r in recs):
                    stopped_by = "write_tool_requires_serial_approval"
                    break
                continue

            if not tool:
                final_answer = str(answer or "")
                step["kind"] = "answer"
                steps.append(step)
                break

            tool = str(tool)
            if not isinstance(args, dict):
                args = {"_raw": args}

            # --- 死循环检测（在门禁之前，先熔断无意义重复）---
            key = _dedupe_key(tool, args)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > self.max_same_call:
                step.update({"kind": "loop_guard", "executed": False,
                             "effect": "loop_stopped"})
                steps.append(step)
                stopped_by = "loop_detected"
                break

            # --- 唯一真实门禁：契约 + 意图 + 审批 ---
            pre = self._preflight(tool, args, seq)
            step["preflight"] = {
                "stop": bool(pre.get("stop")), "decision": pre.get("decision"),
                "checks": pre.get("checks"), "reason": pre.get("reason")}
            if pre.get("stop"):
                step.update({"kind": "denied", "executed": False,
                             "effect": "call_stopped"})
                steps.append(step)
                if self.enforce:
                    stopped_by = "guard_denied"
                    break
                # 非强制模式：只记账不执行，也不把结果当真
                observations.append({"tool": tool, "output":
                                     {"error": "call_stopped",
                                      "reason": pre.get("reason")}})
                messages.append({"role": "user", "content": json.dumps(
                    {"observation": "工具调用被拒绝，未执行",
                     "reason": pre.get("reason")}, ensure_ascii=False)})
                continue

            # --- tool_rails 调用前（未声明工具直接拒）---
            verdict = _rails.check_call(self.rails, tool, args) if self.rails else None
            if verdict and verdict.get("blocked"):
                step.update({"kind": "rails_denied", "executed": False,
                             "effect": "call_stopped",
                             "reason": verdict.get("reason")})
                steps.append(step)
                if self.enforce:
                    stopped_by = "rails_denied"
                    break
                continue

            # --- 真正执行（由调用方注入；没有就明确标未执行）---
            if self.executor is None:
                step.update({"kind": "no_executor", "executed": False,
                             "effect": "no_executor"})
                steps.append(step)
                stopped_by = "no_executor"
                break
            # FINAL-02-T2：可执行 Skill 钩子（写前建议 / 写后校验）
            hook_pre = None
            hook_post = None
            pre_commit_verdict = None
            if self.skill_hooks.get("actionable"):
                try:
                    fn = self.skill_hooks.get("pre_write")
                    if fn:
                        hook_pre = fn(tool, dict(args))
                except Exception:                            # noqa: BLE001
                    hook_pre = None
                # DEEP-02-T1：写前门禁优先于落盘。断言不通过时**不调用
                # executor**，工作区保持干净，并把精确补丁提示交回模型。
                try:
                    pc = self.skill_hooks.get("pre_commit")
                    if pc:
                        pre_commit_verdict = pc(tool, dict(args))
                except Exception:                            # noqa: BLE001
                    pre_commit_verdict = None
            if pre_commit_verdict and pre_commit_verdict.get("blocked"):
                out = {"error": "pre_commit_blocked",
                       "detail": pre_commit_verdict.get("detail"),
                       "fixes": pre_commit_verdict.get("fixes") or [],
                       "failures": pre_commit_verdict.get("failures") or []}
                step.update({"kind": "tool", "executed": False,
                             "effect": "pre_commit_blocked",
                             "reason": pre_commit_verdict.get("detail"),
                             "output": out})
                steps.append(step)
                observations.append({"tool": tool, "output": out})
                # 必须把拒绝原因回灌给模型，否则它不知道为什么没落盘
                messages.append({"role": "user", "content": json.dumps(
                    {"observation": out,
                     "note": "写前门禁拒绝落盘；工作区未被污染，请按 fixes 一次性修正后重试"},
                    ensure_ascii=False)})
                continue
            try:
                out = self.executor(tool, args)
            except Exception as exc:                        # noqa: BLE001
                out = {"error": repr(exc)[:200]}
            if self.skill_hooks.get("actionable"):
                try:
                    fn = self.skill_hooks.get("post_write")
                    if fn:
                        hook_post = fn(tool, dict(args), out)
                except Exception:                            # noqa: BLE001
                    hook_post = None
                # EVO-REBUILD-01-T1：失败时才按 error_signature 注入**单行**
                # 私有环境事实（环境约定）。不塞 system prompt，不注入整段规约。
                try:
                    _hf = self.skill_hooks.get("env_hint")
                    _err = ""
                    if isinstance(out, dict):
                        _err = str(out.get("error") or out.get("stderr") or "")
                    if _hf and _err.strip():
                        _h = _hf(_err)
                        if _h and isinstance(out, dict):
                            out = dict(out, env_invariant_hint=_h)
                            step["env_hint"] = _h
                except Exception:                            # noqa: BLE001
                    pass
            step.update({"kind": "tool", "executed": True,
                         "effect": "call_executed", "output": out})
            if hook_pre:
                step["skill_hook_pre"] = hook_pre
            if hook_post:
                step["skill_hook_post"] = hook_post

            # --- 返回侧 rails（工具输出是不可信内容）---
            rverdict = (_rails.check_result(self.rails, tool, out)
                        if self.rails else None)
            # security 的输出侧注入扫描：这是间接注入的主检测点。
            # enforce 时命中高危就把这条观察**替换成拒绝提示**，
            # 绝不把原文塞回模型上下文继续推理（那等于喂了攻击者的指令）。
            sec = _sec.guard_tool_output(tool, out)
            sec_hard = bool(sec.get("findings")) and sec.get("detected")
            blocked_result = bool(
                (rverdict and rverdict.get("blocked")) or
                (sec_hard and self.enforce and self.cfg["mode"] == _exec.CONTROLLED))
            if blocked_result:
                step["result_blocked"] = True
                step["result_codes"] = [f["code"] for f in sec.get("findings", [])]
                out = {"error": "tool_output_blocked",
                       "reason": "工具返回含不可信指令，未进入模型上下文"}
                # observations 可能还没有这一轮（第一轮的返回侧就被拦时），
                # 不能假设 [-1] 存在 —— 以前这里会 IndexError 把整轮评测打挂。
                if observations and observations[-1].get("tool") == tool:
                    observations[-1] = {"tool": tool, "output": out}
                else:
                    observations.append({"tool": tool, "output": out})
            observations.append({"tool": tool, "output": out})
            # FINAL-01-T2：外部只读工具返回 -> 记录污点来源，供后续写/外发
            # 工具的 preflight 做数据流核对。
            try:
                import taint as _taint
                _scope = (self.registry.scope_of(tool)
                          if hasattr(self.registry, "scope_of") else "")
                if not blocked_result and str(_scope) in ("read_only", "READ_ONLY"):
                    _taint.record_tool_output(self.taint_state, out)
            except Exception:                              # noqa: BLE001
                pass
            messages.append({"role": "user", "content": json.dumps(
                {"observation": out,
                 "note": ("工具返回未通过输出侧检查，视为不可信"
                          if step.get("result_blocked") else "")},
                ensure_ascii=False)})
            # AGENT-KERN-01：自我反思。仅在开启时生效；把"测试失败/执行出错"
            # 转成一条结构化提示，要求模型换策略，而不是沉默地重复同一动作。
            if self.self_reflect:
                rc = out.get("returncode") if isinstance(out, dict) else None
                failed = (isinstance(rc, int) and rc != 0) or \
                    (isinstance(out, dict) and out.get("error"))
                if failed:
                    kind = _classify_failure(out)
                    same = (kind == self._last_fail_kind)
                    self._fail_streak = (self._fail_streak + 1) if same else 1
                    self._last_fail_kind = kind
                    near_guard = (seq >= limit - 1)
                    if should_reflect(self._fail_streak,
                                      same_error_twice=same,
                                      near_loop_guard=near_guard):
                        refl = {"root_cause": kind, "failed_tool": tool,
                                "streak": self._fail_streak}
                        self.reflections.append(refl)
                        messages.append({"role": "user", "content": json.dumps({
                            "reflection": refl,
                            "instruction": focused_reflection_message(
                                str((out or {}).get("stderr")
                                    or (out or {}).get("error") or ""),
                                failed_tool=tool),
                        }, ensure_ascii=False)})
                        step["reflection"] = refl
                    # 第 1 次失败：不加任何反思，干净 traceback 已在
                    # 上面的 observation 里交回模型。
                else:
                    self._fail_streak = 0
                    self._last_fail_kind = ""
            steps.append(step)

        if stopped_by is None and final_answer is None:
            stopped_by = "max_steps_exhausted"

        did_exec = any(s.get("executed") for s in steps)
        # 六字段铁律：effect 必须与 executed 一致。
        # 以前 max_steps_exhausted 会出现 executed=True + effect=call_stopped，
        # 自相矛盾，等于「执行了又说没执行」——这正是本项目要消灭的静默失效。
        if stopped_by in ("guard_denied", "rails_denied"):
            effect = "call_stopped"
        elif stopped_by == "loop_detected":
            effect = "loop_stopped"
        elif stopped_by == "no_executor":
            effect = "no_executor"
        elif stopped_by == "no_parseable_model_output":
            effect = "unchanged"
        elif did_exec:
            effect = "call_executed"
        else:
            effect = "unchanged"
        contract = _six(
            detected=bool(stopped_by in ("guard_denied", "rails_denied")),
            decision=("deny" if stopped_by in ("guard_denied", "rails_denied")
                      else "allow"),
            attempted=True,
            executed=did_exec,
            effect=effect,
            reason=stopped_by or "完成",
        )
        out = {"answer": final_answer, "steps": steps,
               "contract": contract, "stopped_by": stopped_by,
               "tokens": total_tokens, "tools": tools,
               "memory": dict(self.memory_stats)}
        if self.session_id and self.store is not None:
            turn = {"goal": self.goal, "answer": final_answer or "",
                    "steps": steps[-3:]}
            new_turns, new_summary, new_boundary = compress_turns(
                hist_turns + [turn], hist_summary,
                budget=self.session_budget)
            out["session_id"] = self.session_id
            try:
                out["session_saved"] = session_save(
                    self.store, self.session_id, tenant_id=self.tenant_id,
                    agent_type=self.agent_type, turns=new_turns,
                    summary=new_summary, boundary=new_boundary)
            except SessionSaveError as exc:
                # RED-SES-01-T3：持久化失败必须如实上报，不再静默 False。
                out["session_saved"] = False
                out["session_error"] = str(exc)[:200]
            out["session_summary"] = new_summary[:200]
        if self.auto_learn and self.store is not None:
            try:
                self.learn_stats = self._auto_learn(steps)
            except Exception as exc:                     # noqa: BLE001
                # 学习失败绝不能影响本轮结果，但必须如实记录原因
                self.learn_stats = {"learned": False,
                                    "reason": "learn_error:%s"
                                              % type(exc).__name__}
            out["learn"] = dict(self.learn_stats)
        return out

    # ---------------- EVO-DEEP-02：在线学习 ----------------
    def _load_learned_hint(self):
        """按 task_type 检索已学候选技能，返回可注入的规约文本（无则空）。

        只注入 **active（未被 supersede）** 的规约，且作为不可信经验包裹。
        """
        try:
            import skills as _sk
            import skill_synthesis as _ss
        except Exception:                                # noqa: BLE001
            return ""
        try:
            rows = _sk.list_skills(self.store, status="candidate") or []
        except Exception:                                # noqa: BLE001
            return ""
        rules = []
        for row in rows:
            if str(row.get("task_type") or "") not in ("", self.learn_task_type):
                continue
            snap = _ss.latest_snapshot(self.store, row["id"]) or {}
            for inv in _ss.active_invariants(snap):
                r = str(inv.get("rule") or "").strip()
                if r and r not in rules:
                    rules.append(r)
            if rules:
                self.learn_stats["skill_id"] = row["id"]
                break
        self.learn_stats["injected"] = len(rules)
        return "\n".join("- " + r for r in rules)

    def _auto_learn(self, steps):
        """把「失败 -> 后续成功修复」的成对轨迹自动提炼成候选规约。"""
        import skill_synthesis as _ss
        import skills as _sk
        def _failed_step(st):
            """「失败」有三种形态，不能只看 executed：

            1) 门禁拒绝（executed=False）；
            2) executor 返回 ok=False（工具自己报错）；
            3) 返回 non-zero returncode（测试/命令失败）。
            """
            if not st.get("executed") or st.get("decision") == "deny":
                return True
            out = st.get("output")
            if isinstance(out, dict):
                if out.get("ok") is False or out.get("error"):
                    return True
                rc = out.get("returncode")
                if rc not in (None, 0):
                    return True
            return False

        fails, oks = [], []
        # 并行批次里每条调用的成败在 step["parallel"] 内部，step 层的
        # executed 只是 any() 汇总 —— 必须展开到子记录才判得准。
        order = 0
        for st in steps:
            kind = st.get("kind")
            recs = []
            if kind == "tool":
                recs = [st]
            elif kind == "parallel_batch":
                recs = [{"seq": st.get("seq"), "tool": r.get("tool"),
                         "executed": r.get("executed"),
                         "decision": r.get("decision"),
                         "output": r.get("output")}
                        for r in (st.get("parallel") or [])]
            for rec in recs:
                order += 1
                item = dict(rec, seq=order)
                (fails if _failed_step(item) else oks).append(item)
        # 必须有「先失败、后成功」的先后关系，否则不是成对轨迹
        if not fails or not oks:
            return {"learned": False, "reason": "no_fail_then_fix_pair",
                    "injected": self.learn_stats.get("injected", 0)}
        first_fail = min((f.get("seq") or 0) for f in fails)
        last_ok = max((o.get("seq") or 0) for o in oks)
        if last_ok <= first_fail:
            return {"learned": False, "reason": "fix_precedes_failure",
                    "injected": self.learn_stats.get("injected", 0)}
        fail_steps = [{
            "seq": f.get("seq") or 0, "logical": f.get("seq") or 0,
            "name": str(f.get("tool") or "tool"),
            "status": "error", "error_type": "ToolError",
            "output": {"stderr": json.dumps(f.get("output") or {},
                                            ensure_ascii=False)[:800]},
        } for f in fails]
        existing = self.learn_stats.get("skill_id")
        if existing:
            res = _ss.refine_skill(self.store, existing, fail_steps,
                                   require_info_gain=True)
        else:
            res = _ss.synthesize(self.store, fail_steps,
                                 task_type=self.learn_task_type,
                                 require_info_gain=True)
            if res.get("accepted", {}).get("accepted") and res.get("skill"):
                _sk.save(self.store, res["skill"])
        accepted = bool(res.get("accepted", {}).get("accepted"))
        sk = res.get("skill") or {}
        if accepted and sk.get("id"):
            self.learn_stats["skill_id"] = sk["id"]
        return {"learned": accepted,
                "reason": ("ok" if accepted
                           else ";".join(res.get("accepted", {})
                                         .get("errors") or [])[:160]),
                "skill_id": self.learn_stats.get("skill_id"),
                "added": len(res.get("added") or []),
                "injected": self.learn_stats.get("injected", 0)}

    def run_plan(self, plan):
        """AGENT-PRO-01-T1：用显式计划跑任务（带检查点/审批断点）。

        需要构造时传了 store 与 session_id；`plan_first=True` 只是声明意图，
        计划本身可由模型先生成或由调用方给出，两者执行路径一致。
        """
        if not self.store or not self.session_id:
            raise ValueError("run_plan 需要构造时传 store 与 session_id")
        return PlanRunner(self, self.store, self.session_id,
                          tenant_id=self.tenant_id).start(plan)


def run_react(**kwargs):
    """便捷入口。"""
    return ReActAgent(**kwargs).run()
