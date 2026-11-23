# -*- coding: utf-8 -*-
"""legacy_diagnose 的模块级单测（纯逻辑，不需要 runtime 的整套 fixture）。

这块是"故障归因"的心脏：定位错了，恢复期就抑制错步骤，重放必然再犯同一个
错。原先只有经由 runtime 的间接覆盖，`_overlap` / `_recompute` / 退化定位
这些纯函数没有单独锁定。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import legacy_diagnose as LD                               # noqa: E402


def step(seq, name="order.get_order", kind="tool", status="ok",
         logical=None, output=None):
    return {"seq": seq, "logical": logical if logical is not None else seq,
            "kind": kind, "name": name, "status": status, "input": {},
            "output": output if output is not None else {"ok": True}}


class TestOverlap(unittest.TestCase):
    def _q(self, q, t, n=2):
        return LD._overlap(q, t, n)

    def test_short_query_returns_true(self):
        """查询太短时无法判断，按"不算答非所问"处理（宁缺勿滥）。"""
        self.assertTrue(self._q("好", "完全无关的回答"))

    def test_overlapping_ngram(self):
        self.assertTrue(self._q("订单金额是多少", "订单金额为 861.5 元"))

    def test_no_overlap(self):
        self.assertFalse(self._q("订单金额是多少", "今天天气不错"))

    def test_non_chinese_is_ignored(self):
        """只比中文 n-gram：纯英文两边都为空 -> 走短查询分支。"""
        self.assertTrue(self._q("order total", "the order total is 861"))

    def test_mixed_text_compares_chinese_part_only(self):
        self.assertTrue(self._q("金额 total", "金额 861.5"))
        self.assertFalse(self._q("金额 total", "数量 3 件"))


class TestRecompute(unittest.TestCase):
    def test_discount_applies_above_threshold(self):
        items = [{"price": 100, "qty": 9}]        # 900 >= 800
        subtotal, total = LD._recompute(
            items, {"rate": 0.1, "threshold": 800})
        self.assertEqual(subtotal, 900.0)
        self.assertAlmostEqual(total, 810.0, places=2)

    def test_no_discount_below_threshold(self):
        items = [{"price": 100, "qty": 3}]        # 300 < 800
        subtotal, total = LD._recompute(
            items, {"rate": 0.1, "threshold": 800})
        self.assertEqual((subtotal, total), (300.0, 300.0))

    def test_multiple_items_summed(self):
        items = [{"price": 99.5, "qty": 2}, {"price": 10, "qty": 1}]
        subtotal, _ = LD._recompute(items, {"rate": 0.2, "threshold": 10000})
        self.assertAlmostEqual(subtotal, 209.0, places=2)


class TestDiagnoseLocalization(unittest.TestCase):
    def test_errored_step_is_localized(self):
        steps = [step(1), step(2, status="error", output={"error": "timeout"}),
                 step(3)]
        d = LD.diagnose(steps)
        self.assertEqual(d["first_bad_step"], 2)
        self.assertEqual(d["first_bad_logical"], 2)

    def test_logical_and_seq_can_differ(self):
        """故障真值按逻辑步号记；报告里两者都要对上。"""
        steps = [step(11, logical=1), step(12, logical=2, status="error",
                                           output={"error": "deny"}),
                 step(13, logical=3)]
        d = LD.diagnose(steps)
        self.assertEqual(d["first_bad_step"], 12)
        self.assertEqual(d["first_bad_logical"], 2)

    def test_unknown_falls_back_to_last_tool_step(self):
        """定位不到时必须退化成最后一个数据步骤，否则抑制点丢失。"""
        steps = [step(1), step(2), {"seq": 3, "logical": 3, "kind": "answer",
                                    "name": "answer", "status": "ok",
                                    "output": {"text": "x"}}]
        d = LD.diagnose(steps)
        self.assertEqual(d["first_bad_step"], 2,
                         "退化定位应取最后一个 tool/replay 步骤")
        self.assertEqual(d["first_bad_logical"], 2)

    def test_empty_steps_do_not_crash(self):
        d = LD.diagnose([])
        self.assertIn("root_cause", d)
        self.assertEqual(d["first_bad_step"], 0)


class TestInferAgentType(unittest.TestCase):
    def test_infers_from_tool_prefix(self):
        steps = [step(1, name="order.get_order"), step(2, name="order.settle")]
        self.assertEqual(LD._infer_agent_type(steps), "order")

    def test_unknown_when_no_tool_steps(self):
        self.assertEqual(LD._infer_agent_type([]), "unknown")


class TestDecisionTableIntegrity(unittest.TestCase):
    """两张表的**跨表一致性**：诊断出来了却取不到策略 = 那类故障永远救不回。

    这个缺口真实存在过：4 个 `INV-PLAN-*` 映射到 `incorrect_planning`，但
    `STRATEGY_BY_CAUSE` 里没有这个 key —— runtime 侧靠
    `.get(cause, "human_takeover")` 兜住了行为，表上却看不见这个决定。
    """

    def test_every_invariant_cause_has_a_declared_strategy(self):
        missing = [(inv, cause) for inv, cause in LD.INVARIANT_TO_CAUSE.items()
                   if cause not in LD.STRATEGY_BY_CAUSE]
        self.assertEqual(missing, [],
                         "这些不变量诊断出来后没有显式策略：%s" % (missing,))

    def test_strategy_names_are_from_known_set(self):
        """策略名拼错会静默退化成 human_takeover，等于悄悄放弃自动恢复。"""
        known = {"retry", "replay_checkpoint", "fallback_tool", "cross_verify",
                 "reformat", "human_takeover"}
        bad = [(c, s) for c, s in LD.STRATEGY_BY_CAUSE.items()
               if s not in known]
        self.assertEqual(bad, [], "未知策略名：%s" % (bad,))

    def test_invariant_ids_follow_naming_convention(self):
        bad = [i for i in LD.INVARIANT_TO_CAUSE if not i.startswith("INV-")]
        self.assertEqual(bad, [])

    def test_every_invariant_maps_to_a_known_cause(self):
        causes = set(LD.STRATEGY_BY_CAUSE)
        unknown = {i: c for i, c in LD.INVARIANT_TO_CAUSE.items()
                   if c not in causes}
        self.assertEqual(unknown, {})


if __name__ == "__main__":
    unittest.main()
