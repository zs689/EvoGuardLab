# -*- coding: utf-8 -*-
"""DEEP-05：不可见字符规整与隐藏载荷检测（从 security.py 拆出）。"""

# DEEP-05 通用兜底 imports（机械拆分后原模块级依赖统一补齐）
import hashlib
import json
import os
import re
import time
import traceback
import uuid
import concurrent.futures as _cf

import re
import json


def _text(v):
    """与主模块同名的纯函数（避免循环 import；行为必须保持一致）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)

_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
# 快速路径：正则在 C 层扫一遍，正常文本（占绝大多数）不做 Python 逐字符循环。
# 没有这一步，10KB 文本每字符过一遍 Python 会让 scan 从 ~0.5ms 涨到 ~1ms，
# 逼近 tests/test_security.py 的 1.5ms 预算，并在负载高时偶发超时。
_INVISIBLE_RE = re.compile(
    "[\U000E0000-\U000E007F\u200B\u200C\u200D\u2060\uFEFF"
    "\u202A-\u202E\u2066-\u2069]")
_TAG_RE = re.compile("[\U000E0000-\U000E007F]")
_ZW_RE = re.compile("[\u200B\u200C\u200D\u2060\uFEFF]")
_BIDI_RE = re.compile("[\u202A-\u202E\u2066-\u2069]")
_ZW_BIDI_RE = re.compile("[\u200B\u200C\u200D\u2060\uFEFF\u202A-\u202E\u2066-\u2069]")
# str.translate 在 C 层一次过，比 Python 逐字符快一个数量级
_DELETE_INVISIBLE = {cp: None for cp in (_ZERO_WIDTH | _BIDI)}
# tag 解码也走 translate：早先用 re.sub(lambda) 时，1 万个 tag 字符要 7.7ms，
# 等于给攻击者留了一个「塞满隐藏字符拖慢扫描」的慢速面。
_TAG_DROP = {0xE0000 + i: None for i in range(0x80)}
_TAG_DECODE = {0xE0000 + i: chr(i) for i in range(0x80)}

# 不做「只解码前 N 个」这类截断：截断就可能漏掉藏在后面的指令。
# 代价是含隐藏字符的 10KB 文本最坏 ~2.3ms（正常文本仍 0.38ms），
# 按「时间不急、安全优先」的口径接受；绝不为性能牺牲覆盖。
# 但也不能让攻击者用「塞满 tag」把扫描拖成秒级 —— 实测全 tag 10KB 1.9ms，
# 因为 decode 走的是 C 层 translate（与 tag 数量无关的常数开销），不是逐字符循环。


def strip_invisible(text):
    """返回 (剥离视图, 解码视图, 各类计数)。

    剥离视图：删掉所有不可见字符，让被拆开的关键词重新连上
              （`Ig\\u200Bnore` → `Ignore`）。
    解码视图：把 tag chars 还原成对应 ASCII（LlamaFirewall 的做法），
              用来抓「整段指令都藏在 tag 字符里」的情况。
    两个视图都扫，互补；正常文本没有隐藏字符时零开销。

    实现上刻意全用 C 层的 re / str.translate：早先逐字符 Python 循环，
    10KB 带隐藏字符的文本要 1.39ms（3.9 倍），逼近 1.5ms 预算。
    """
    s = _text(text)
    if not _INVISIBLE_RE.search(s):
        return s, s, {"tag": 0, "zero_width": 0, "bidi": 0}
    counts = {"tag": len(_TAG_RE.findall(s)),
              "zero_width": len(_ZW_RE.findall(s)),
              "bidi": len(_BIDI_RE.findall(s))}
    base = s.translate(_DELETE_INVISIBLE)
    # 两个视图都覆盖完整 128 个 tag 码位，不做截断 —— 少解码一个字符就可能漏一条攻击。
    plain = base.translate(_TAG_DROP)
    decoded = base.translate(_TAG_DECODE)
    return plain, decoded, counts


def sanitize_invisible(text):
    """显式剥离不可见字符（调用方要用才调，默认路径不改内容）。"""
    return strip_invisible(text)[0]


def _hidden_findings(counts, decoded, where, only=None):
    out = []
    rules = [
        ("tag", "hidden_tag_chars", "error",
         "ASCII tag 字符（U+E0000..U+E007F）可把指令藏成肉眼不可见文本；"
         "解码后片段：%s" % (decoded[:120] or "—"),
         "极少合法出现；若确为富文本标签残留需人工确认"),
        ("zero_width", "hidden_zero_width", "warn",
         "零宽字符可拆开关键词绕过检测，已剥离后重新扫描",
         "部分编辑器/复制粘贴会带入零宽空格，需人工看上下文"),
        ("bidi", "hidden_bidi_control", "warn",
         "双向控制符可造成视觉顺序与实际顺序不一致（Trojan Source）",
         "阿拉伯语/希伯来语等 RTL 文本会合法使用，属可接受误报"),
    ]
    for key, code, severity, detail, fp in rules:
        if not counts[key] or (only and code not in only):
            continue
        out.append({"code": code, "severity": severity, "where": where,
                    "match": "%s ×%d" % (key, counts[key]),
                    "detail": detail, "false_positive": fp})
    return out


_CALLS, _FINDINGS, _NANOS = 0, 0, 0


