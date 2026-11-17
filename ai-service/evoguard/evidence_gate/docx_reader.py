# -*- coding: utf-8 -*-
"""读 .docx，输出线性块流。纯标准库（zipfile + ElementTree）。

输出块类型：
  {"type": "h",     "level": 1|2|3, "text": "..."}   # 标题
  {"type": "p",     "text": "..."}                    # 正文段落
  {"type": "table", "rows": [["a","b"], ...]}         # 表格

为什么不用 python-docx：不改动你机器上的环境。
"""
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text_of(el):
    out = []
    for node in el.iter():
        if node.tag == W + "t":
            out.append(node.text or "")
        elif node.tag == W + "tab":
            out.append("\t")
        elif node.tag == W + "br":
            out.append("\n")
    return "".join(out).strip()


def _heading_level(p):
    pr = p.find(W + "pPr")
    if pr is None:
        return 0
    # 先看显式大纲级别：不使用内置 Heading 样式名的 Word 文档也能识别。
    ol = pr.find(W + "outlineLvl")
    if ol is not None:
        raw = ol.get(W + "val")
        try:
            return int(raw) + 1       # outlineLvl 0-based；本项目层级 1-based
        except (TypeError, ValueError):
            pass
    style = pr.find(W + "pStyle")
    if style is None:
        return 0
    val = (style.get(W + "val") or "").lower()
    for i in range(1, 7):
        if val in ("heading%d" % i, "标题%d" % i, "%d" % i):
            return i
    return 0


def read_docx(path):
    # 用 with 管理句柄：批量解析时旧写法会泄漏文件描述符
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    blocks = []

    for child in body:
        if child.tag == W + "p":
            txt = _text_of(child)
            if not txt:
                continue
            lvl = _heading_level(child)
            if lvl:
                blocks.append({"type": "h", "level": lvl, "text": txt})
            else:
                blocks.append({"type": "p", "text": txt})
        elif child.tag == W + "tbl":
            rows = []
            for tr in child.findall(W + "tr"):
                cells = [_text_of(tc).replace("\n", " / ") for tc in tr.findall(W + "tc")]
                if any(cells):
                    rows.append(cells)
            if rows:
                blocks.append({"type": "table", "rows": rows})
    return blocks
