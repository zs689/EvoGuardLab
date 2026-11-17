# -*- coding: utf-8 -*-
"""条款抽取（设计文档里的难点 A 的第一版）。

输入：docx_reader 给的块流
输出：条款列表，每条带
  id / level / heading / body / numbers / strength / has_body

三件事是这一版的重心：
  1. 认编号 —— 中文制度文本编号体系混乱（一、/ 1.1 / 第三条 / （一）），要都认
  2. 抽数值 —— 后面门禁要做「数值比对」而不是「字符串包含」，必须有结构化数值
  3. 判强制等级 —— 必须/应当/原则上/可以，四级混了答案就会错得理直气壮

注意：这一版是「规则版」，先当基线。后面要拿模型补判（比如条件从句），
但基线必须先有数，否则不知道模型到底帮了多少。
"""
import re

# 编号体系：按「优先级」从强到弱匹配，先匹配到的算
NUM_PATTERNS = [
    ("chapter", re.compile(r"^第([一二三四五六七八九十百零]+)章")),
    ("section", re.compile(r"^第([一二三四五六七八九十百零]+)节")),
    ("article", re.compile(r"^第([一二三四五六七八九十百零]+)条")),
    ("cn_list", re.compile(r"^([一二三四五六七八九十]+)、")),
    ("cn_paren", re.compile(r"^（([一二三四五六七八九十]+)）")),
    ("dotted",   re.compile(r"^(\d+(?:\.\d+)+)[\.、)）]?\s*")),
    ("n_plain",  re.compile(r"^(\d+)[\.、)）]\s*")),
]

NUM_RE = re.compile(r"\d+(?:\.\d+)?")
UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(元|万元|天|个工作日|工作日|小时|分钟|人|页|次|%|％)")

# 强制等级：顺序有意义，先命中 must 就不要再往下判
STRENGTH_PATTERNS = [
    ("must",         re.compile(r"(不得|禁止|严禁|一律|必须|须(?!知))")),
    ("should",       re.compile(r"(应当|应该|应(?![用急该]|对))")),
    ("in_principle", re.compile(r"(原则上|一般应|通常情况下)")),
    ("may",          re.compile(r"(可以|可(?![能行]))")),
]


def match_numbering(text):
    """返回 (kind, label, 剩余正文)。认不出来返回 (None, None, text)。"""
    t = text.strip()
    for kind, pat in NUM_PATTERNS:
        m = pat.match(t)
        if m:
            return kind, m.group(1), t[m.end():].strip()
    return None, None, t


def extract_numbers(text):
    """结构化数值：区分「数字」和「带单位的数量」。"""
    plain = NUM_RE.findall(text)
    with_unit = [{"value": float(v), "unit": u} for v, u in UNIT_RE.findall(text)]
    return {"plain": plain, "quantities": with_unit}


def judge_strength(text):
    for name, pat in STRENGTH_PATTERNS:
        if pat.search(text):
            return name
    return "none"


def extract(blocks, doc_id="doc"):
    """块流 -> 条款列表。标题开新条款，正文与表格挂到当前条款上。"""
    clauses = []
    cur = None
    stack = []  # [(level, clause)]

    def close(c):
        if c is not None:
            c["body"] = "\n".join(c["_body"]).strip()
            del c["_body"]

    for b in blocks:
        if b["type"] == "h":
            close(cur)
            kind, label, rest = match_numbering(b["text"])
            level = b["level"]
            if cur is not None:
                while stack and stack[-1][0] >= level:
                    stack.pop()
            parent = stack[-1][1] if stack else None
            label = label if label else "H%d" % (len(clauses) + 1)
            # 层级化 id：真实文档里「1.」「2.」这种编号是**章内局部编号**，
            # 平铺成 doc#1 会跨章撞车，而且看不出归属。必须带上父级。
            cid = (parent["id"] + "#" + label) if parent else ("%s#%s" % (doc_id, label))
            cur = {
                "id": cid,
                "level": level,
                "numbering": kind,
                "label": label,
                "heading": rest or b["text"],
                "full_heading": b["text"],
                "parent": parent["id"] if parent else None,
                "_body": [],
            }
            clauses.append(cur)
            stack.append((level, cur))
        else:
            if cur is None:
                # 文档开头没有标题的散段，挂一个匿名条款，别丢
                cur = {"id": "%s#preamble" % doc_id, "level": 0, "numbering": None,
                       "label": None, "heading": "（无标题开篇）", "full_heading": "",
                       "parent": None, "_body": []}
                clauses.append(cur)
                # **不要**把 preamble（level 0）压进父级栈：
                # 否则后面所有一级标题（level 1）都不会把它弹出，
                # parent 全变成 #preamble，层级树被污染。

            if b["type"] == "table":
                cur["_body"].append("[表格] " + " ； ".join(" / ".join(r) for r in b["rows"]))
            else:
                cur["_body"].append(b["text"])

    close(cur)

    for c in clauses:
        c["has_body"] = len(c["body"]) >= 10
        c["numbers"] = extract_numbers(c["body"] + " " + c["heading"])
        c["strength"] = judge_strength(c["body"])
        c["body_len"] = len(c["body"])
    return clauses


def summarize(clauses):
    total = len(clauses)
    with_body = sum(1 for c in clauses if c["has_body"])
    with_qty = sum(1 for c in clauses if c["numbers"]["quantities"])
    by_strength = {}
    for c in clauses:
        by_strength[c["strength"]] = by_strength.get(c["strength"], 0) + 1
    by_numbering = {}
    for c in clauses:
        k = c["numbering"] or "（无编号）"
        by_numbering[k] = by_numbering.get(k, 0) + 1
    empty = [c["id"] for c in clauses if not c["has_body"]]
    return {
        "total": total,
        "with_body": with_body,
        "empty": empty,
        "with_quantity": with_qty,
        "by_strength": by_strength,
        "by_numbering": by_numbering,
    }
