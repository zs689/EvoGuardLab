# -*- coding: utf-8 -*-
"""语料解析与「就绪度体检」。

就绪度体检要回答的问题只有一个：
  把这份文档交给 AI 之前，它到底够不够用？
v1 只查三件最便宜、最能说明问题的事：
  1. 有没有空章节（有标题没正文）——AI 会被标题骗
  2. 有没有近乎重复的章节——会让检索把同一段召回两次，挤掉正确答案
  3. 章节长度分布——太长的章节检索时只能截断，容易丢答案
"""
import os
import re
from collections import Counter

from .tokenize import tokenize

HEAD_RE = re.compile(r"^(#{2,3})\s+(\S+)\s+(.*)$")


def load_corpus(corpus_dir):
    """返回 (docs, sections)

    docs:     [{"doc_id","title","sections":[{"sec_id","heading","text"}]}]
    sections: {"差旅#3.1": "正文……"}
    """
    docs = []
    sections = {}
    for name in sorted(os.listdir(corpus_dir)):
        if not name.lower().endswith(".md"):
            continue
        path = os.path.join(corpus_dir, name)
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

        stem = os.path.splitext(name)[0]
        doc_id = stem.split("-", 1)[1] if "-" in stem else stem
        title = ""
        secs = []
        cur = None

        for line in lines:
            if line.startswith("# ") and not title:
                title = line[2:].strip()
                continue
            m = HEAD_RE.match(line)
            if m:
                if cur:
                    cur["text"] = "\n".join(cur["buf"]).strip()
                    del cur["buf"]
                    secs.append(cur)
                # group(1) 现在是井号串：决定层级（## = 2，### = 3）
                level = len(m.group(1))
                cur = {"sec_id": m.group(2), "heading": m.group(3).strip(),
                       "level": level, "buf": []}
                continue
            if cur is not None:
                cur["buf"].append(line)

        if cur:
            cur["text"] = "\n".join(cur["buf"]).strip()
            del cur["buf"]
            secs.append(cur)

        for s in secs:
            s["key"] = doc_id + "#" + s["sec_id"]
            # 正文里必须带上章节号与标题：否则回答写「根据 3.1 条规定」时，
            # 数字 3.1 不在 evidence 里，会被误判成 NUMBER_MISMATCH。
            head = ("%s %s" % (s["sec_id"], s.get("heading") or "")).strip()
            sections[s["key"]] = (head + "\n" + s["text"]).strip()
        docs.append({"doc_id": doc_id, "title": title, "sections": secs})

    return docs, sections


def health_check(docs, sections):
    """就绪度体检：返回可直接写进报告的几组数字。"""
    total_chars = sum(len(t) for t in sections.values())
    empty = [k for k, t in sections.items() if len(t) < 20]

    # 近似重复：二元组集合的 Jaccard > 0.8
    keys = list(sections.keys())
    tok_sets = {k: set(tokenize(sections[k])) for k in keys}
    dups = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = tok_sets[keys[i]], tok_sets[keys[j]]
            if not a or not b:
                continue
            inter = len(a & b)
            union = len(a | b)
            if union and inter / union > 0.8:
                dups.append((keys[i], keys[j], round(inter / union, 2)))

    lens = sorted(len(t) for t in sections.values())
    mid = lens[len(lens) // 2] if lens else 0

    # 词频前 10（方便判断这份语料到底在讲什么）
    freq = Counter()
    for t in sections.values():
        freq.update(tokenize(t))

    return {
        "docs": len(docs),
        "sections": len(sections),
        "chars": total_chars,
        "median_len": mid,
        "max_len": lens[-1] if lens else 0,
        "empty": empty,
        "dups": dups,
        "top_terms": [w for w, _ in freq.most_common(10)],
    }
