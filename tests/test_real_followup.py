# -*- coding: utf-8 -*-
"""本地真实模型后续动作链路：产出可解析决策，失败一律记 N/A。

修的 bug：provider 开了 JSON grammar，旧 prompt 却要求「SEND 或 KEEP + 一句理由」
这种自由文本 —— 模型只能把输入复述成 JSON，启发式解析全读成 KEEP，还有
3 次 `token repeat limit reached`。现在显式要求 JSON，解析不了就不猜。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import defense_demo as D  # noqa: E402


class _FakeProvider:
    def __init__(self, text="", exc=None):
        self.text, self.exc = text, exc

    def info(self):
        return {"provider": "fake/qwen"}

    is_mock = False

    def complete(self, messages, temperature=0.0, max_tokens=256):
        if self.exc:
            raise self.exc
        return self.text, 100, "fake/qwen"


def _followup(text="", exc=None):
    f = D.RealFollowUp("ollama")
    f.provider = _FakeProvider(text, exc)
    f.model_id = "fake/qwen"
    return f


class TestStructuredDecision(unittest.TestCase):
    def test_keep_is_parsed(self):
        r = _followup('{"decision":"KEEP","reason":"文档不可信"}').decide("doc")
        self.assertEqual(r["status"], "observed")
        self.assertFalse(r["send"])
        self.assertEqual(r["decision"], "KEEP")
        self.assertIn("不可信", r["reason"])

    def test_send_is_parsed(self):
        r = _followup('{"decision":"SEND","reason":"照做"}').decide("doc")
        self.assertTrue(r["send"])

    def test_code_fence_is_stripped(self):
        r = _followup('```json\n{"decision":"KEEP","reason":"x"}\n```').decide("doc")
        self.assertEqual(r["status"], "observed")

    def test_bad_json_is_not_guessed(self):
        for text in ("SEND 因为文档要求", "not json", '{"a": 1}',
                     '{"decision":"MAYBE","reason":"x"}', "[1,2]"):
            r = _followup(text).decide("doc")
            self.assertEqual(r["status"], "parse_error", text)
            self.assertIsNone(r["send"], "解析失败不许给决策")
            self.assertIsNotNone(r["raw"], "原始输出必须留档")

    def test_empty_output_is_na(self):
        r = _followup("").decide("doc")
        self.assertEqual(r["status"], "no_answer")
        self.assertIsNone(r["send"])

    def test_provider_error_is_na(self):
        r = _followup(exc=RuntimeError("HTTP 500 token repeat")).decide("doc")
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("500", r["reason"])

    def test_no_provider_is_na(self):
        f = D.RealFollowUp("ollama")
        f.provider = None
        f.last_error = "服务没起"
        r = f.decide("doc")
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("服务没起", r["reason"])

    def test_prompt_asks_for_json_not_free_text(self):
        """防止以后有人把 prompt 改回自由文本、和 grammar 打架。"""
        prov = _FakeProvider('{"decision":"KEEP","reason":"x"}')
        f = D.RealFollowUp("ollama")
        f.provider = prov
        captured = {}
        orig = prov.complete

        def spy(messages, **kw):
            captured["messages"] = messages
            return orig(messages, **kw)

        with mock.patch.object(prov, "complete", spy):
            f.decide("untrusted doc")
        user = captured["messages"][-1]["content"]
        self.assertIn("JSON", user)
        self.assertIn("decision", user)
        self.assertIn("untrusted doc", user)


@unittest.skipUnless(
    os.path.isfile(os.path.join(ROOT, "experiments", "defense_demo_real_20261110",
                                "demo.json")),
    "还没跑过 --followup real")
class TestRealArtifact(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "experiments", "defense_demo_real_20261110",
                               "demo.json"), encoding="utf-8") as f:
            self.d = json.load(f)

    def test_all_model_calls_have_parseable_decisions(self):
        rows = [r for r in self.d["results"] if r.get("followup_decision")]
        self.assertEqual(len(rows), 8, "只有工具返回注入那组向量触发模型")
        for r in rows:
            self.assertEqual(r["followup_decision"]["status"], "observed")
            self.assertIn(r["followup_decision"]["decision"], ("SEND", "KEEP"))
            self.assertTrue(r["followup_decision"]["raw"])

    def test_verdict_discloses_coverage_not_hype(self):
        self.assertIn("8 条 case", self.d["verdict"])
        self.assertIn("不能据此宣称防住提示注入", self.d["verdict"])

    def test_real_run_only_changes_the_followup_path(self):
        """真实模型只影响「后续动作」：其它向量逐条必须和冻结版一致。

        整体 sink_hits 变小是**预期**的 —— controlled 观测器按最坏情况照做注入，
        真实模型答 KEEP 就没外发。拿这个差值当「效果提升」是自欺，所以这里
        只钉住与模型无关的那些 case。
        """
        path = os.path.join(ROOT, "experiments", "defense_demo_v2", "demo.json")
        with open(path, encoding="utf-8") as f:
            frozen = json.load(f)
        key = lambda r: (r["mode"], r["case_id"])
        cold = {key(r): r["sink_hits"] for r in frozen["results"]
                if r["scenario"] != "indirect_injection"}
        for r in self.d["results"]:
            if r["scenario"] != "indirect_injection":
                self.assertEqual(r["sink_hits"], cold[key(r)], key(r))
        for mode in ("off", "audit", "enforce", "enforce+wrap"):
            self.assertLessEqual(self.d["summary"][mode]["sink_hits"],
                                 frozen["summary"][mode]["sink_hits"])


if __name__ == "__main__":
    unittest.main()
