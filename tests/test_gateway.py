# -*- coding: utf-8 -*-
"""AGENT-PLUG-01：模型无关热插拔 + 协议自适应 的回归测试。

零网络：用自定义 Provider 桩模拟「原生 tool_calls 模型 / 纯文本模型 /
格式先坏后好」，验证 Agent 侧适配逻辑；不依赖任何真实模型。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402
import autonomous_agent as AA                               # noqa: E402
import tool_registry as TR                                  # noqa: E402


class NativeToolsProvider(GW.Provider):
    """声明支持原生工具，直接返回 tool_calls。"""
    name = "stub-native-tools"
    supports_tools = True

    def __init__(self, calls):
        self.calls = calls
        self.i = 0

    def complete(self, messages, temperature=0.0, max_tokens=256):
        return "", 1, self.name

    def complete_ex(self, messages, *, tools=None, temperature=0.0,
                    max_tokens=256):
        assert tools, "原生工具模型应收到 tools 规格"
        c = self.calls[self.i] if self.i < len(self.calls) else None
        self.i += 1
        if c is None:
            return GW.ToolCallReply("", 1, self.name, [])
        return GW.ToolCallReply("", 1, self.name, [c])


class TextJsonProvider(GW.Provider):
    """不支持原生工具，只能吐文本 JSON。"""
    name = "stub-text-json"
    supports_tools = False

    def __init__(self, texts):
        self.texts = list(texts)
        self.i = 0

    def complete(self, messages, temperature=0.0, max_tokens=256):
        t = self.texts[self.i] if self.i < len(self.texts) else None
        self.i += 1
        return t, 1, self.name


class TestProviderSpec(unittest.TestCase):
    def test_spec_from_env_role_routing(self):
        os.environ["EVOGUARD_MODEL_PLANNER_MODEL"] = "planner-m"
        os.environ["EVOGUARD_MODEL_EXECUTOR_MODEL"] = "executor-m"
        self.addCleanup(os.environ.pop, "EVOGUARD_MODEL_PLANNER_MODEL", None)
        self.addCleanup(os.environ.pop, "EVOGUARD_MODEL_EXECUTOR_MODEL", None)
        self.assertEqual(
            GW.ProviderSpec.from_env(role="planner").model, "planner-m")
        self.assertEqual(
            GW.ProviderSpec.from_env(role="executor").model, "executor-m")

    def test_register_custom_provider(self):
        class Dummy(GW.Provider):
            name = "dummy"
            def __init__(self, spec): self.spec = spec
            def complete(self, messages, temperature=0.0, max_tokens=256):
                return "ok", 1, "dummy"
        GW.register_provider("dummy_backend", lambda sp: Dummy(sp))
        GW.reset_cache()
        p = GW.get_provider(force="dummy_backend")
        self.assertEqual(p.complete([{"role": "user", "content": "hi"}])[0], "ok")

    def test_cache_key_includes_model(self):
        GW.reset_cache()
        a = GW.get_provider(force="local",
                            spec=GW.ProviderSpec(backend="local", model="m1"))
        b = GW.get_provider(force="local",
                            spec=GW.ProviderSpec(backend="local", model="m2"))
        self.assertIsNot(a, b, "不同模型不应共用同一缓存实例")

    def test_api_key_not_in_info_or_repr(self):
        os.environ["MY_KEY_ENV"] = "sk-secret-should-not-leak"
        self.addCleanup(os.environ.pop, "MY_KEY_ENV", None)
        spec = GW.ProviderSpec(backend="openai_compat",
                               base_url="http://x/v1", model="m",
                               api_key_env="MY_KEY_ENV")
        p = GW.OpenAICompatProvider(spec)
        blob = repr(p.info()) + repr(spec.describe()) + repr(p.__dict__)
        self.assertNotIn("sk-secret-should-not-leak", blob,
                         "API key 泄漏到了 info/describe/属性")


class TestAgentProtocolAdaptation(unittest.TestCase):
    def test_native_tool_calls_used(self):
        reg = TR.ToolRegistry()
        reg.from_json_schema("kb.search", {"type": "object",
            "properties": {"query": {"type": "string"}}}, source="json_schema")
        reg.declare_agent("react", ["kb.search"], replace=True)
        prov = NativeToolsProvider([
            {"id": "c1", "name": "kb.search", "args": {"query": "x"}}])
        a = AA.ReActAgent(provider=prov, registry=reg,
                          executor=lambda t, x: {"ok": 1},
                          agent_type="react", enforce=False, max_steps=1)
        out = a.run()
        # 原生 tool_calls 被转换成下游 {tool,args} 并执行
        self.assertTrue(any(s.get("tool") == "kb.search" for s in out["steps"]),
                        "原生 tool_calls 未被识别")

    def test_text_json_fallback(self):
        reg = TR.ToolRegistry()
        reg.from_json_schema("kb.search", {"type": "object",
            "properties": {"query": {"type": "string"}}}, source="json_schema")
        reg.declare_agent("react", ["kb.search"], replace=True)
        prov = TextJsonProvider([
            '{"thought":"","tool":"kb.search","args":{"query":"x"}}'])
        a = AA.ReActAgent(provider=prov, registry=reg,
                          executor=lambda t, x: {"ok": 1},
                          agent_type="react", enforce=False, max_steps=1)
        out = a.run()
        self.assertTrue(any(s.get("tool") == "kb.search" for s in out["steps"]))

    def test_format_repair_retries_once(self):
        reg = TR.ToolRegistry()
        reg.from_json_schema("kb.search", {"type": "object",
            "properties": {"query": {"type": "string"}}}, source="json_schema")
        reg.declare_agent("react", ["kb.search"], replace=True)
        # 第一次坏格式，第二次修复成功
        prov = TextJsonProvider([
            "这不是 JSON，我来解释一下……",
            '{"thought":"","tool":"kb.search","args":{"query":"y"}}'])
        a = AA.ReActAgent(provider=prov, registry=reg,
                          executor=lambda t, x: {"ok": 1},
                          agent_type="react", enforce=False, max_steps=1)
        out = a.run()
        self.assertTrue(any(s.get("tool") == "kb.search" for s in out["steps"]),
                        "格式自修复未生效")

    def test_repetition_detected(self):
        junk = "一样的话" * 40
        self.assertTrue(AA._detect_repetition(junk))
        self.assertFalse(AA._detect_repetition("正常一句话，没有重复。"))

    def test_repetition_returns_none(self):
        reg = TR.ToolRegistry()
        reg.from_json_schema("kb.search", {"type": "object",
            "properties": {"query": {"type": "string"}}}, source="json_schema")
        reg.declare_agent("react", ["kb.search"], replace=True)
        prov = TextJsonProvider(["重复重复" * 60])
        a = AA.ReActAgent(provider=prov, registry=reg,
                          executor=lambda t, x: {"ok": 1},
                          agent_type="react", enforce=False, max_steps=2)
        out = a.run()
        self.assertEqual(out["stopped_by"], "no_parseable_model_output")


if __name__ == "__main__":
    unittest.main()
