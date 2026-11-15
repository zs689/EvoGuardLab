#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-020/021：前端静态可访问性自查（不装浏览器，纯标准库）。

    python scripts/check_frontend_a11y.py --json <path>

**边界**：静态解析只能查标签/属性层面的问题；焦点顺序、读屏实际朗读、
键盘操作是否真的可用，仍需要真实浏览器人工验证。
"""
import argparse
import io
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "frontend", "index.html")


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = None
        self.inputs = []
        self.buttons = []
        self.labels_for = set()
        self.positive_tabindex = []
        self.button_text = {}
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "label" and a.get("for"):
            self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            self.inputs.append({
                "tag": tag, "id": a.get("id"),
                "type": a.get("type", ""),
                "aria_label": a.get("aria-label"),
                "aria_labelledby": a.get("aria-labelledby"),
                "title": a.get("title"),
                "placeholder": a.get("placeholder"),
                "line": self.getpos()[0],
            })
        if tag == "button":
            bid = a.get("id") or ("anon@%d" % self.getpos()[0])
            self.buttons.append({
                "id": bid, "aria_label": a.get("aria-label"),
                "title": a.get("title"), "text": "",
                "line": self.getpos()[0],
            })
            self.stack.append(("button", bid))
        ti = a.get("tabindex")
        if ti and ti.isdigit() and int(ti) > 0:
            self.positive_tabindex.append((tag, a.get("id"), ti))
        if tag in ("input", "select", "textarea", "button", "a", "tr", "div", "li"):
            self.stack.append((tag, a.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        for kind, ident in self.stack:
            if kind == "button" and ident:
                for b in self.buttons:
                    if b["id"] == ident:
                        b["text"] += text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    with io.open(PAGE, encoding="utf-8") as f:
        src = f.read()
    c = Collector()
    c.feed(src)

    problems, notes = [], []
    if not c.lang:
        problems.append("html 缺 lang 属性")
    for inp in c.inputs:
        has_name = (inp["aria_label"] or inp["aria_labelledby"] or
                    inp["title"] or
                    (inp["id"] and inp["id"] in c.labels_for))
        if not has_name:
            problems.append("L%d <%s id=%s> 无可访问名称（只有 placeholder）"
                            % (inp["line"], inp["tag"], inp["id"]))
    for b in c.buttons:
        if not (b["text"].strip() or b["aria_label"] or b["title"]):
            problems.append("L%d <button id=%s> 无可访问名称"
                            % (b["line"], b["id"]))
    for tag, ident, ti in c.positive_tabindex:
        problems.append("<%s id=%s> 用了正 tabindex=%s" % (tag, ident, ti))
    if not re.search(r":focus", src):
        problems.append("CSS 里找不到 :focus 样式")
    else:
        notes.append("CSS 有 :focus 规则（%d 处）" % len(
            re.findall(r":focus", src)))
    # JS 里给非交互元素挂 onclick：静态只能提示，需真实浏览器确认
    js_click = re.findall(r"\$\$\('([^']+)'\)\.forEach\([^)]*onclick", src)
    if js_click:
        notes.append("以下选择器由 JS 挂 onclick，键盘可达性需浏览器确认：%s"
                     % ", ".join(js_click))
    report = {
        "version": 1,
        "method": "stdlib html.parser 静态解析；不等于真实浏览器/读屏验证",
        "page": "frontend/index.html",
        "lang": c.lang,
        "inputs": len(c.inputs), "buttons": len(c.buttons),
        "label_for_targets": len(c.labels_for),
        "focus_rules": len(re.findall(r":focus", src)),
        "js_onclick_selectors": js_click,
        "problems": problems,
        "notes": notes,
        "passed": not problems,
    }
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
            f.write("\n")
    for p in problems:
        print("  [!] %s" % p)
    print("== %s ==（%d 项问题，%d 个 input / %d 个 button）"
          % ("PASS" if report["passed"] else "FAIL", len(problems),
             len(c.inputs), len(c.buttons)))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
