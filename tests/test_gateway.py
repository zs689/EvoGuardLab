# -*- coding: utf-8 -*-
"""模型网关测试：用本地假服务验证「真模型链路」，不需要下模型。

为什么这么测：网关的 bug 都出在协议细节（字段名、思考块、坏 JSON、HTTP 错误码），
这些用假服务能 100% 覆盖；等真模型下好了，只需要把 base 换掉。

跑法：python -m unittest tests.test_gateway -v
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

import gateway  # noqa: E402


class Stub(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容服务。行为由类属性控制。"""
    mode = "ok"

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json(200, {"data": [{"id": "stub-model"}]})
        self._json(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        mode = Stub.mode
        if mode == "http500":
            return self._json(500, {"error": "boom"})
        if mode == "down502":
            return self._json(502, {"error": "upstream connect failed"})
        if mode == "badjson":
            content = '{"total": 123, '          # 结构坏掉
        elif mode == "think":
            content = "<think>先算一下</think>{\"total\": 1058.4}"
        elif mode == "fence":
            content = "```json\n{\"total\": 1058.4}\n```"
        else:
            content = "{\"total\": 1058.4}"
        self._json(200, {"model": "stub-model",
                         "choices": [{"message": {"role": "assistant", "content": content}}],
                         "usage": {"total_tokens": 42}})


class TestGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.base = "http://127.0.0.1:%d/v1" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def provider(self):
        return gateway.LocalProvider(base=self.base, model=None, timeout=5)

    def test_mock_returns_none(self):
        p = gateway.MockProvider()
        text, tokens, mid = p.complete([{"role": "user", "content": "你好"}])
        self.assertIsNone(text, "mock 必须返回 None，不许编内容")
        self.assertGreater(tokens, 0)
        self.assertEqual(mid, "mock")

    def test_ok_and_usage(self):
        Stub.mode = "ok"
        p = self.provider()
        self.assertEqual(p.resolve_model(), "stub-model", "应自动取 /models 的第一个")
        text, tokens, mid = p.complete([{"role": "user", "content": "算一下"}])
        self.assertEqual(json.loads(text)["total"], 1058.4)
        self.assertEqual(tokens, 42, "token 必须取 usage，不能自己估")
        self.assertEqual(mid, "stub-model")

    def test_strip_think(self):
        Stub.mode = "think"
        text, _, _ = self.provider().complete([{"role": "user", "content": "x"}])
        self.assertNotIn("think", text)
        self.assertEqual(gateway.extract_json(text)["total"], 1058.4)

    def test_code_fence(self):
        Stub.mode = "fence"
        text, _, _ = self.provider().complete([{"role": "user", "content": "x"}])
        self.assertEqual(gateway.extract_json(text)["total"], 1058.4, "带 ```json 围栏也要能解析")

    def test_bad_json_returns_none(self):
        Stub.mode = "badjson"
        text, _, _ = self.provider().complete([{"role": "user", "content": "x"}])
        self.assertIsNone(gateway.extract_json(text), "坏 JSON 必须返回 None，交给调用方兜底")

    def test_http_500_raises_classified(self):
        Stub.mode = "http500"
        with self.assertRaises(gateway.LLMError) as cm:
            self.provider().complete([{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.code, "E_LLM_HTTP")

    def test_502_means_model_not_loaded(self):
        Stub.mode = "down502"
        with self.assertRaises(gateway.LLMError) as cm:
            self.provider().complete([{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.code, "E_LLM_DOWN")
        self.assertIn("没起来", str(cm.exception))

    def test_unreachable(self):
        p = gateway.LocalProvider(base="http://127.0.0.1:59991/v1", timeout=2)
        with self.assertRaises(gateway.LLMError) as cm:
            p.complete([{"role": "user", "content": "x"}])
        self.assertIn(cm.exception.code, ("E_LLM_UNREACHABLE", "E_LLM_TIMEOUT"))


class TestProviderSwitch(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("AGENTLAB_LLM", None)
        gateway.reset_cache()

    def test_default_is_mock(self):
        gateway.reset_cache()
        self.assertTrue(gateway.get_provider().is_mock, "默认必须是 mock，不能偷偷连模型")

    def test_switch_to_local(self):
        gateway.reset_cache()
        os.environ["AGENTLAB_LLM"] = "local"
        p = gateway.get_provider()
        self.assertFalse(p.is_mock)
        self.assertIsInstance(p, gateway.LocalProvider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
