# -*- coding: utf-8 -*-
"""`evoguard config` 的输出绝不能把凭据打出来。

用户把 config 输出贴到 issue 里是极常见的操作。当前 SECURITY_SWITCHES
里只有 0/1 开关和模型名，还没有凭据键；但只要以后有人往列表里加一个
AGENTLAB_LLM_API_KEY，原样输出的实现就会把密钥打印出去。这里先把
行为锁死：名字像凭据的一律脱敏，不管列表里有什么。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import baseline                                             # noqa: E402


class TestConfigRedaction(unittest.TestCase):
    def test_sensitive_values_are_masked(self):
        env = {
            "AGENTLAB_SECURITY_MODE": "enforce",
            "AGENTLAB_LLM_API_KEY": "sk-" + "fakefakefakefake" + "9999",
            "AGENTLAB_LLM_TOKEN": "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        }
        cfg = baseline.effective_security_config(
            env, extra=("AGENTLAB_LLM_API_KEY", "AGENTLAB_LLM_TOKEN"))
        self.assertEqual(cfg["AGENTLAB_SECURITY_MODE"], "enforce")
        self.assertNotIn("sk-abcdef", cfg["AGENTLAB_LLM_API_KEY"])
        self.assertNotIn("ghp_abc", cfg["AGENTLAB_LLM_TOKEN"])
        self.assertIn("redacted", cfg["AGENTLAB_LLM_API_KEY"].lower())

    def test_unset_still_shows_unset(self):
        cfg = baseline.effective_security_config({})
        self.assertEqual(cfg["AGENTLAB_SECURITY_MODE"], "<unset>")


if __name__ == "__main__":
    unittest.main()
