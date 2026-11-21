# -*- coding: utf-8 -*-
"""FINAL-01-T2：轻量污点追踪（零第三方依赖）。

信息流隔离公理：外部只读工具返回的数据带污点；当后续写/外发工具的敏感
参数源自该污点、且**不在原始可信 user_goal 中**时，preflight 必须拦。

这是「改写型间接注入」的本质防御：不依赖攻击文本里有没有关键词，只看
数据流是否从不可信源流入有副作用的动作。
"""
import re
from urllib.parse import unquote

# TAINT-FIX-01-T1：不再硬编码工具名。副作用由契约的 side_effect/scope
# 动态判定 —— 这样 MCP 动态注册的写工具、fs.patch、order.settle 等
# 全部自动纳入污点检查，不会因为是「新工具」就绕过。
SIDE_EFFECT_VALUES = ("write", "external", "arbitrary")
SCOPE_DANGEROUS = ("write", "external")

# 敏感参数字段：值可能承载外部来源的目标/收款人/金额/路径
SENSITIVE_FIELDS = ("url", "endpoint", "callback", "to", "recipient",
                    "payee", "account", "amount", "total", "path",
                    "target_path", "payload")

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ACCOUNT_RE = re.compile(r"\b(?:ACC|ACCT|IBAN)[-_]?[0-9A-Za-z]{4,}\b", re.I)
_AMOUNT_RE = re.compile(r"\b\d{3,}(?:\.\d{1,2})?\b")

# TAINT-FIX-01-T2：文件路径提取（原先只提 URL/邮箱/账号/金额，导致
# SENSITIVE_FIELDS 里的 path/target_path 永远不可能命中污点）。
_UNIX_PATH_RE = re.compile(
    r"(?<![\w:/.])/(?:etc|var|usr|home|root|tmp|opt|srv|mnt|media|boot|"
    r"proc|sys|dev|lib|bin|sbin|windows)/[A-Za-z0-9_.\-/]*", re.I)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\s\"'<>|?*\\]+\\)*[^\s\"'<>|?*\\]+")
_REL_PATH_RE = re.compile(r"(?:^|[\s\"'(=])((?:\.\./){1,}[^\s\"'<>]+)")

# 契约缺失时的名字兜底（fail-closed：不认识的名字只要带副作用语义就算）
_NAME_HINTS = re.compile(
    r"(write|send|post|put|upload|export|settle|delete|remove|patch|"
    r"exec|shell|spawn|transfer|pay|purchase|update|insert|create|drop|"
    r"truncate|deploy|grant|revoke)", re.I)


def side_effect_of(tool, contract=None):
    """判断工具的副作用类型；返回 None 表示只读、无需污点检查。

    判定顺序（fail-closed，宁可多检查）：
      1) 显式 `side_effect` ∈ write/external/arbitrary -> 该值；
      2) `scope`/`_scope` ∈ write/external -> 该值；
      3) 契约**明确**声明只读（side_effect=none 或 scope=read_only）-> None；
      4) 契约缺失或信息不完整 -> 按名字语义兜底。
    """
    c = contract if isinstance(contract, dict) else {}
    se = str(c.get("side_effect") or "").strip().lower()
    if se in SIDE_EFFECT_VALUES:
        return se
    sc = str(c.get("_scope") or c.get("scope") or "").strip().lower()
    if sc in SCOPE_DANGEROUS:
        return sc
    if se == "none" or sc in ("read_only", "readonly"):
        return None                     # 契约明确只读，不检查污点
    if _NAME_HINTS.search(str(tool or "")):
        return "assumed_dangerous"      # 无契约但名字像写操作 -> 仍检查
    return None


def extract_sources(output):
    """从工具返回对象提取污点来源值（URL/邮箱/账号/金额/**文件路径**）。

    TAINT-FIX-01-T2：先做 URL 解码归一化，防止 `%2Fetc%2Fpasswd` 这类
    编码绕过；路径从「去掉 URL 后的文本」里提取，避免把 URL 的 `//`
    误当 Unix 路径。
    """
    raw = output if isinstance(output, str) else _flatten(output)
    text = unquote(raw)                 # 编码变体归一化
    src = {"urls": [], "emails": [], "accounts": [], "amounts": [],
           "paths": []}
    for k, pattern in (("urls", _URL_RE), ("emails", _EMAIL_RE),
                       ("accounts", _ACCOUNT_RE), ("amounts", _AMOUNT_RE)):
        seen = []
        for m in pattern.findall(text):
            v = m.rstrip(".,);") if isinstance(m, str) else str(m)
            if v and v not in seen:
                seen.append(v)
        src[k] = seen

    # 路径：在**已解码的原文**上提取（URL query 里的敏感路径也是路径，
    # 如 ?f=%2Fetc%2Fpasswd -> /etc/passwd），只过滤掉 URL 自身的
    # scheme/host 片段（含 :// 或以 // 开头）。
    paths = []
    for pattern in (_UNIX_PATH_RE, _WIN_PATH_RE, _REL_PATH_RE):
        for m in pattern.findall(text):
            v = (m if isinstance(m, str) else str(m))
            v = v.strip().rstrip(".,);")
            if not v or "://" in v or v.startswith("//"):
                continue
            if v not in paths:
                paths.append(v)
    src["paths"] = paths
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
    """汇总污点来源里的所有具体值（含 TAINT-FIX-01-T2 新增的 paths）。"""
    src = (state or {}).get("taint_sources") or {}
    out = []
    for k in ("urls", "emails", "accounts", "amounts", "paths"):
        out.extend(str(v) for v in (src.get(k) or []))
    return out


def _decode_variants(value):
    """MOD-DET-01-T2：把参数值展开成「原文 + 常见编码还原」集合。

    公理 C：污点比对前必须穿透编码层。只做**确定性解码**，不执行任何
    内容；解码失败就跳过该变体（不静默放过原文比对）。
    """
    import base64 as _b64
    import binascii as _bin
    from urllib.parse import unquote as _unquote
    out = [value]
    seen = {value}
    def add(v):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    # URL / 双重 URL 编码
    u = _unquote(value)
    add(u)
    add(_unquote(u))
    # Hex（连续偶数位十六进制且长度合理）
    if len(value) >= 8 and len(value) % 2 == 0 \
            and all(c in "0123456789abcdefABCDEF" for c in value):
        try:
            add(bytes.fromhex(value).decode("utf-8", "replace"))
        except (ValueError, _bin.Error):
            pass
    # Base64：整串或从 =/空白 之后的子串（容忍 "data=..." 前缀）
    candidates = [value]
    for sep in ("=", ":", ",", ";", " "):
        if sep in value:
            candidates.append(value.rsplit(sep, 1)[-1])
    import re as _re
    candidates += _re.findall(r"[A-Za-z0-9+/]{12,}={0,2}", value)
    for c in candidates:
        if len(c) < 8 or len(c) % 4:
            continue
        try:
            dec = _b64.b64decode(c, validate=True)
            add(dec.decode("utf-8", "replace"))
        except (_bin.Error, ValueError, UnicodeError):
            continue
    return out


def tainted_argument(tool, args, state, contract=None):
    """若**副作用工具**的敏感参数源自污点且不在 user_goal 中，返回命中项。

    TAINT-FIX-01-T1：副作用判定改为 `side_effect_of(tool, contract)`，
    不再依赖硬编码的 3 个工具名。返回 None 表示无污染；否则返回
    {"field":..., "value":...}。
    """
    if side_effect_of(tool, contract) is None:
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
        variants = _decode_variants(val)
        if any(t and any(t == x or t in x or x in t for x in variants)
               for t in tainted):
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
                           {"urls": [], "emails": [], "accounts": [],
                            "amounts": [], "paths": []})
    for k, vals in found.items():
        for v in vals:
            if v not in cur.setdefault(k, []):
                cur[k].append(v)
