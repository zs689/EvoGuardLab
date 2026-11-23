# -*- coding: utf-8 -*-
"""gateway 协议层：错误映射与本地后端接线。

跨模型实测已经证明两套协议（OpenAI 兼容 / Ollama 原生）都能跑通；
但**故障时的提示质量**没有单测锁定：把「模型名写错」报成「服务没起来」，
会让人排错排到错误的方向。这里用真实 HTTP 状态码覆盖映射。
"""
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPresetsIntegrity(unittest.TestCase):
    def test_every_preset_has_five_fields(self):
        for name, v in GW.PROVIDER_PRESETS.items():
            with self.subTest(name=name):
                self.assertEqual(len(v), 5, "preset 字段数变了：%s" % name)
                backend, base, key_env, model, tools = v
                self.assertIn(backend, ("openai_compat", "anthropic_compat",
                                        "ollama"))
                self.assertIsInstance(tools, bool)

    def test_local_backends_need_no_key(self):
        for name in ("ollama", "local_openai"):
            self.assertIsNone(GW.PROVIDER_PRESETS[name][2],
                              "%s 不该要求 API key" % name)

    def test_provider_name_maps_to_backend(self):
        """厂商名 != 协议后端名：deepseek 是 openai_compat，ollama 是 ollama。"""
        self.assertEqual(GW.PROVIDER_PRESETS["deepseek"][0], "openai_compat")
        self.assertEqual(GW.PROVIDER_PRESETS["ollama"][0], "ollama")
        self.assertEqual(GW.PROVIDER_PRESETS["qwen"][0], "openai_compat")


class TestOllamaBaseNormalization(unittest.TestCase):
    def test_v1_suffix_is_stripped(self):
        """/v1 是 OpenAI 兼容的路径习惯；原生接口在根路径 /api/*。"""
        p = GW.OllamaNativeProvider(base="http://127.0.0.1:11434/v1")
        self.assertEqual(p.base, "http://127.0.0.1:11434")

    def test_trailing_slash_stripped(self):
        p = GW.OllamaNativeProvider(base="http://host:11434/")
        self.assertEqual(p.base, "http://host:11434")

    def test_json_mode_names_the_provider(self):
        self.assertEqual(GW.OllamaNativeProvider(json_mode=True).name, "ollama")
        self.assertEqual(GW.OllamaNativeProvider(json_mode=False).name,
                         "ollama_raw")


class TestErrorMapping(unittest.TestCase):
    """HTTP 状态码 -> 可执行错误码。写错模型名和写错地址必须能区分。"""

    def _provider_raising(self, code):
        """替换 urlopen（不是 _post）：错误映射就写在 _post 的 except 里，
        换掉 _post 等于把被测逻辑一起换掉了。"""
        p = GW.OllamaNativeProvider(base="http://127.0.0.1:11434",
                                    model="m", timeout=5)
        err = urllib.error.HTTPError("http://x", code, "err", {},
                                     io.BytesIO(b"detail-body"))
        self._patch = mock.patch("urllib.request.urlopen", side_effect=err)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        return p

    def test_400_and_404_mean_model_name(self):
        for code in (400, 404):
            with self.subTest(code=code):
                p = self._provider_raising(code)
                with self.assertRaises(GW.LLMError) as ctx:
                    p.complete([{"role": "user", "content": "x"}])
                self.assertEqual(ctx.exception.code, "E_LLM_MODEL")

    def test_5xx_means_service_down(self):
        for code in (502, 503, 504):
            with self.subTest(code=code):
                p = self._provider_raising(code)
                with self.assertRaises(GW.LLMError) as ctx:
                    p.complete([{"role": "user", "content": "x"}])
                self.assertEqual(ctx.exception.code, "E_LLM_DOWN")

    def test_other_http_codes_are_not_misreported(self):
        p = self._provider_raising(429)
        with self.assertRaises(GW.LLMError) as ctx:
            p.complete([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.code, "E_LLM_HTTP")

    def test_unreachable_is_distinct_from_timeout(self):
        p = GW.OllamaNativeProvider(base="http://127.0.0.1:1", model="m",
                                    timeout=1)

        patch = mock.patch("urllib.request.urlopen",
                           side_effect=urllib.error.URLError("refused"))
        patch.start()
        self.addCleanup(patch.stop)
        with self.assertRaises(GW.LLMError) as ctx:
            p.complete([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.code, "E_LLM_UNREACHABLE")


if __name__ == "__main__":
    unittest.main()
