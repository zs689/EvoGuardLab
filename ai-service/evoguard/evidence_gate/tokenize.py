# -*- coding: utf-8 -*-
"""中文分词：只靠标准库。

为什么不用 jieba：本项目要求「不装任何包」。
对检索来说，中文按「字符二元组」切已经够用：
  「住宿费标准」-> 住宿/宿费/费标/标准
它比单字精确，又不需要词典。
"""
import re

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text):
    text = text.lower()
    out = []
    for m in _ASCII_WORD.finditer(text):
        out.append(m.group())
    for m in _CJK_RUN.finditer(text):
        s = m.group()
        if len(s) == 1:
            out.append(s)
        else:
            for i in range(len(s) - 1):
                out.append(s[i:i + 2])
    return out


def tokenize_weighted(text):
    """带权重的切分，供覆盖率计算用：bigram 权重 1.0、unigram 0.5。

    为什么补 unigram：只切二元组时，「住酒店」→ 住酒/酒店，而正文「住宿」
    → 住宿，两串零交集，口语查询必然漏检（q01 的根因）。
    补单字能接上，但单字区分度低，所以只给半权重，避免覆盖率虚高。
    """
    from collections import Counter
    c = Counter()
    low = (text or "").lower()
    for m in _ASCII_WORD.finditer(low):
        c[m.group()] += 1.0
    for m in _CJK_RUN.finditer(low):
        s = m.group()
        if len(s) == 1:
            c[s] += 1.0
            continue
        for i in range(len(s) - 1):
            c[s[i:i + 2]] += 1.0
        for ch in s:
            c[ch] += 0.5
    return c
