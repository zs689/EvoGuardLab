# -*- coding: utf-8 -*-
"""runtime_context.Ctx 的纯逻辑单测：逻辑步、故障注入、预算。

拆出 Ctx 是为了让 runtime.py 变薄，但拆完只有经由 runtime 的间接覆盖 ——
而 `_fault_at` 用**逻辑步号**而不是绝对 seq 判定，是「恢复期重放还能打中
同一个故障」的前提。这条语义错了，整个自愈闭环会静默失效（故障不再注入，
评测里所有策略都会「莫名有效」）。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import runtime_common as RC                                # noqa: E402
import runtime_context as RCON                             # noqa: E402


def ctx(exp=None, **kw):
    """只测纯逻辑：store/run_id 传占位，不触碰持久化。"""
    return RCON.Ctx(None, "run-x", dict(exp or {}), **kw)


class TestFaultSpecs(unittest.TestCase):
    def test_single_fault_legacy_shape(self):
        specs = RC._fault_specs({"fault_type": "tool_timeout",
                                 "fault_target": 2})
        self.assertEqual(specs, [{"type": "tool_timeout", "target": 2}])

    def test_chain_takes_precedence(self):
        specs = RC._fault_specs({
            "fault_type": "tool_timeout", "fault_target": 1,
            "fault_chain": [{"type": "wrong_tool", "target": 1},
                            {"type": "none", "target": 3}]})
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[1]["type"], "none")

    def test_none_means_no_specs(self):
        self.assertEqual(RC._fault_specs({"fault_type": "none"}), [])
        self.assertEqual(RC._fault_specs({}), [])


class TestLogicalStep(unittest.TestCase):
    def test_next_increments_both(self):
        c = ctx(start_seq=1)
        # __init__ 里 seq = start_seq - 1，首次 _next 先加 1 再返回
        self.assertEqual(c._next(), 1)
        self.assertEqual((c.seq, c.logical), (1, 1))
        self.assertEqual(c._next(), 2)
        self.assertEqual((c.seq, c.logical), (2, 2))

    def test_budget_counts_logical_not_absolute_seq(self):
        """预算按本轮逻辑步算：绝对 seq 会跨轮累积，用它会第 3 轮一开局就超限。"""
        c = ctx(start_seq=500)                # 绝对序号很大，但逻辑步从 0 起
        for _ in range(3):
            c._next()
        self.assertEqual(c.logical, 3)
        self.assertEqual(c.seq, 502)

    def test_budget_exceeded_raises(self):
        c = ctx()
        c.logical = RC.MAX_STEPS
        with self.assertRaises(Exception) as e:
            c._next()
        # ApiError 的 str 只给 message，错误码在属性上
        self.assertEqual(getattr(e.exception, "code", ""), "E_BUDGET_EXCEEDED",
                         str(e.exception))


class TestFaultAt(unittest.TestCase):
    def test_hits_matching_logical_step_only(self):
        c = ctx({"fault_type": "tool_timeout", "fault_target": 2})
        self.assertIsNone(c._fault_at(1))
        self.assertEqual(c._fault_at(2), "tool_timeout")
        self.assertIsNone(c._fault_at(3))

    def test_suppressed_step_is_skipped(self):
        c = ctx({"fault_type": "tool_timeout", "fault_target": 2},
                suppress={2})
        self.assertIsNone(c._fault_at(2),
                          "被策略抑制的逻辑步不能再注入故障")

    def test_transient_fault_only_fires_once(self):
        """瞬时故障第 2 次尝试自然恢复 —— 这是「重试就能救」的物理基础。"""
        c = ctx({"fault_type": "tool_timeout", "fault_target": 2})
        self.assertEqual(c._fault_at(2), "tool_timeout")
        c.attempt = 2
        self.assertIsNone(c._fault_at(2))

    def test_persistent_fault_keeps_firing(self):
        """持久故障不会因为重试就消失，必须靠策略修。"""
        c = ctx({"fault_type": "wrong_tool", "fault_target": 2})
        c.attempt = 5
        self.assertEqual(c._fault_at(2), "wrong_tool")

    def test_plain_none_type_is_never_fault(self):
        c = ctx({"fault_chain": [{"type": "none", "target": 1}]})
        self.assertIsNone(c._fault_at(1))

    def test_multiple_faults_on_different_steps(self):
        c = ctx({"fault_chain": [{"type": "wrong_tool", "target": 1},
                                 {"type": "tool_timeout", "target": 3}]})
        self.assertEqual(c._fault_at(1), "wrong_tool")
        self.assertIsNone(c._fault_at(2))
        self.assertEqual(c._fault_at(3), "tool_timeout")

    def test_replay_hits_same_logical_step(self):
        """恢复重放时绝对 seq 往后走，但同一逻辑步仍要命中 —— 用 seq 判会打偏。"""
        c1 = ctx({"fault_type": "wrong_tool", "fault_target": 2})
        c1._next(); c1._next()
        self.assertEqual(c1._fault_at(c1.logical), "wrong_tool")
        c2 = ctx({"fault_type": "wrong_tool", "fault_target": 2},
                 start_seq=900, attempt=2)
        c2._next(); c2._next()
        self.assertEqual(c2.seq, 901)
        self.assertEqual(c2.logical, 2)
        self.assertEqual(c2._fault_at(c2.logical), "wrong_tool")


if __name__ == "__main__":
    unittest.main()
