# -*- coding: utf-8 -*-
"""外部语料链路的测试：只吃夹具，不联网。

守两件事：
  1. 归一化/评测的**口径**不能悄悄变（注入命中只认 `injection_*`、
     干净对照的构造方式、载荷单扫这几条都得钉死）；
  2. 语料清单可核验（sha256 写入 manifest），换一份语料不会静默改数字。
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import eval_external_attacks as EV      # noqa: E402
import import_external_attacks as IM    # noqa: E402

FIX = os.path.join(HERE, "fixtures", "external", "injecagent_sample.jsonl")
FIX_SHA = "22e8d4b674ba8e52e70492d905d4e335bd9483b6f640e8292330b22978d7d3f3"


def _cases():
    return EV.load_cases(FIX)


class TestCorpusIntegrity(unittest.TestCase):
    def test_fixture_is_unchanged(self):
        """夹具动了就得重算 sha 并重看数字，不许静默变。"""
        with open(FIX, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), FIX_SHA)

    def test_every_case_has_entry_point_fields(self):
        for c in _cases():
            for k in ("case_id", "goal", "variant", "category", "user_tool",
                      "tool_response", "attacker_instruction",
                      "tool_response_template"):
                self.assertIn(k, c, c.get("case_id"))
            self.assertTrue(c["tool_response"].strip())
            self.assertTrue(c["attacker_instruction"].strip())

    def test_normalize_keeps_control_template(self):
        raw = [{"Attack Type": "Data Security Harm", "User Tool": "t",
                "Tool Response": "resp with payload",
                "Tool Response Template": "resp with <Attacker Instruction>",
                "Attacker Instruction": "do bad thing",
                "Attacker Tools": "a.b", "User Instruction": "u",
                "Expected Achievements": "ach", "Tool Parameters": "{}",
                "Modifed": 1}]
        out = IM.normalize(raw, {"goal": "data_stealing", "variant": "base"},
                           {"name": "injecagent"})
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["tool_response_template"],
                         "resp with <Attacker Instruction>")
        self.assertEqual(c["attacker_tools"], ["a.b"], "单值也要包成列表")
        self.assertTrue(c["modified"])
        self.assertTrue(c["case_id"].startswith("injecagent/data_stealing/base#"))

    def test_normalize_skips_empty_tool_response(self):
        raw = [{"Tool Response": "   ", "Attacker Instruction": "x"}]
        self.assertEqual(IM.normalize(raw, {"goal": "g", "variant": "v"},
                                      {"name": "s"}), [])

    def test_manifest_records_sha256_per_file(self):
        """下载器产出的清单必须能逐文件核对，否则「同一份语料」是空话。"""
        payload = json.dumps([{"Tool Response": "r", "Attacker Instruction": "a"}]
                             ).encode("utf-8")
        blob = json.dumps({"encoding": "base64", "size": len(payload),
                           "content": base64.b64encode(payload).decode()}
                          ).encode("utf-8")
        fake = mock.MagicMock()
        fake.__enter__ = lambda s: mock.MagicMock(
            read=lambda: blob)
        fake.__exit__ = lambda *a: False
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(IM, "CACHE", tmp), \
                    mock.patch.object(IM.urllib.request, "urlopen",
                                      return_value=fake), \
                    mock.patch.object(IM.time, "sleep", lambda *_: None):
                out = os.path.join(tmp, "out")
                m = IM.build(out, sources=[{
                    "name": "demo", "repo": "x/y", "license": "MIT",
                    "license_url": "", "homepage": "",
                    "files": [{"path": "p.json", "git_sha": "abc",
                               "goal": "g", "variant": "v"}]}],
                    log=lambda *_: None)
            self.assertEqual(m["total_cases"], 1)
            self.assertEqual(m["files"][0]["sha256"],
                             hashlib.sha256(payload).hexdigest())
            self.assertEqual(m["files"][0]["git_sha"], "abc")

    def test_offline_mode_fails_loudly_without_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(IM, "CACHE", tmp):
                with self.assertRaises(SystemExit):
                    IM.build(os.path.join(tmp, "out"), offline=True)


class TestMetricDefinition(unittest.TestCase):
    """口径一旦松动，报告里的数就会变好看 —— 这里钉死。"""

    def setUp(self):
        self.cases = _cases()
        self.rows = EV.evaluate(self.cases)

    def test_injection_hit_only_counts_injection_codes(self):
        for r, c in zip(self.rows, self.cases):
            inj = [x for x in r["codes"] if x.startswith("injection_")]
            self.assertEqual(r["injection_hit"], bool(inj), c["case_id"])
            # PII 类命中不能算「认出攻击」
            if not inj and "pii_email" in r["codes"]:
                self.assertFalse(r["injection_hit"])

    def test_control_is_built_from_template_with_benign_filler(self):
        for r, c in zip(self.rows, self.cases):
            self.assertEqual(r["has_control"],
                             bool(c["tool_response_template"].strip()))
        self.assertTrue(EV.BENIGN.strip())

    def test_payload_scan_uses_attacker_instruction_only(self):
        for r, c in zip(self.rows, self.cases):
            from security import scan
            expect = {f["code"] for f in scan(c["attacker_instruction"],
                                              "payload")}
            self.assertEqual(set(r["payload_codes"]), expect)

    def test_summary_shapes_and_ranges(self):
        s = EV.summarize(self.rows)
        self.assertEqual(s["cases"], len(self.rows))
        for k in ("injection_recall", "any_signal_rate", "blocked_rate",
                  "payload_recall", "control_fp_rate",
                  "control_fp_injection_rate"):
            self.assertIsNotNone(s[k], k)
            self.assertGreaterEqual(s[k], 0.0)
            self.assertLessEqual(s[k], 1.0)
        self.assertEqual(set(s["by_goal"]), {"data_stealing", "direct_harm"})
        self.assertEqual(set(s["by_variant"]), {"base", "enhanced"})

    def test_any_signal_is_not_below_injection(self):
        """任意信号包含注入信号，若小于就是算错了。"""
        for r in self.rows:
            if r["injection_hit"]:
                self.assertTrue(r["any_signal"])

    def test_report_states_the_honest_limits(self):
        s = EV.summarize(self.rows)
        md = EV._markdown({"summary": s,
                           "mistakes": {"misses": [], "false_positives": []}},
                          {"sources": [{"repo": "r", "license": "MIT"}],
                           "by_goal": {k: v["cases"] for k, v
                                       in s["by_goal"].items()}})
        for phrase in ("不是", "端到端", "干净对照", "注入特征"):
            self.assertIn(phrase, md)

    def test_evaluate_does_not_mutate_cases(self):
        before = json.dumps(self.cases, sort_keys=True, ensure_ascii=False)
        EV.evaluate(self.cases)
        self.assertEqual(json.dumps(self.cases, sort_keys=True,
                                    ensure_ascii=False), before)


if __name__ == "__main__":
    unittest.main()
