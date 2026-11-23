# -*- coding: utf-8 -*-
"""Anthropic Messages 协议的回环测试（不依赖真 key）。

gateway 主张"模型无关"，此前只有 OpenAI 兼容协议有回环验证。Anthropic
用另一套端点（/v1/messages）、把 system 独立出来、tool_use 结构也不
同 —— 这些差异用 mock 服务逐条锁住，比多接一家真厂商更能证明协议无关。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    reply = {}
    status = 200
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Handler.seen.append({"path": self.path,
                              "headers": dict(self.headers),
                              "body": json.loads(self.rfile.read(n) or "{}")})
        payload = json.dumps(_Handler.reply).encode()
        self.send_response(_Handler.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = "http://127.0.0.1:%d/v1" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        _Handler.seen = []
        _Handler.status = 200
        _Handler.reply = {
            "id": "msg_1", "model": "mock-claude",
            "content": [{"type": "text", "text": "你好"}],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    def provider(self, **kw):
        sp = GW.ProviderSpec(backend="anthropic_compat", base_url=self.base,
                             model="mock-claude", supports_tools=True,
                             timeout=10, **kw)
        return GW.AnthropicCompatProvider(sp)


class TestRequestShape(_Base):
    def test_endpoint_and_headers(self):
        p = self.provider(api_key_env="ANTHROPIC_MOCK_KEY")
        os.environ["ANTHROPIC_MOCK_KEY"] = "sk-ant-test"
        try:
            p.complete([{"role": "user", "content": "hi"}])
        finally:
            os.environ.pop("ANTHROPIC_MOCK_KEY", None)
        req = _Handler.seen[0]
        self.assertEqual(req["path"], "/v1/messages",
                         "Anthropic 端点是 /v1/messages，不是 chat/completions")
        # HTTP 头名字大小写不敏感，服务端可能规范化成 X-Api-Key
        h = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(h.get("x-api-key"), "sk-ant-test")
        self.assertEqual(h.get("anthropic-version"), "2023-06-01")

    def test_system_is_separated_from_messages(self):
        p = self.provider()
        p.complete([{"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "hi"}])
        body = _Handler.seen[0]["body"]
        self.assertEqual(body["system"], "你是助手")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}],
                         "system 不能留在 messages 里（Anthropic 不接受）")

    def test_temperature_forwarded(self):
        p = self.provider()
        p.complete([{"role": "user", "content": "hi"}], temperature=0.3)
        self.assertEqual(_Handler.seen[0]["body"]["temperature"], 0.3)


class TestResponse(_Base):
    def test_text_and_usage(self):
        p = self.provider()
        text, tokens, model = p.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "你好")
        self.assertEqual(tokens, 18, "input+output tokens 都要算")
        self.assertEqual(model, "mock-claude")

    def test_multiple_text_blocks_are_joined(self):
        _Handler.reply["content"] = [
            {"type": "text", "text": "第一段"},
            {"type": "text", "text": "第二段"},
            {"type": "tool_use", "id": "x", "name": "kb.search",
             "input": {"q": "a"}},
        ]
        p = self.provider()
        text, _t, _m = p.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "第一段第二段",
                         "tool_use 块的文本不能被拼进回复")

    def test_tool_use_is_normalized(self):
        _Handler.reply["content"] = [
            {"type": "text", "text": ""},
            {"type": "tool_use", "id": "t1", "name": "kb.search",
             "input": {"q": "订单"}},
        ]
        p = self.provider()
        reply = p.complete_ex([{"role": "user", "content": "hi"}],
                              tools=[{"function": {"name": "kb.search"}}])
        self.assertEqual(reply.tool_calls,
                         [{"id": "t1", "name": "kb.search",
                           "args": {"q": "订单"}}])
        self.assertIn("tools", _Handler.seen[0]["body"])

    def test_tool_schema_drops_openai_wrapper(self):
        """Anthropic 的 tools 是 {name,description,input_schema}，无 function 层。"""
        p = self.provider()
        p.complete_ex([{"role": "user", "content": "hi"}],
                      tools=[{"function": {"name": "kb.search",
                                           "description": "搜索",
                                           "parameters": {"type": "object"}}}])
        t = _Handler.seen[0]["body"]["tools"][0]
        self.assertEqual(t["name"], "kb.search")
        self.assertNotIn("function", t)
        self.assertIn("input_schema", t)

    def test_missing_usage_gives_nonzero_estimate(self):
        _Handler.reply.pop("usage", None)
        p = self.provider()
        _text, tokens, _m = p.complete([{"role": "user", "content": "hi"}])
        self.assertGreaterEqual(tokens, 1)


class TestErrors(_Base):
    def test_auth_401(self):
        _Handler.status = 401
        _Handler.reply = {"error": {"message": "bad key"}}
        with self.assertRaises(GW.LLMError) as ctx:
            self.provider().complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.code, "E_LLM_AUTH")

    def test_500_is_down_retryable(self):
        _Handler.status = 500
        _Handler.reply = {"error": {"message": "oops"}}
        with self.assertRaises(GW.LLMError) as ctx:
            self.provider().complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.code, "E_LLM_DOWN")


if __name__ == "__main__":
    unittest.main()
