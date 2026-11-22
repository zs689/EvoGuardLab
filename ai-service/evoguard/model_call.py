# -*- coding: utf-8 -*-
"""DEEP-05：从 autonomous_agent.py 机械拆出协议自适应模型调用；保留显式 re-export。"""
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
import time
import gateway as _gw
import tool_contract as _tc
import hashlib
import traceback

from tool_naming import (_tools_spec, _normalize_tool_name,
                         tool_name_from_model)
from agent_utils import _detect_repetition

def _model_call(provider, messages, max_tokens=1024, *,
                registry=None, agent_type=None, enable_tools=True):
    """AGENT-PLUG-01-T2：协议自适应的模型调用。

    1) 模型声明 supports_tools 且给了 registry -> 走原生 `tools`/`tool_calls`；
    2) 否则退化为 prompt-JSON（旧行为，兼容弱模型/本地模型）；
    3) 文本格式坏掉时发**1 次**轻量纠错提示（不直接崩溃）；
    4) 复读死循环 -> 返回 None（上层记 no_parseable_model_output）。
    """
    use_native = bool(enable_tools and registry is not None
                      and getattr(provider, "supports_tools", False))
    tools = _tools_spec(registry, agent_type) if use_native else None
    try:
        reply = provider.complete_ex(messages, tools=tools,
                                     temperature=0.0, max_tokens=max_tokens)
        text, tokens, model_id = reply.as_tuple()
        calls = reply.tool_calls
    except AttributeError:
        text, tokens, model_id = provider.complete(
            messages, temperature=0.0, max_tokens=max_tokens)
        calls = []

    if _detect_repetition(text):
        return None, tokens, model_id          # 复读熔断

    # 原生 tool_calls：转成下游统一的 {tool,args} 结构
    if calls:
        first = calls[0]
        call_args = first.get("args") or {}
        # 有些模型把 prompt-JSON 的壳套进原生 arguments（{"args":{...}}），
        # 解包一层，否则工具收到嵌套参数 -> 契约判无 path 而拒绝。
        if (isinstance(call_args, dict) and set(call_args) == {"args"}
                and isinstance(call_args.get("args"), dict)):
            call_args = call_args["args"]
        if not isinstance(call_args, dict):
            call_args = {}
        obj = {"thought": "",
               "tool": tool_name_from_model(first.get("name"),
                                            registry, agent_type),
               "args": call_args,
               "tool_calls": calls}
        return obj, tokens, model_id

    if not text:
        return None, tokens, model_id
    obj = _gw.extract_json(text)
    if isinstance(obj, dict):
        return _normalize_tool_name(obj, registry, agent_type), tokens, model_id

    # SCORE-03-T3：格式自修复最多 2 次，且带**精确字段级**校验错误。
    # 旧实现只重试 1 次、提示是泛泛的「只输出 JSON」，弱模型仍会重复犯错。
    repair_msgs = list(messages)
    for attempt in range(2):
        if attempt == 0:
            hint = ("上一条输出不是合法 JSON 对象。请只输出一个 JSON 对象"
                    "（含 thought/tool/args/answer 键），不要解释或代码块。")
        else:
            hint = ("第 2 次格式错误：必须严格输出单个 JSON 对象；"
                    "tool 必须是已登记工具名，args 必须是对象。")
        repair_msgs = repair_msgs + [{"role": "user", "content": hint}]
        try:
            reply2 = provider.complete_ex(
                repair_msgs, tools=tools, temperature=0.0,
                max_tokens=max_tokens)
            text2, tokens2, model2 = reply2.as_tuple()
            tokens += tokens2 or 0
        except AttributeError:
            text2, tokens2, model2 = provider.complete(
                repair_msgs, temperature=0.0, max_tokens=max_tokens)
            tokens += tokens2 or 0
        if _detect_repetition(text2):
            return None, tokens, model_id
        obj2 = _gw.extract_json(text2)
        if not isinstance(obj2, dict):
            continue
        # 字段级校验：把具体哪个字段不合法回灌给模型（下一轮）
        problems = []
        if "tool" in obj2 and obj2.get("tool"):
            try:
                import tool_contract as _tc
                errs = _tc.validate_args(str(obj2["tool"]),
                                         obj2.get("args") or {})
                problems = [e for e in errs if not e.get("ok")]
            except Exception:                              # noqa: BLE001
                problems = []
        if not problems:
            return (_normalize_tool_name(obj2, registry, agent_type),
                    tokens, model2 or model_id)
        repair_msgs = repair_msgs + [{
            "role": "user",
            "content": "参数校验未通过，请修正后重输出完整 JSON："
                       + json.dumps([{
                           "field": e.get("field"),
                           "rule": e.get("rule"),
                           "detail": str(e.get("detail"))[:120]}
                           for e in problems[:5]], ensure_ascii=False)}]
    return None, tokens, model_id

