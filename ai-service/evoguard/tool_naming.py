# -*- coding: utf-8 -*-
"""DEEP-05：从 autonomous_agent.py 机械拆出模型工具名映射；保留显式 re-export。"""
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
import re

def tool_name_for_model(name):
    """工具名适配模型协议：把 `fs.read` -> `fs_read`（点号不合法）。

    多家 API（DeepSeek/OpenAI）要求 tool name 匹配 ^[a-zA-Z0-9_-]+$，
    但本系统工具名带命名空间点号。发送前转换，返回时反解。
    """
    return str(name).replace(".", "_")


def tool_name_from_model(name, registry=None, agent_type=None):
    """把模型返回的 tool name 还原成本系统真实名。"""
    raw = str(name or "")
    try:
        names = sorted((registry.allowed_tools(agent_type)
                        if registry else []) or [])
    except Exception:                                       # noqa: BLE001
        names = []
    for n in names:
        if tool_name_for_model(n) == raw:
            return n
    # 兜底：只在无下划线版本唯一时还原
    return raw.replace("_", ".", 1) if "." not in raw else raw


def _normalize_tool_name(obj, registry, agent_type):
    """模型输出里的 tool 名统一还原（原生/文本两条路径都走这里）。"""
    if not isinstance(obj, dict) or not obj.get("tool"):
        return obj
    obj = dict(obj)
    obj["tool"] = tool_name_from_model(obj["tool"], registry, agent_type)
    return obj


def _tools_spec(registry, agent_type):
    """把 registry 里该 Agent 的工具转成 OpenAI `tools` 数组（原生调用用）。"""
    declared = registry.declared() or {}
    try:
        names = sorted(registry.allowed_tools(agent_type))
    except Exception:                                       # noqa: BLE001
        names = list(declared)
    out = []
    for name in names:
        d = declared.get(name) or {}
        pkeys = list(d.get("param_keys") or [])
        props = {k: {"type": "string"} for k in pkeys}
        required = [k for k in (d.get("required") or []) if k in pkeys]
        # 没有显式 required 时，param_keys 全部视为必填（工具契约要求）；
        # 否则模型会生成空 arguments（DeepSeek 实测），工具调用必然失败。
        if not required:
            required = pkeys
        out.append({"type": "function", "function": {
            "name": tool_name_for_model(name),
            "description": "%s — params: %s" % (name, ", ".join(pkeys)),
            "parameters": {"type": "object", "properties": props,
                           "required": required,
                           "additionalProperties": False}}})
    return out

