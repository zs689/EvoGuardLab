# -*- coding: utf-8 -*-
"""前端 HTML 结构与安全渲染回归测试。

防止再次出现：
1. 未闭合的 <select> 吞掉后续 DOM（曾导致 #runs-body 变 null，首页红字报错）；
2. bars() 调用传入原始 HTML 标签被 esc() 转义为字面量 <b>A</b>。
"""
import html.parser
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(ROOT, "frontend", "index.html")


class _SelectBalanceParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.max_depth = 0
        self.negative = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "select":
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)

    def handle_endtag(self, tag):
        if tag.lower() == "select":
            self.depth -= 1
            if self.depth < 0:
                self.negative = True


class TestFrontendHtmlIntegrity(unittest.TestCase):
    def setUp(self):
        with open(INDEX_HTML, encoding="utf-8") as f:
            self.html = f.read()

    def test_all_select_tags_are_properly_closed(self):
        parser = _SelectBalanceParser()
        parser.feed(self.html)
        self.assertFalse(parser.negative, "存在多余的 </select>")
        self.assertEqual(parser.depth, 0, "存在未闭合的 <select> 标签（会吞掉后续 DOM）")
        self.assertEqual(parser.max_depth, 1, "<select> 不应嵌套")

    def test_no_raw_html_tags_passed_to_bars_label(self):
        # bars() 对 label 做 esc() 转义，传入 <b>/<span> 会直接把标签打印到页面上
        bad = re.findall(r"label\s*:\s*['\"]<(?:b|span)\b", self.html)
        self.assertEqual(bad, [], "bars() label 不应含原始 HTML 标签：%s" % bad)


if __name__ == "__main__":
    unittest.main()
