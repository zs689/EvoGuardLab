# -*- coding: utf-8 -*-
"""E01/E02 验收：声明式条件技能 + 外部确定性执行器 + 配对验证骨架。"""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import conditional_skill as CS  # noqa: E402

POLICY = {
    "allowed_tools": ["kb.search_all", "kb.tier_lookup", "order.get_order"],
    "write_tools": ["order.settle"],
}


def _skill(**kw):
    s = CS.example_skill()
    s.update(kw)
    return s


class TestSchema(unittest.TestCase):
    def test_example_skill_is_accepted(self):
        d = CS.accept(CS.example_skill(), POLICY)
        self.assertTrue(d["accepted"])
        self.assertEqual(d["schema_version"], CS.SCHEMA_VERSION)
        self.assertEqual(len(d["fingerprint"]), 64)

    def test_missing_required_field_is_rejected(self):
        s = CS.example_skill()
        del s["fallback"]
        with self.assertRaises(CS.SkillRejected) as cm:
            CS.accept(s, POLICY)
        self.assertTrue(any("fallback" in e for e in cm.exception.errors))

    def test_forbidden_privilege_fields_are_rejected(self):
        for key in ("evaluation_criteria", "approval_status",
                    "set_allowed_tools", "code"):
            s = CS.example_skill()
            s[key] = "anything"
            with self.assertRaises(CS.SkillRejected) as cm:
                CS.accept(s, POLICY)
            self.assertTrue(any(key in e for e in cm.exception.errors), key)

    def test_code_markers_are_rejected(self):
        s = CS.example_skill()
        s["note"] = "import os; exec(payload)"
        with self.assertRaises(CS.SkillRejected) as cm:
            CS.accept(s, POLICY)
        self.assertTrue(any("代码特征" in e for e in cm.exception.errors))

    def test_nested_forbidden_key_is_rejected(self):
        s = CS.example_skill()
        s["decision_params"] = {"evidence_selector": "first",
                                "nested": {"exec": "x"}}
        with self.assertRaises(CS.SkillRejected):
            CS.accept(s, POLICY)

    def test_unregistered_decision_param_is_rejected(self):
        s = _skill(decision_params={"evidence_selector": "first",
                                    "arbitrary": 1})
        with self.assertRaises(CS.SkillRejected):
            CS.accept(s, POLICY)

    def test_unknown_selector_is_rejected(self):
        s = _skill(decision_params={"evidence_selector": "magic"})
        with self.assertRaises(CS.SkillRejected):
            CS.accept(s, POLICY)

    def test_tool_declaration_must_be_subset_of_frozen_policy(self):
        s = _skill(allowed_tools=["kb.search_all", "shell.exec"])
        with self.assertRaises(CS.SkillRejected) as cm:
            CS.accept(s, POLICY)
        self.assertTrue(any("越权" in e for e in cm.exception.errors))

    def test_skill_cannot_declare_write_tools(self):
        s = _skill(allowed_tools=["order.settle"])
        with self.assertRaises(CS.SkillRejected) as cm:
            CS.accept(s, POLICY)
        self.assertTrue(any("写工具" in e for e in cm.exception.errors))

    def test_fingerprint_is_stable_and_sensitive(self):
        a = CS.fingerprint(CS.example_skill())
        b = CS.fingerprint(copy.deepcopy(CS.example_skill()))
        self.assertEqual(a, b)
        s = _skill(name="改名")
        self.assertNotEqual(a, CS.fingerprint(s))


class TestExecution(unittest.TestCase):
    def test_max_version_selects_newest_evidence(self):
        ctx = {"evidence": [{"version": 1, "text": "旧版"},
                            {"version": 3, "text": "新版"},
                            {"version": 2, "text": "中间"}], "steps": 1}
        r = CS.execute(CS.example_skill(), ctx, POLICY)
        self.assertEqual(r["action"], "use_evidence")
        self.assertEqual(r["selected"]["version"], 3)
        self.assertFalse(r["fallback_used"])
        self.assertEqual(r["tools_allowed"], CS.example_skill()["allowed_tools"])

    def test_declared_order_selects_first_declared_rule(self):
        s = _skill(decision_params={"evidence_selector": "declared_order",
                                    "order_by": ["R-B", "R-A"], "max_steps": 6})
        ctx = {"evidence": [{"rule_id": "R-A", "version": 9, "text": "a"},
                            {"rule_id": "R-B", "version": 1, "text": "b"}],
               "steps": 1}
        r = CS.execute(s, ctx, POLICY)
        self.assertEqual(r["selected"]["rule_id"], "R-B",
                         "按声明的业务顺序，不是按版本号")

    def test_missing_required_evidence_uses_fallback(self):
        ctx = {"evidence": [{"version": 2}], "steps": 1}   # 缺 text
        r = CS.execute(CS.example_skill(), ctx, POLICY)
        self.assertTrue(r["fallback_used"])
        self.assertEqual(r["action"], "abstain")
        self.assertIsNone(r["selected"])

    def test_no_evidence_uses_fallback(self):
        r = CS.execute(CS.example_skill(), {"evidence": [], "steps": 1}, POLICY)
        self.assertTrue(r["fallback_used"])

    def test_max_steps_triggers_stop(self):
        ctx = {"evidence": [{"version": 2, "text": "x"}], "steps": 99}
        r = CS.execute(CS.example_skill(), ctx, POLICY)
        self.assertTrue(r["stopped"])
        self.assertTrue(r["fallback_used"])

    def test_execute_revalidates_permissions(self):
        """执行前再校验：策略收紧后，同一个技能不能绕过校验继续执行。"""
        s = CS.example_skill()
        strict = {"allowed_tools": ["kb.tier_lookup"], "write_tools": []}
        with self.assertRaises(CS.SkillRejected):
            CS.execute(s, {"evidence": [{"version": 1, "text": "x"}], "steps": 1},
                       strict)


class TestPairedCheck(unittest.TestCase):
    def _cases(self, successes, **extra):
        out = []
        for i, ok in enumerate(successes):
            c = {"key": "case_%d" % i, "success": ok}
            c.update(extra)
            out.append(c)
        return out

    def test_promote_when_gain_and_no_unauthorized(self):
        before = self._cases([False, False, True, True])
        after = self._cases([True, True, True, True])
        r = CS.paired_check(before, after)
        self.assertEqual(r["verdict"], "PROMOTE")
        self.assertEqual(r["gain"], 0.5)
        self.assertEqual(r["paired_cases"], 4)

    def test_unauthorized_operation_overrides_business_gain(self):
        before = self._cases([False, False])
        after = self._cases([True, True], unauthorized=True)
        r = CS.paired_check(before, after)
        self.assertEqual(r["verdict"], "REJECT")
        self.assertEqual(r["gain"], 1.0)
        self.assertIn("业务收益不能抵消", r["reason"])

    def test_negative_transfer_is_rejected(self):
        before = self._cases([True, True, False])
        after = self._cases([False, False, True])
        r = CS.paired_check(before, after, max_negative_transfer=0.1)
        self.assertEqual(r["verdict"], "REJECT")
        self.assertIn("负迁移", r["reason"])

    def test_no_stable_gain_allows_reject_and_negative_result(self):
        before = self._cases([True, True])
        after = self._cases([True, True])
        r = CS.paired_check(before, after, min_gain=0.1)
        self.assertEqual(r["verdict"], "REJECT")
        self.assertIn("负结果", r["reason"])

    def test_no_paired_cases_is_needs_review_with_null(self):
        r = CS.paired_check([{"key": "a", "success": False}],
                            [{"key": "b", "success": True}])
        self.assertEqual(r["verdict"], "NEEDS_REVIEW")
        self.assertIsNone(r["gain"])
        self.assertIsNone(r["negative_transfer_rate"])


if __name__ == "__main__":
    unittest.main()
