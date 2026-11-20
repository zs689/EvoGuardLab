# -*- coding: utf-8 -*-
"""统一执行入口：所有真实工具调用必须先过这里。

为什么需要它（评审 R01）：
`runtime.Ctx.call` 原来依次做「工具白名单 → guard_tool_args → sim.validate_args
→ 缓存 → 可选 ApprovalGate → sim.call_tool」。`tool_contract.authorize` 和
`security.guard_tool_intent` 都写好了、也都有测试，但**真实调用路径一次都没调**。
把两个门禁换成恒定 deny，工具照样执行一次 —— 这就是「模块通过 ≠ 真实生效」。

这里把「调用前必须成立的判断」收成一个 preflight：
    契约（结构授权） → 意图（语义一致） → 审批（一次性票据） → 才允许执行

三条不等价的东西被刻意分开，不许互相顶替：
  - 契约 deny：参数/身份/资源越界，这是结构问题，模型说什么都不改变结论；
  - 意图 deny：这次调用是否服务于用户最初的目标，只有模型能判，允许它不确定；
  - 审批未满足：动作合规但需要人点头，属流程问题。
任何一层说「不行」，执行层就真的不执行 —— 不是写条日志了事。

受控配置与历史实验配置分开：
  - legacy（默认）：保持既有实验行为，缺审批后端也照跑，报告里如实标注；
  - controlled：写副作用工具没有审批后端 / 必要检查器缺失 / 身份未知 → 拒绝启动。
"""
import hashlib
import json
import os

CONFIG_VERSION = "exec-config-v1"

LEGACY = "legacy"
CONTROLLED = "controlled"

ALLOW, DENY, REVIEW, NONE = "allow", "deny", "review", "none"
STOP = (DENY, REVIEW)

# 每种失败的默认处置。controlled 下 review/error 一律停（不许静默放行）。
DEFAULT_ON_MISSING_GOAL = REVIEW
DEFAULT_ON_JUDGE_UNAVAILABLE = REVIEW


class ExecutionConfigError(RuntimeError):
    """受控配置不成立。必须在启动时就炸，不能等到调用时才拦一半。"""


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "on", "yes")


def build_config(env=None, *, agent_type=None, allowed_tools=None,
                 write_tools=(), has_approval_backend=False,
                 has_contract=True, has_intent_judge=True):
    """生成**生效配置**并校验。返回的 dict 就是启动时应当导出的那份。"""
    env = os.environ if env is None else env
    raw = (env.get("AGENTLAB_EXEC_MODE") or LEGACY).strip().lower()
    if raw in ("controlled", "enforce", "strict"):
        mode = CONTROLLED
    elif raw in ("legacy", "off", "audit", ""):
        mode = LEGACY
    else:
        # 写错不是「回退到默认」，是配置错误：否则打错一个字就悄悄降级成不防护。
        raise ExecutionConfigError("未知 AGENTLAB_EXEC_MODE=%r（可选 legacy / controlled）"
                                   % raw)

    tools = sorted(allowed_tools or [])
    writes = sorted(set(tools) & set(write_tools or ()))
    cfg = {
        "version": CONFIG_VERSION,
        "mode": mode,
        "agent_type": agent_type,
        "identity_known": bool(agent_type) and bool(tools),
        "allowed_tools": tools,
        "write_tools_in_scope": writes,
        "contract": {"enabled": bool(has_contract),
                     "mode": "enforce" if mode == CONTROLLED else "audit"},
        "intent": {"enabled": bool(has_intent_judge),
                   "on_missing_goal": DEFAULT_ON_MISSING_GOAL,
                   "on_unavailable": DEFAULT_ON_JUDGE_UNAVAILABLE},
        "approval": {"backend": bool(has_approval_backend),
                     "required_for_write": mode == CONTROLLED},
        "on_review": "stop",
        "gaps": [],
    }

    # 受控模式的前提检查：缺一项就拒绝启动，而不是运行到一半才失败。
    gaps = cfg["gaps"]
    if not cfg["identity_known"]:
        gaps.append("身份未知：agent_type=%r 没有可用工具白名单" % agent_type)
    if writes and not cfg["approval"]["backend"]:
        gaps.append("写副作用工具 %s 在作用域内，但没有审批后端" % "、".join(writes))
    if not cfg["contract"]["enabled"]:
        gaps.append("结构契约检查器不可用")
    if mode == CONTROLLED and gaps:
        raise ExecutionConfigError(
            "受控执行配置不成立，拒绝启动：\n  - " + "\n  - ".join(gaps)
            + "\n（若确实要保持历史实验行为，显式设 AGENTLAB_EXEC_MODE=legacy）")
    return cfg


def call_id(run_id, seq, tool, args):
    """稳定 call_id：同一次调用的轨迹与审计能对上，跨重启也可复算。"""
    body = json.dumps({"run": run_id, "seq": seq, "tool": tool, "args": args},
                      sort_keys=True, ensure_ascii=False, default=str)
    return "call_" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _record(result, decision, reason, checks):
    result.update({"decision": decision, "reason": reason,
                   "checks": checks, "attempted": True, "executed": False})
    return result


def preflight(cfg, *, agent_type, tool, args, user_goal=None, cid=None,
              contracts=None, policy=None, intent_fn=None,
              contract_fn=None, taint_state=None):
    """调用前的全部判断。返回 dict；`stop` 为真表示**不许执行工具**。

    导入与调用都做成可注入的：测试里可以把任意一层替成恒定 deny，
    从而证明「真 deny 时工具调用次数为 0」——这是本模块存在的意义。
    """
    checks = []
    out = {"call_id": cid, "tool": tool, "stop": False, "decision": ALLOW,           "reason": "全部前置检查通过", "checks": checks,
           "attempted": True, "executed": False,
           "config_mode": cfg["mode"], "contract": None, "intent": None}

    # ---- 1) 结构契约 ----
    if cfg["contract"]["enabled"]:
        fn = contract_fn or _default_contract_fn()
        if fn is None:
            c = {"checker": "contract", "status": "unavailable"}
            checks.append(c)
            if cfg["mode"] == CONTROLLED:
                return _stop(out, REVIEW, "结构契约检查器不可用，转人工复核", checks)
        else:
            # 没传 policy 时用「本 Agent 被允许的工具」当白名单。
            # 不能留空：空白名单会让每一次契约检查都判越权（fail-closed 到不可用）。
            pol = policy or {"allowed_tools": cfg.get("allowed_tools") or [],
                             "write_tools": cfg.get("write_tools_in_scope") or []}
            r = fn(agent_type, tool, args, mode=cfg["contract"]["mode"],
                   contracts=contracts, policy=pol)
            out["contract"] = {"decision": r.get("decision"),
                               "reasons": r.get("reasons", []),
                               "version": r.get("version")}
            # 归一化后的对象往下传：审批摘要与真实执行必须消费同一个东西，
            # 否则又会出现「校验 amount、执行 total」那种错位（评审 R02）。
            if isinstance(r.get("verified_args"), dict):
                out["verified_args"] = r["verified_args"]
                out["aliases_folded"] = bool(r.get("aliases_folded"))
            checks.append({"checker": "contract", "status": r.get("decision")})
            if r.get("decision") == "deny":
                return _stop(out, DENY,
                             "结构契约拒绝：%s" % "；".join(r.get("reasons") or [])[:200],
                             checks)

    # ---- 1.5) 按工具声明的双向护栏（调用侧）----
    # 融合 NeMo 的 `rails.tool_call.per_tool`：**未声明的工具在跑任何检查之前就挡**，
    # 配置缺项在加载时炸（tool_rails.load 会抛），不是运行时才拦一半。
    try:
        import tool_rails
        rules = tool_rails.load_default()
    except Exception as e:                        # noqa: BLE001
        checks.append({"checker": "tool_rails", "status": "config_error",
                       "error": "%s: %s" % (type(e).__name__, e)})
        if cfg["mode"] == CONTROLLED:
            return _stop(out, DENY, "工具护栏配置不成立，拒绝执行：%s" % e, checks)
        rules = None
    if rules is not None:
        v = tool_rails.check_call(rules, tool, args)
        out["rails_call"] = {"status": v["status"], "ran": v["ran"],
                             "blocked": v["blocked"]}
        checks.append({"checker": "tool_rails.call", "status": v["status"],
                       "ran": v["ran"]})
        if v["status"] == tool_rails.ERROR and cfg["mode"] == CONTROLLED:
            # 检查器坏了 ≠ 检查通过
            return _stop(out, REVIEW, v["reason"], checks)
        if v["blocked"]:
            return _stop(out, DENY, v["reason"], checks)

    # ---- 2) 语义意图 ----
    if cfg["intent"]["enabled"]:
        fn = intent_fn or _default_intent_fn()
        if fn is None:
            checks.append({"checker": "intent", "status": "unavailable"})
            if cfg["mode"] == CONTROLLED:
                return _stop(out, cfg["intent"]["on_unavailable"],
                             "意图判定器不可用，按配置转复核", checks)
        else:
            from security import intent_enabled
            if not intent_enabled():                checks.append({"checker": "intent", "status": "disabled"})
            elif not (user_goal or "").strip():
                checks.append({"checker": "intent", "status": "no_goal"})
                if cfg["mode"] == CONTROLLED:
                    return _stop(out, cfg["intent"]["on_missing_goal"],
                                 "受控模式缺可信用户目标，不猜、转人工复核", checks)
            else:
                try:
                    r = fn(agent_type, tool, args, user_goal)
                except Exception as e:            # noqa: BLE001
                    # 判定器自己炸了，不等于这次调用合规。受控档转复核，不静默放行。
                    checks.append({"checker": "intent", "status": "error",
                                   "error": "%s: %s" % (type(e).__name__, e)})
                    out["intent"] = {"decision": "review",
                                     "error": "%s: %s" % (type(e).__name__, e)}
                    if cfg["mode"] == CONTROLLED:
                        return _stop(out, cfg["intent"]["on_unavailable"],
                                     "意图判定异常（%s），转人工复核"
                                     % type(e).__name__, checks)
                    return out
                dec = r.get("decision") or NONE
                out["intent"] = {"decision": dec,
                                 "covered": r.get("covered_fields"),
                                 "omitted": r.get("omitted_fields")}
                checks.append({"checker": "intent", "status": dec,
                               "omitted": r.get("omitted_fields") or []})
                if dec in STOP:
                    return _stop(out, dec, r.get("reason") or "意图判定未通过", checks)
                if dec == NONE and cfg["mode"] == CONTROLLED:
                    # 门禁开着却给了 none —— 只有「不给结论」这一种情况，不得当 allow
                    return _stop(out, cfg["intent"]["on_unavailable"],
                                 "意图判定没有结论（%s），不静默放行"
                                 % (r.get("reason") or "")[:120], checks)

    # ---- FINAL-01-T2) 污点追踪：受污染参数流入写/外发工具 ----
    # 信息流隔离：即使攻击文本不含关键词绕过 L1，只要写工具的目标/收款人
    # 源自外部工具输出且不在原始可信 user_goal 里，也必须拦。
    if taint_state is not None:
        try:
            import taint as _taint
            # TAINT-FIX-01-T1：把该工具的契约传下去，让副作用判定走
            # side_effect/scope 动态读取，而不是硬编码工具名。
            _c = (contracts or {}).get(tool) if contracts else None
            hit = _taint.tainted_argument(tool, args, taint_state,
                                          contract=_c)
            checks.append({"checker": "taint", "status":
                           "hit" if hit else "clean"})
            out["taint"] = {"hit": hit}
            if hit and cfg["mode"] == CONTROLLED:
                return _stop(out, DENY,
                             "tainted_argument_to_side_effect："
                             "污点参数流入副作用工具 %s=%s"
                             % (hit["field"], hit["value"]), checks)
        except ImportError:
            pass
    return out


def _stop(out, decision, reason, checks):
    out.update({"stop": True, "decision": decision, "reason": reason,
                "checks": checks, "attempted": True, "executed": False})
    return out


def _default_contract_fn():
    try:
        import tool_contract
    except Exception:                     # noqa: BLE001
        return None
    return tool_contract.authorize


def _default_intent_fn():
    try:
        import security
    except Exception:                     # noqa: BLE001
        return None
    return lambda a, t, args, goal: security.guard_tool_intent(a, t, args, goal)


def export(cfg):
    """启动时导出的生效配置（不含任何凭据）。"""
    return {k: cfg[k] for k in (
        "version", "mode", "agent_type", "identity_known", "allowed_tools",
        "write_tools_in_scope", "contract", "intent", "approval",
        "on_review", "gaps")}
