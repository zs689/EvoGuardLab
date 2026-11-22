# -*- coding: utf-8 -*-
"""DEEP-05：反思触发与语义反思（从 autonomous_agent.py 拆出）。"""
# DEEP-05 通用兜底 imports（机械拆分后原模块级依赖统一补齐）
import hashlib
import json
import os
import re
import time
import traceback
import uuid
import concurrent.futures as _cf

import json
import os
import time

import gateway as _gw
import core as _core
import security as _sec
from model_call import _model_call

def _dedupe_key(tool, args):
    return hashlib.sha256(json.dumps(
        [tool, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _six(detected=False, decision="allow", attempted=False, executed=False,
         effect="unchanged", reason=""):
    """六字段契约：executed 为 True 才算真的执行。"""
    return {"detected": bool(detected), "decision": decision,
            "attempted": bool(attempted), "executed": bool(executed),
            "effect": effect, "reason": reason}


# ---- AGENT-CAP-01-T1：多轮会话持久化 + 上下文预算压缩 -------------------
def should_reflect(fail_count, *, same_error_twice=False,
                   near_loop_guard=False):
    """仅当卡壳才触发反思：连续 2 次同错，或即将触发 loop_guard。

    第 1 次失败直接把干净 traceback 交回模型即可 —— 实测无条件插长篇
    反思模板在 DeepSeek 上 -4.35pp（linear 65.22% vs reflect 60.87%）。
    """
    if near_loop_guard:
        return True
    return int(fail_count) >= 2 and bool(same_error_twice)


def focused_reflection_message(error_text, *, failed_tool=None,
                               diff_hint=None):
    """极简定向诊断：差异 + 上次报错，不塞长篇模板。"""
    err = str(error_text or "").strip().replace("\n", " ")[:140]
    parts = ["上一步失败：", err]
    if failed_tool:
        parts.append("工具=%s" % str(failed_tool)[:40])
    if diff_hint:
        parts.append("差异=%s" % str(diff_hint)[:60])
    parts.append("换一种策略（改参数/换工具/回滚后重做），不要重复同一动作。")
    return " ".join(parts)[:200]



def _plan_step_needs_approval(step, registry):
    """计划步骤是否写/外发操作（需审批、串行）。"""
    decl = (registry.declared() or {}).get(str(step.get("tool") or "")) or {}
    side = str(decl.get("side_effect") or step.get("side_effect") or "")
    scope = str(decl.get("_scope") or decl.get("scope") or step.get("scope") or "")
    return side in ("write", "external", "arbitrary") or scope in ("write", "external")


def semantic_reflect(failure_reason, *, provider=None, remaining=None,
                     tool=None, args=None, max_replan=2):
    """SCORE-03-T1：规则快速分类 + 模型语义归因。

    模型无关：provider 可为任意实现 `complete(messages, **kw) -> (text,_,_)`
    的后端；没有 provider 时保留确定性规则回退。输出经结构校验，
    不会把模型的非字符串/空步骤塞进计划。

    返回 {root_cause, bad_assumption, revised_steps, model_backed, ...}
    """
    reason = str(failure_reason or "")
    rc, strategy = "unknown", "retry_same"
    if "tool_output_blocked" in reason or "injection" in reason.lower():
        rc, strategy = "untrusted_output_injection", "isolate_output"
    elif "rails" in reason or "deny" in reason or "guard" in reason:
        rc, strategy = "policy_denied", "adjust_args_or_abort"
    elif "returncode" in reason or "assert" in reason or "fail" in reason:
        rc, strategy = "test_regression", "rollback_and_rework"
    elif "timeout" in reason or "no_executor" in reason:
        rc, strategy = "exec_failure", "replan_alternate_tool"
    elif "no_parseable" in reason or "E_LLM" in reason:
        rc, strategy = "model_output_invalid", "simplify_prompt"
    fallback = {"root_cause": rc, "bad_assumption": "",
                # 规则回退也给出可执行的一步，不能只报一个根因标签
                "revised_steps": [strategy], "next_strategy": strategy,
                "model_backed": False, "reason": reason[:200]}
    if provider is None:
        return fallback
    prompt = (
        "你是 planner。分析步骤失败并给出修正计划。只输出 JSON："
        '{"root_cause":str,"bad_assumption":str,'
        '"revised_steps":[str,...]}。\n'
        "失败原因：" + reason[:1200] + "\n"
        "工具：" + str(tool or "")[:80] + "\n"
        "参数：" + str(args or "")[:400] + "\n"
        "剩余计划：" + json.dumps(remaining or [], ensure_ascii=False)[:600])
    try:
        text, _tok, _mid = provider.complete(
            [{"role": "planner", "content": prompt}],
            temperature=0.0, max_tokens=400)
        obj = _gw.extract_json(text)
        if not isinstance(obj, dict):
            return fallback
        steps = obj.get("revised_steps")
        if not isinstance(steps, list):
            steps = []
        steps = [str(x).strip() for x in steps
                 if isinstance(x, str) and x.strip()][:max(1, max_replan * 4)]
        out = dict(fallback)
        out.update({
            "root_cause": str(obj.get("root_cause") or rc)[:200],
            "bad_assumption": str(obj.get("bad_assumption") or "")[:300],
            "revised_steps": steps,
            "model_backed": True,
        })
        return out
    except Exception:                                      # noqa: BLE001
        return fallback


# ZERO-07：PlanRunner 拆到 plan_runner.py；re-export 保持兼容。
from plan_runner import PlanRunner  # noqa: E402

