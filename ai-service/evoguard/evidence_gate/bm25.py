# -*- coding: utf-8 -*-
"""纯标准库 BM25 检索器（这是「混合检索」里的关键词那一路）。

为什么先只做 BM25：
  1. 它不需要模型、不需要 GPU、结果稳定可复现 —— 基线必须先稳。
  2. 制度类文本的核心是「条款号 + 数字 + 专有名词」的精确匹配，
     BM25 在这类查询上常常比纯向量检索还准。
  3. 等这版基线数字出来，再往上叠向量检索，才能说清「提升了多少」。
"""
import math
from collections import Counter

from .tokenize import tokenize


class BM25Index:
    def __init__(self, k1=1.5, b=0.75):
        self.k1 = k1
        self.b = b
        self.ids = []
        self.texts = []
        self.tfs = []
        self.lens = []
        self.df = Counter()
        self.idf_map = {}
        self.avgdl = 0.0

    def add(self, doc_id, text):
        tf = Counter(tokenize(text))
        self.ids.append(doc_id)
        self.texts.append(text)
        self.tfs.append(tf)
        self.lens.append(sum(tf.values()) or 1)
        for term in tf:
            self.df[term] += 1

    def build(self):
        n = len(self.ids)
        if n == 0:
            return self
        self.avgdl = sum(self.lens) / n
        for term, df in self.df.items():
            self.idf_map[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        return self

    def search(self, query, top_k=5):
        q_terms = sorted(set(tokenize(query)))
        scored = []
        for i, tf in enumerate(self.tfs):
            dl = self.lens[i]
            s = 0.0
            for t in q_terms:
                f = tf.get(t)
                if not f:
                    continue
                s += self.idf_map[t] * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                )
            if s > 0:
                scored.append((round(s, 4), self.ids[i]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[:top_k]
