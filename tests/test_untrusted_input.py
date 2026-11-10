# -*- coding: utf-8 -*-
"""不可信输入的入口硬化：技能候选、CI 策略、结构化契约。

这些东西的来源本身就不可信 —— 技能是「模型提出的候选」，策略是手写 JSON，
契约参数来自 Agent 调用。不合格的输入必须**干净地拒绝**（抛受控异常或
fail-closed 阻断），不能抛 AttributeError/TypeError 把执行器带崩，
更不能因为「字段漏配」就悄悄放行。

发现 bug 修实现，不改测试放水。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in ("ai-service", "ai-service/evoguard", "ai-service/evoguard_inspect"):
    sys.path.insert(0, os.path.join(ROOT, p))

import ci_policy               # noqa: E402
import conditional_skill as CS  # noqa: E402
import tool_contract as TC      # noqa: E402

POLICY = {"allowed_tools": ["kb.search", "kb.search_all", "kb.tier_lookup",
                            "order.settle"],
          "write_tools": ["order.settle"]}

# 一个合规的保障报告骨架（判定 PASS、三维覆盖齐全）
GOOD_REPORT = {
    "verdict": "PASS",
    "dimensions": {"reliability": {"verdict": "PASS"},
                   "security": {"verdict": "PASS"},
                   "operability": {"verdict": "PASS"}},
    "dimension_coverage": {
        "reliability": {"coverage_ok": True},
        "security": {"coverage_ok": True},
        "operability": {"coverage_ok": True},
    },
}


class TestSkillCandidateHardening(unittest.TestCase):
    """技能候选来自模型，形态完全不可控。"""

    def test_non_object_skill_is_rejected_not_crashed(self):
        for bad in (None, [], "str", 0, 3.5, (1, 2), b"{}"):
            with self.assertRaises(CS.SkillRejected) as cm:
                CS.accept(bad, POLICY)
            self.assertTrue(cm.exception.errors)

    def test_execute_rejects_non_object_before_touching_it(self):
        with self.assertRaises(CS.SkillRejected):
            CS.execute(None, {"evidence": [], "steps": 1}, POLICY)

    def test_missing_write_tools_in_policy_still_blocks_write_tools(self):
        """策略漏写 write_tools，不能成为技能拿写权限的缺口。"""
        s = CS.example_skill()
        s["allowed_tools"] = ["order.settle"]          # 已登记为写工具
        slim = {"allowed_tools": ["order.settle"]}     # 没有 write_tools
        with self.assertRaises(CS.SkillRejected) as cm:
            CS.accept(s, slim)
        self.assertTrue(any("写工具" in e for e in cm.exception.errors))

    def test_external_and_arbitrary_side_effects_are_not_skill_tools(self):
        for tool in ("http.send", "shell.exec"):
            s = CS.example_skill()
            s["allowed_tools"] = [tool]
            with self.assertRaises(CS.SkillRejected):
                CS.accept(s, {"allowed_tools": [tool]})

    def test_nested_forbidden_key_is_rejected(self):
        s = CS.example_skill()
        s["decision_params"] = {"evidence_selector": "max_version",
                                "order_by": ["a"],
                                "max_steps": 3}
        s["fallback"] = {"action": "abstain", "hook": {"code": "print(1)"}}
        with self.assertRaises(CS.SkillRejected) as cm:
            CS.accept(s, POLICY)
        self.assertTrue(any("code" in e for e in cm.exception.errors))

    def test_code_marker_inside_string_is_rejected(self):
        s = CS.example_skill()
        s["name"] = "按版本选证据 import os"
        with self.assertRaises(CS.SkillRejected):
            CS.accept(s, POLICY)

    def test_schema_version_mismatch_rejected(self):
        s = CS.example_skill()
        s["schema_version"] = "conditional-skill-v0"
        with self.assertRaises(CS.SkillRejected):
            CS.accept(s, POLICY)


class TestCiPolicyHardening(unittest.TestCase):
    """策略是手写 JSON：写错必须阻断，不能崩、也不能悄悄放行。"""

    def test_wrong_types_block_instead_of_crashing(self):
        bads = [
            {"allow_verdicts": None},
            {"allow_verdicts": "PASS"},
            {"allow_missing_config": "config:allowed_tools"},
            {"allow_limited": "false"},          # 字符串不是布尔
            {"require_security_coverage": 0},
            {"require_all_dimension_coverage": "yes"},
            {"version": "1"},
            {"protocol": None},
        ]
        for bad in bads:
            d = ci_policy.evaluate(GOOD_REPORT, bad)
            self.assertEqual(d["ci_action"], "block", "策略 %r 未被阻断" % bad)
            self.assertTrue(d["policy_problems"], "策略 %r 未列出问题" % bad)
            self.assertEqual(d["exit_code"], 3)
            self.assertEqual(d["report_verdict"], "PASS", "报告判定被策略改写")

    def test_default_policy_still_passes_clean_report(self):
        d = ci_policy.evaluate(GOOD_REPORT)
        self.assertEqual(d["ci_action"], "pass")
        self.assertEqual(d["policy_problems"], [])

    def test_invalid_report_verdict_is_blocked(self):
        for v in (None, "", "OK", 1, ["PASS"]):
            d = ci_policy.evaluate(dict(GOOD_REPORT, verdict=v))
            self.assertEqual(d["ci_action"], "block", "判定 %r 未被阻断" % (v,))

    def test_missing_dimension_coverage_does_not_crash(self):
        for data in ({"verdict": "PASS"},
                     {"verdict": "PASS", "dimension_coverage": None},
                     {"verdict": "PASS", "dimension_coverage": {"security": None}}):
            d = ci_policy.evaluate(data)
            self.assertIn(d["ci_action"], ("pass", "review", "block"))


class TestToolContractHardening(unittest.TestCase):
    """契约参数来自 Agent 调用，类型不可控。"""

    def test_policy_missing_keys_does_not_crash(self):
        for pol in (None, {}, {"allowed_tools": None}, {"allowed_tools": "kb.search"}):
            r = TC.authorize("support-agent", "kb.search", {}, mode="enforce",
                             policy=pol)
            self.assertIn(r["decision"], ("allow", "deny", "report"))

    def test_contract_table_missing_tool_is_reported_not_crashed(self):
        r = TC.authorize("support-agent", "nope.tool", {}, mode="enforce",
                         contracts={}, policy=POLICY)
        self.assertEqual(r["decision"], "deny")
        self.assertFalse(r["contract_known"])

    def test_nan_and_infinite_amount_cannot_pass_cap(self):
        for amount in (float("nan"), float("inf"), -float("inf")):
            r = TC.authorize("order", "order.settle",
                             {"order_id": "A1001", "total": amount,
                              "approval_request_id": "appr_x"},
                             mode="enforce", policy=POLICY)
            self.assertEqual(r["decision"], "deny",
                             "金额 %r 居然通过了上限检查" % amount)


if __name__ == "__main__":
    unittest.main()
