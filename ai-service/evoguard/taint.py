# -*- coding: utf-8 -*-
"""FINAL-01-T2：轻量污点追踪（零第三方依赖）。

信息流隔离公理：外部只读工具返回的数据带污点；当后续写/外发工具的敏感
参数源自该污点、且**不在原始可信 user_goal 中**时，preflight 必须拦。

这是「改写型间接注入」的本质防御：不依赖攻击文本里有没有关键词，只看
数据流是否从不可信源流入有副作用的动作。
"""
import re

# 副作用工具（写/外发/任意执行）——这些才检查污点流入
SIDE_EFFECT_TOOLS = {"http.send", "shell.exec", "fs.write"}
# 敏感参数字段：值可能承载外部来源的目标/收款人/金额/路径
SENSITIVE_FIELDS = ("url", "endpoint", "callback", "to", "recipient",
                    "payee", "account", "amount", "total", "path",
                    "target_path", "payload")

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ACCOUNT_RE = re.compile(r"\b(?:ACC|ACCT|IBAN)[-_]?[0-9A-Za-z]{4,}\b", re.I)
_AMOUNT_RE = re.compile(r"\b\d{3,}(?:\.\d{1,2})?\b")


def extract_sources(output):
    """从工具返回对象提取污点来源值（URL/邮箱/账号/金额）。"""
    text = output if isinstance(output, str) else _flatten(output)
    src = {"urls": [], "emails": [], "accounts": [], "amounts": []}
    for k, pattern in (("urls", _URL_RE), ("emails", _EMAIL_RE),
                       ("accounts", _ACCOUNT_RE), ("amounts", _AMOUNT_RE)):
        seen = []
        for m in pattern.findall(text):
            v = m.rstrip(".,);") if isinstance(m, str) else str(m)
            if v and v not in seen:
                seen.append(v)
        src[k] = seen
    return src


def _flatten(obj, depth=0):
    """把 dict/list 展平成文本（限深防循环）。"""
    if depth > 6:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)) or obj is None:
        return str(obj)
    if isinstance(obj, dict):
        return " ".join(_flatten(v, depth + 1) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_flatten(v, depth + 1) for v in obj)
    return str(obj)


def _values(state):
    """汇总污点来源里的所有具体值。"""
    src = (state or {}).get("taint_sources") or {}
    out = []
    for k in ("urls", "emails", "accounts", "amounts"):
        out.extend(str(v) for v in (src.get(k) or []))
    return out


def tainted_argument(tool, args, state):
    """若写/外发工具的敏感参数源自污点且不在 user_goal 中，返回命中项。

    返回 None 表示无污染；否则返回 {"field":..., "value":...}。
    """
    if tool not in SIDE_EFFECT_TOOLS:
        return None
    if not isinstance(args, dict):
        return None
    goal = str((state or {}).get("user_goal") or "")
    tainted = _values(state)
    if not tainted:
        return None
    for field in SENSITIVE_FIELDS:
        v = args.get(field)
        if v is None:
            continue
        val = str(v)
        if not val:
            continue
        if any(t and (t == val or t in val or val in t) for t in tainted):
            # 原始可信目标里出现过 -> 视为用户本意，不拦
            if val and val in goal:
                continue
            if any(t and t in goal for t in tainted if t in val):
                continue
            return {"field": field, "value": val[:120]}
    return None


def record_tool_output(state, output):
    """把一次只读工具返回的污点累积进 state（原地修改）。"""
    if state is None:
        return
    found = extract_sources(output)
    cur = state.setdefault("taint_sources",
                           {"urls": [], "emails": [], "accounts": [], "amounts": []})
    for k, vals in found.items():
        for v in vals:
            if v not in cur.setdefault(k, []):
                cur[k].append(v)
