# -*- coding: utf-8 -*-
"""MOD-DET-01-T2：污点编码绕过（修前必红反例）。

公理 C：污点比对前必须先解码归一。攻击者把受污染邮箱/URL 做
Base64/URL/Hex 编码后塞进 http.send 的 payload 即可绕过字面匹配。
"""
import base64
import os
import sys
import unittest
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import taint                                                # noqa: E402


def _state(email):
    s = {}
    taint.record_tool_output(s, {"email": email, "note": "from tool output"})
    return s


class TestTaintEncodingBypass(unittest.TestCase):
    EMAIL = "victim@example.com"

    def test_plain_email_blocked(self):
        hit = taint.tainted_argument(
            "http.send", {"payload": self.EMAIL}, _state(self.EMAIL))
        self.assertIsNotNone(hit, "明文邮箱本就该拦")

    def test_base64_email_blocked(self):
        enc = base64.b64encode(self.EMAIL.encode()).decode()
        hit = taint.tainted_argument(
            "http.send", {"payload": "data=" + enc}, _state(self.EMAIL))
        self.assertIsNotNone(hit, "Base64 编码的污点必须被解码后比对")

    def test_url_encoded_email_blocked(self):
        enc = quote(self.EMAIL, safe="")
        hit = taint.tainted_argument(
            "http.send", {"payload": "mail=" + enc}, _state(self.EMAIL))
        self.assertIsNotNone(hit, "URL 编码的污点必须被拦")

    def test_hex_email_blocked(self):
        enc = self.EMAIL.encode().hex()
        hit = taint.tainted_argument(
            "http.send", {"payload": enc}, _state(self.EMAIL))
        self.assertIsNotNone(hit, "Hex 编码的污点必须被拦")

    def test_unrelated_payload_not_blocked(self):
        hit = taint.tainted_argument(
            "http.send", {"payload": "hello world"}, _state(self.EMAIL))
        self.assertIsNone(hit, "无关内容不能误报")


if __name__ == "__main__":
    unittest.main()
