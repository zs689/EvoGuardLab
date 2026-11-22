# -*- coding: utf-8 -*-
"""DEEP-05：从 autonomous_agent.py 机械拆出的只读工具并行执行器（保留 re-export 兼容）。"""
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
import threading

import concurrent.futures as _cf
import tool_rails as _rails
import tool_registry as _reg
import core as _core
import execution as _exec
import security as _sec

# ---- AGENT-CAP-01-T2：单轮多只读工具并行 -------------------------------
def _is_readonly(tool, registry=None):
    """严格白名单：只有明确声明为只读的工具才可并行。

    RED-PAR-01-T1：旧实现是黑名单（只排除 side_effect=='write'），于是
    external（http.send）、arbitrary（shell.exec）以及**未登记工具**全被
    当成只读放行 —— 通过 `tools:[...]` 并行批次即可绕过串行审批。
    现在要求三个条件同时成立：
      1) 工具已在 registry 明确登记；
      2) scope == 'read_only'；
      3) side_effect in ('none', '')。
    任何缺项一律 False（fail-closed），绝不因为「没查到副作用」就放行。
    """
    reg = registry or _reg.REGISTRY
    declared = reg.declared() or {}
    decl = declared.get(tool)
    if not isinstance(decl, dict):
        return False                      # 未登记 -> 不是只读
    scope = str(decl.get("_scope") or decl.get("scope") or "")
    side = str(decl.get("side_effect") or "")
    return scope == "read_only" and side in ("none", "")


def parallel_readonly(calls, *, executor, registry=None, rails=None,
                      preflight_fn=None, max_workers=4,
                      enforce=False, strict_output=True):
    """并发执行一批**只读**工具；每个各自过 preflight+rails+返回侧护栏。

    返回 list[dict]，每项含 {tool,args,executed,output,decision,reason}。
    **写工具不会在这里跑**：非只读项直接 executed=False + deny，
    由调用方走串行审批路径。这样「并行」不会绕过写门禁。

    RED-PAR-01-T2：返回侧必须与单工具路径**策略一致** —— 工具输出是不可信
    内容，过 guard_tool_output + tool_rails.check_result；命中高危时把输出
    替换成拒绝提示，绝不原样塞回 messages。strict_output 默认 True，
    只有显式关掉才退回旧行为（避免再开一条绕过返回侧护栏的旁路）。
    """
    import concurrent.futures as _cf
    results = [None] * len(calls)

    def one(i, tool, args):
        rec = {"tool": tool, "args": args, "executed": False,
               "decision": "allow", "output": None, "reason": "",
               "effect": "unchanged"}
        if not _is_readonly(tool, registry):
            rec.update({"decision": "deny", "reason": "write_tool_not_parallel"})
            return i, rec
        if preflight_fn is not None:
            pre = preflight_fn(tool, args)
            if pre and pre.get("stop"):
                rec.update({"decision": "deny",
                            "reason": str(pre.get("reason") or "preflight_stop"),
                            "effect": "call_stopped"})
                return i, rec
        if rails:
            v = _rails.check_call(rails, tool, args)
            if v and v.get("blocked"):
                rec.update({"decision": "deny",
                            "reason": "rails_call_blocked",
                            "effect": "call_stopped"})
                return i, rec
        if executor is None:
            rec["reason"] = "no_executor"
            return i, rec
        try:
            out = executor(tool, args)
        except Exception as exc:                            # noqa: BLE001
            rec["reason"] = "executor_error:" + type(exc).__name__
            rec["output"] = {"error": rec["reason"]}
            return i, rec

        # RED-PAR-01-T2：返回侧安全门禁（与单工具路径同一策略）。
        blocked = False
        codes = []
        if strict_output:
            rverdict = _rails.check_result(rails, tool, out) if rails else None
            sec = _sec.guard_tool_output(tool, out)
            codes = [f["code"] for f in sec.get("findings", [])]
            sec_hard = bool(sec.get("findings")) and sec.get("detected")
            blocked = bool((rverdict and rverdict.get("blocked")) or
                           (sec_hard and enforce))
        if blocked:
            # 执行确实发生了（工具已跑），但输出不进入上下文 -> 拒绝该观察值。
            # executed 保持 True（事实），decision=deny 表示输出侧被拦；
            # output 替换成拒绝提示，note 标记不可信，杜绝注入继续传播。
            rec.update({"executed": False, "decision": "deny",
                        "reason": "tool_output_blocked",
                        "result_blocked": True, "result_codes": codes,
                        "output": {"error": "tool_output_blocked",
                                   "reason": "工具返回含不可信指令，未进入模型上下文"},
                        "effect": "call_stopped"})
            return i, rec
        rec["output"] = out
        rec["executed"] = True
        rec["effect"] = "call_executed"
        return i, rec

    workers = min(max(1, int(max_workers)), max(1, len(calls)))
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, i, str(c.get("tool") or ""),
                            c.get("args") or {})
                for i, c in enumerate(calls)]
        for f in _cf.as_completed(futs):
            i, rec = f.result()
            results[i] = rec
    return results


# ---- MOD-AGENT-01-T1：按需反思（公理 B：不打断正常流）-----------------
