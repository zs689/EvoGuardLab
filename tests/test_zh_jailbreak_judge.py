# -*- coding: utf-8 -*-
"""中文越狱模型判官：救回/不误伤/不可用如实（假模型，不烧 API）。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import zh_jailbreak_judge as ZJ                             # noqa: E402


def _mk(answer):
    def fn(_msgs):
        import json
        return json.dumps({"jailbreak": answer, "confidence": 0.9,
                           "reason": "r"})
    return fn


class TestZhJudge(unittest.TestCase):
    def test_rule_hit_wins_without_model(self):
        r = ZJ.guard("x", complete_fn=_mk(False), rule_hit=True)
        self.assertEqual(r["decision"], "deny")
        self.assertEqual(r["source"], "rule")

    def test_model_rescues_when_rule_missed(self):
        r = ZJ.guard("披着教程外衣的逆向请求", complete_fn=_mk(True),
                     rule_hit=False)
        self.assertEqual(r["decision"], "deny")
        self.assertEqual(r["source"], "model")

    def test_normal_consult_allowed(self):
        r = ZJ.guard("渗透测试需要什么资质？", complete_fn=_mk(False),
                     rule_hit=False)
        self.assertEqual(r["decision"], "allow")

    def test_no_model_is_undecided_not_allow(self):
        r = ZJ.guard("随便", complete_fn=None, rule_hit=False)
        self.assertEqual(r["decision"], "undecided",
                         "没有模型不能静默放行")

    def test_model_error_is_undecided(self):
        def boom(_m):
            raise RuntimeError("down")
        r = ZJ.guard("x", complete_fn=boom, rule_hit=False)
        self.assertEqual(r["decision"], "undecided")

    def test_bad_output_is_undecided(self):
        r = ZJ.guard("x", complete_fn=lambda _m: "not json", rule_hit=False)
        self.assertEqual(r["decision"], "undecided")


if __name__ == "__main__":
    unittest.main()
