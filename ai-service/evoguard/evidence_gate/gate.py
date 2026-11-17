# -*- coding: utf-8 -*-
"""证据门禁：一条答案要连过四关，才算「可核验」。

第一关 每句话都要带引用 —— 没依据的话一句都不许有
第二关 引用必须指向真实存在的章节 —— 防模型编造章节号
第三关 被引用的章节要真的支撑这句话 —— 防「引一个无关条款凑数」
第四关 句子里的数字必须逐字出现在被引章节里 —— 防「550 改成 800」

为什么单独做第四关：
  字面覆盖率对「只改一个数字」几乎无感（改完仍然 85% 重合），
  而制度/合规场景里，数字错了和没有依据是同一个后果。
  这是本门禁与通用 groundedness 检测最不一样的地方。
"""
import re
from collections import Counter

from .tokenize import tokenize, tokenize_weighted

CITE_RE = re.compile(r"\[([^\[\]#]+)#([^\[\]]+)\]")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _norm_num(s):
    """归一化数字，供集合比对。避免「550 in 5500」这种子串误判。

    只做两件事：去千分位、去小数尾零。不做单位换算 —— 换算是语义问题，
    这里只保证「同一个数写成不同形式能匹配上」。
    """
    s = str(s).replace(",", "").replace("，", "").strip()
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s

SUPPORT_THRESHOLD = 0.40

PASS = "PASS"
ABSTAIN = "ABSTAIN"
NO_CITATION = "NO_CITATION"
CITATION_NOT_FOUND = "CITATION_NOT_FOUND"
CITATION_NOT_SUPPORTING = "CITATION_NOT_SUPPORTING"
NUMBER_MISMATCH = "NUMBER_MISMATCH"

# 明确拒答的说法：这些话本来就不该带引用，不能算「无引用」
ABSTAIN_RE = re.compile(r"(未找到|找不到|没有找到|未提及|未作规定|无相关规定|没有相关|无法确定|无法回答|资料中未|文档中未)")

# 严重程度排序：数字越小越严重，报告里取最严重的那一条
SEVERITY = {
    NO_CITATION: 1,
    CITATION_NOT_FOUND: 2,
    NUMBER_MISMATCH: 3,
    CITATION_NOT_SUPPORTING: 4,
    ABSTAIN: 8,
    PASS: 9,
}

LABEL_CN = {
    PASS: "可核验",
    ABSTAIN: "明确拒答",
    NO_CITATION: "无引用",
    CITATION_NOT_FOUND: "引用不存在",
    CITATION_NOT_SUPPORTING: "引用不支撑",
    NUMBER_MISMATCH: "数字对不上",
}


def split_sentences(text):
    parts = re.split(r"[。！？；\n]", text)
    return [p.strip() for p in parts if p.strip()]


def coverage(claim, evidence):
    """claim 的二元组有多大比例出现在 evidence 里（0~1）。"""
    c = tokenize_weighted(claim)
    total = sum(c.values())
    if total == 0:
        return 0.0
    e = tokenize_weighted(evidence)
    hit = sum(min(v, e[k]) for k, v in c.items() if k in e)
    return hit / total


def check(answer, sections):
    """sections: {"差旅#3.1": "正文……"}

    返回的 label 是整条答案里**最严重**的那个问题：
    只要有半句没依据，整条就不能算通过 —— 这是刻意的，宁可严不可漏。
    唯一的例外是「明确拒答」：它不算问题，只在整条答案全是拒答时才体现出来。
    """
    sentences = split_sentences(answer)
    rows = []
    worst = PASS
    has_pass = False
    has_abstain = False

    for sent in sentences:
        cites = CITE_RE.findall(sent)
        claim = CITE_RE.sub("", sent).strip()

        if not cites:
            # 明确说「没找到」是正确行为，不该被当成「无引用」扣分。
            # 这一条不加的话，接上真模型后所有正确拒答都会被误判，数据全废。
            if ABSTAIN_RE.search(sent):
                has_abstain = True
                rows.append({"sentence": sent, "status": ABSTAIN, "cite": "", "coverage": None})
            else:
                rows.append({"sentence": sent, "status": NO_CITATION, "cite": "", "coverage": None})
                if SEVERITY[NO_CITATION] < SEVERITY[worst]:
                    worst = NO_CITATION
            continue

        # 一句话引多个条款时，必须把它们当作**证据并集**一起支撑这句话。
        # 以前的写法是对每个 cite 单独比对全部数字：一句「[差旅#3.1][差旅#2.1]」
        # 因为 450 只在 3.1 里，判 2.1 时必然报 NUMBER_MISMATCH —— 合规的
        # 联合引用 100% 误报。
        resolved, not_found = [], []
        for doc_id, sec_id in cites:
            key = doc_id + "#" + sec_id
            evidence = sections.get(key)
            if evidence is None:
                not_found.append(key)
            else:
                resolved.append((key, evidence))

        if not_found:
            rows.append({"sentence": sent, "status": CITATION_NOT_FOUND,
                         "cite": "|".join(not_found), "coverage": None})
            if SEVERITY[CITATION_NOT_FOUND] < SEVERITY[worst]:
                worst = CITATION_NOT_FOUND
            continue

        merged = "\n".join(ev for _, ev in resolved)
        keys = "|".join(k for k, _ in resolved)
        nums = NUM_RE.findall(claim)
        ev_nums = {_norm_num(x) for x in NUM_RE.findall(merged)}
        # 集合比对而不是子串包含：篡改成子串（5500 -> 550）必须被抓到。
        missing = [n for n in nums if _norm_num(n) not in ev_nums]
        cov = coverage(claim, merged)

        # 顺序很重要：先判「这段话到底在不在讲这件事」，再判数字。
        # 反过来的话，引一个完全无关的章节时（覆盖率 0）会被误报成「数字对不上」，
        # 掩盖了更严重的问题 —— 引错了地方。
        if cov < SUPPORT_THRESHOLD:
            status = CITATION_NOT_SUPPORTING
        elif missing:
            status = NUMBER_MISMATCH
        else:
            status = PASS
            has_pass = True

        rows.append({
            "sentence": sent,
            "status": status,
            "cite": keys,
            "coverage": round(cov, 3),
            "missing_numbers": missing,
        })
        if SEVERITY[status] < SEVERITY[worst]:
            worst = status

    # 整条答案全是「没找到」—— 这是明确拒答，不是通过，也不该算错
    if worst == PASS and has_abstain and not has_pass:
        worst = ABSTAIN

    return {"label": worst, "label_cn": LABEL_CN[worst], "rows": rows}

