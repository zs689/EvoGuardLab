# -*- coding: utf-8 -*-
"""OpenAI 兼容协议的本地回环测试（不依赖任何厂商 key）。

跨模型实测已经证明两家能跑通，但"任何 OpenAI 兼容服务都能接"这个主张，
靠的是**协议解析本身**是否正确：请求体字段名、Bearer 头、响应里 content /
usage / tool_calls 的位置、错误码映射。这些用本地 mock 服务逐条验，比多接
一家真实厂商更本质 —— 也不会有 key 缺失导致的大片 NOT_RUN。
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
    server_version = "MockOpenAI/1.0"
    #: 由测试类注入
    reply = {}
    status = 200
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        _Handler.seen.append({"path": self.path,
                              "headers": dict(self.headers),
                              "body": json.loads(raw or "{}")})
        payload = json.dumps(_Handler.reply).encode()
        self.send_response(_Handler.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _MockBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _Handler.seen = []
        _Handler.status = 200
        _Handler.reply = {
            "id": "chatcmpl-1", "object": "chat.completion",
            "model": "mock-1",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "你好"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                      "total_tokens": 18},
        }

    def provider(self, **kw):
        sp = GW.ProviderSpec(backend="openai_compat", base_url=self.base,
                             model="mock-1", supports_tools=True, timeout=10,
                             **kw)
        return GW.OpenAICompatProvider(sp)


class TestRequestShape(_MockBase):
    def test_endpoint_path_and_core_fields(self):
        p = self.provider()
        p.complete([{"role": "user", "content": "hi"}], temperature=0.2,
                   max_tokens=64)
        req = _Handler.seen[0]
        self.assertTrue(req["path"].endswith("/chat/completions"),
                        "应打到 /chat/completions，实际 %s" % req["path"])
        b = req["body"]
        self.assertEqual(b["model"], "mock-1")
        self.assertEqual(b["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(b["temperature"], 0.2)
        self.assertEqual(b["max_tokens"], 64)

    def test_bearer_header_when_key_present(self):
        os.environ["MOCK_KEY_ENV"] = "sk-test-123"
        try:
            p = self.provider(api_key_env="MOCK_KEY_ENV")
            p.complete([{"role": "user", "content": "hi"}])
            h = _Handler.seen[0]["headers"]
            self.assertEqual(h.get("Authorization"), "Bearer sk-test-123")
        finally:
            os.environ.pop("MOCK_KEY_ENV", None)

    def test_no_auth_header_for_local_endpoint(self):
        p = self.provider()
        p.complete([{"role": "user", "content": "hi"}])
        self.assertNotIn("Authorization", _Handler.seen[0]["headers"])

    def test_missing_key_when_explicitly_required_raises(self):
        p = self.provider(api_key_env="DEFINITELY_NOT_SET_ENV")
        with self.assertRaises(GW.LLMError) as ctx:
            p.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.code, "E_LLM_AUTH")
        self.assertEqual(_Handler.seen, [], "鉴权缺失时不该发出无鉴权请求")


class TestResponseParsing(_MockBase):
    def test_content_and_usage(self):
        p = self.provider()
        text, tokens, model = p.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "你好")
        self.assertEqual(tokens, 18, "token 应取 total_tokens")
        self.assertEqual(model, "mock-1")

    def test_native_tool_calls(self):
        _Handler.reply["choices"][0]["message"] = {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "kb.search",
                                         "arguments": '{"q":"x"}'}}]}
        _Handler.reply["choices"][0]["finish_reason"] = "tool_calls"
        p = self.provider()
        reply = p.complete_ex([{"role": "user", "content": "hi"}],
                              tools=[{"type": "function", "function": {
                                  "name": "kb.search"}}])
        calls = reply.tool_calls
        self.assertEqual(len(calls), 1)
        # 解析后是**归一化**结构 {id, name, args}，不是 OpenAI 原始嵌套格式：
        # agent 侧统一按这个消费，换 provider 不用改判断逻辑。
        self.assertEqual(calls[0]["name"], "kb.search")
        self.assertEqual(calls[0]["args"], {"q": "x"})
        # 请求里必须带上 tools，否则模型不会走原生调用
        self.assertIn("tools", _Handler.seen[0]["body"])

    def test_missing_usage_does_not_crash(self):
        _Handler.reply.pop("usage", None)
        p = self.provider()
        text, tokens, _m = p.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "你好")
        self.assertGreaterEqual(tokens, 1, "无 usage 时要给合理估计，不能 0")


class TestErrorMapping(_MockBase):
    def _expect(self, status, code):
        _Handler.status = status
        _Handler.reply = {"error": {"message": "mock failure"}}
        p = self.provider()
        with self.assertRaises(GW.LLMError) as ctx:
            p.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.code, code,
                         "HTTP %d 应映射成 %s" % (status, code))

    def test_401_is_auth(self):
        self._expect(401, "E_LLM_AUTH")

    def test_404_is_model(self):
        self._expect(404, "E_LLM_MODEL")

    def test_429_is_rate_limited(self):
        self._expect(429, "E_LLM_RATE")

    def test_500_is_down(self):
        self._expect(500, "E_LLM_DOWN")


if __name__ == "__main__":
    unittest.main()
