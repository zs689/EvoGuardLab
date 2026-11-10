# -*- coding: utf-8 -*-
"""§8 的收口：CI 读 `security_effects`，只认「判了拒绝却仍然执行」。

报告说「检测到 N 条」不等于「拦住了」。这条链路把真实执行点的后果接进流水线：
`decision=deny / encode / wrap` 而 `executed=1`，说明说得和做得不一致，
必须先停下来查。测试守着：这种情形一定阻断，别的计数不许顺手改判定。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard_inspect"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ci_policy               # noqa: E402
import core                    # noqa: E402
import export_security_effects as EX  # noqa: E402
import security as S           # noqa: E402

GOOD = {
    "verdict": "PASS",
    "dimensions": {"reliability": {"verdict": "PASS"},
                   "security": {"verdict": "PASS"},
                   "operability": {"verdict": "PASS"}},
    "dimension_coverage": {"security": {"coverage_ok": True},
                           "reliability": {"coverage_ok": True},
                           "operability": {"coverage_ok": True}},
}


def _eff(decision="deny", executed=True, **kw):
    row = {"run_id": "r1", "where": "tool_call", "tool": "http.send",
           "decision": decision, "executed": executed, "attempted": True,
           "effect": "unchanged", "reason": "x"}
    row.update(kw)
    return row


class TestSummarize(unittest.TestCase):
    def test_counts(self):
        s = ci_policy.summarize_effects([
            _eff(),                              # deny + executed → 1
            _eff(decision="encode"),             # encode + executed → 2
            _eff(decision="deny", executed=False),   # 只判不执行 → 不算
            _eff(decision="report", executed=False),
        ])
        self.assertEqual(s["rows"], 4)
        self.assertEqual(s["executed"], 2)
        self.assertEqual(s["denied_but_executed"], 2)
        self.assertEqual(s["reported_only"], 1)
        self.assertEqual(len(s["examples"]), 2)

    def test_empty_is_zero_not_error(self):
        s = ci_policy.summarize_effects([])
        self.assertEqual(s["rows"], 0)
        self.assertEqual(s["denied_but_executed"], 0)
        self.assertEqual(s["examples"], [])


class TestDecision(unittest.TestCase):
    def test_denied_but_executed_blocks_even_when_report_passes(self):
        d = ci_policy.evaluate(GOOD, effects=[_eff()])
        self.assertEqual(d["ci_action"], "block")
        self.assertEqual(d["exit_code"], 3)
        self.assertEqual(d["report_verdict"], "PASS", "报告判定不许被改写")
        self.assertTrue(any("未授权执行" in r for r in d["reasons"]))
        self.assertEqual(d["security_effects"]["denied_but_executed"], 1)

    def test_denied_and_not_executed_does_not_block(self):
        d = ci_policy.evaluate(GOOD, effects=[_eff(executed=False)])
        self.assertEqual(d["ci_action"], "pass")
        self.assertEqual(d["security_effects"]["denied_but_executed"], 0)

    def test_policy_can_disable_the_rule_but_must_be_explicit(self):
        d = ci_policy.evaluate(GOOD, {"block_on_unauthorized_execution": False},
                               effects=[_eff()])
        self.assertEqual(d["ci_action"], "pass")
        self.assertEqual(d["security_effects"]["denied_but_executed"], 1,
                         "关掉规则也要照样统计出来")

    def test_no_effects_input_keeps_old_behavior(self):
        d = ci_policy.evaluate(GOOD)
        self.assertEqual(d["ci_action"], "pass")
        self.assertIsNone(d["security_effects"])


class TestEndToEnd(unittest.TestCase):
    """从真实 SQLite 记录 → 导出 jsonl → 流水线决策，一条链走通。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eff_")
        self.db = os.path.join(self.dir, "t.db")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))

    def _write(self, store):
        S.record_effect(store, "run_1", "tool_call",
                        {"detected": True, "decision": "deny",
                         "attempted": True},
                        tool="http.send",
                        override={"executed": True, "effect": "unchanged"})
        S.record_effect(store, "run_1", "tool_output",
                        {"detected": True, "decision": "deny"},
                        tool="kb.search",
                        override={"executed": False, "effect": "unchanged"})

    def test_export_then_block(self):
        store = core.Store(self.db)
        self._write(store)
        store.close()
        out = os.path.join(self.dir, "eff.jsonl")
        r = EX.export(self.db, out)
        self.assertEqual(r["rows"], 2)
        self.assertEqual(r["denied_but_executed"], 1)
        rows = ci_policy.load_effects(out)
        self.assertEqual(len(rows), 2)
        d = ci_policy.evaluate(GOOD, effects=rows)
        self.assertEqual(d["ci_action"], "block")

    def test_export_missing_db_fails_loudly(self):
        with self.assertRaises(SystemExit):
            EX.export(os.path.join(self.dir, "nope.db"),
                      os.path.join(self.dir, "x.jsonl"))

    def test_exported_jsonl_is_stable(self):
        store = core.Store(self.db)
        self._write(store)
        store.close()
        a, b = os.path.join(self.dir, "a.jsonl"), os.path.join(self.dir, "b.jsonl")
        EX.export(self.db, a)
        EX.export(self.db, b)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())

    def test_clean_run_exports_empty_list(self):
        store = core.Store(self.db)
        store.close()
        out = os.path.join(self.dir, "none.jsonl")
        r = EX.export(self.db, out)
        self.assertEqual(r["rows"], 0)
        self.assertEqual(json.loads(json.dumps(ci_policy.load_effects(out))), [])


if __name__ == "__main__":
    unittest.main()
