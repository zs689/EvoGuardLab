# -*- coding: utf-8 -*-
"""§5 回归：授权由结构化契约决定，正则不承担授权决策。"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import tool_contract as TC  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "tool_contract_demo", os.path.join(ROOT, "scripts", "tool_contract_demo.py"))
DEMO = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DEMO)

POLICY = {"allowed_tools": ["kb.search", "order.get_order", "order.settle"],
          "write_tools": ["order.settle"]}


def auth(tool, args, agent="support-agent", mode="enforce", **kw):
    return TC.authorize(agent, tool, args, mode=mode, policy=POLICY, **kw)


class TestStructuralAuthorization(unittest.TestCase):
    def test_regex_hit_does_not_revoke_a_valid_contract(self):
        """反例 1：文本里有危险字样，正则命中，但结构化检查全过 → allow。"""
        r = auth("order.get_order",
                 {"order_id": "A1001", "note": "备注里写着 rm -rf / 请不要执行"})
        self.assertEqual(r["decision"], "allow")
        self.assertTrue(r["regex_signals"], "正则应该命中")
        self.assertTrue(r["regex_only"])
        self.assertFalse(r["structural_only"])

    def test_no_regex_hit_but_structural_violation_denies(self):
        """反例 2：正则零命中，域名/身份越界 → deny。"""
        r = auth("http.send", {"url": "http://127.0.0.1:9/collect",
                               "body": "订单明细"})
        self.assertEqual(r["decision"], "deny")
        self.assertEqual(r["regex_signals"], [])
        self.assertTrue(r["structural_only"])
        self.assertIn("allowed_domain", " ".join(x["rule"] for x in r["checks"]))

    def test_amount_over_cap_is_denied_without_any_regex_signal(self):
        r = auth("order.settle",
                 {"order_id": "A1001", "total": 99999.0,
                  "approval_request_id": "appr_x"}, agent="order")
        self.assertEqual(r["decision"], "deny")
        self.assertTrue(any(x["rule"] == "max_amount" and not x["ok"]
                            for x in r["checks"]))

    def test_unknown_identity_denied_in_enforce(self):
        r = auth("kb.search", {"query": "x", "doc_id": "KB-01"}, agent="nobody")
        self.assertEqual(r["decision"], "deny")
        self.assertTrue(any(x["rule"] == "known_identity" and not x["ok"]
                            for x in r["checks"]))

    def test_unknown_tool_contract_denied(self):
        r = auth("mystery.tool", {})
        self.assertEqual(r["decision"], "deny")
        self.assertFalse(r["contract_known"])

    def test_audit_mode_reports_but_does_not_deny(self):
        r = auth("http.send", {"url": "http://127.0.0.1:9/collect"},
                 mode="audit")
        self.assertEqual(r["decision"], "report")
        self.assertTrue(r["reasons"], "留原因但不拦")

    def test_write_requires_approval_request_id(self):
        r = auth("order.settle", {"order_id": "A1001", "total": 100.0},
                 agent="order")
        self.assertEqual(r["decision"], "deny")
        self.assertTrue(any(x["rule"] == "approval_request_id" and not x["ok"]
                            for x in r["checks"]))

    def test_resource_id_format_is_checked(self):
        bad = auth("order.get_order", {"order_id": "BAD-ID"})
        self.assertEqual(bad["decision"], "deny")
        good = auth("order.get_order", {"order_id": "A1001"})
        self.assertEqual(good["decision"], "allow")

    def test_writable_path_and_method_are_checked(self):
        bad = auth("order.settle",
                   {"order_id": "A1001", "total": 100.0,
                    "approval_request_id": "a", "path": "/etc/passwd",
                    "method": "delete"}, agent="order")
        rules = {x["rule"] for x in bad["checks"] if not x["ok"]}
        self.assertIn("writable_path", rules)
        self.assertIn("method", rules)

    def test_normal_read_is_allowed(self):
        r = auth("kb.search", {"query": "退货时限", "doc_id": "KB-01"})
        self.assertEqual(r["decision"], "allow")
        self.assertEqual(r["reasons"], [])

    def test_regex_signals_never_change_decision(self):
        """同一组合约下，加不加危险字样，decision 必须一样。"""
        clean = auth("order.get_order", {"order_id": "A1001"})
        noisy = auth("order.get_order",
                     {"order_id": "A1001", "note": "rm -rf /"})
        self.assertEqual(clean["decision"], noisy["decision"])
        self.assertEqual(len(noisy["regex_signals"]) > 0, noisy["regex_only"])

    def test_demo_all_cases_pass(self):
        out = tempfile.mkdtemp(prefix="tc_demo_")
        with contextlib.redirect_stdout(io.StringIO()):
            d = DEMO.main(["--out", out])
        self.assertEqual(d["passed"], d["total"])
        self.assertEqual(d["total"], 7)
        with open(os.path.join(out, "demo.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["passed"], 7)


if __name__ == "__main__":
    unittest.main()
