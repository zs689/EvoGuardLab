# -*- coding: utf-8 -*-
"""网关防静默降级：force=<preset 名> 必须解析出预设 base，不落本地。

修前：get_provider(force="deepseek") 走到 else->LocalProvider，连
127.0.0.1:1234；调用失败后判官静默回退规则，报告里却写着 deepseek。
实测教训见 scripts/provider_from_preset.py 文档。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402


class TestPresetResolve(unittest.TestCase):
    def setUp(self):
        GW.reset_cache()

    def tearDown(self):
        GW.reset_cache()

    def test_force_preset_name_uses_preset_base(self):
        """force='deepseek' 必须走 OpenAI 兼容 + 预设 base。"""
        p = GW.get_provider(force="deepseek")
        self.assertEqual(p.name, "openai_compat", p.name)
        self.assertEqual(p.base, "https://api.deepseek.com/v1", p.base)

    def test_force_unknown_backend_still_works(self):
        """未知名保持旧行为（当 backend 名），不因新增解析而崩。"""
        p = GW.get_provider(force="local")
        self.assertEqual(p.base.rstrip("/"),
                         (GW.ProviderSpec().base_url or
                          os.environ.get("AGENTLAB_LLM_BASE") or
                          "http://127.0.0.1:1234/v1").rstrip("/"))

    def test_explicit_spec_wins_over_preset(self):
        """显式 spec 的 base 不能被 preset 覆盖。"""
        sp = GW.ProviderSpec(backend="openai_compat",
                             base_url="https://my.example/v1", model="x")
        p = GW.get_provider(force="deepseek", spec=sp)
        self.assertEqual(p.base, "https://my.example/v1", p.base)

    def test_preset_resolves_model_default(self):
        p = GW.get_provider(force="deepseek")
        self.assertTrue(p.model, "预设应带默认模型")
        self.assertIn("deepseek", str(p.model).lower())


if __name__ == "__main__":
    unittest.main()
