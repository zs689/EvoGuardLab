# -*- coding: utf-8 -*-
"""core_state 状态机与错误码的独立测试。

状态转移表漏一条，运行就会卡在某个状态永远推进不下去（比如 FAILED 不能
回到 RECOVERING）。这类 bug 在 happy-path 测试里完全看不见。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core_state as CS                                    # noqa: E402


class TestStateMachine(unittest.TestCase):
    def test_transitions_cover_all_states(self):
        self.assertEqual(set(CS.TRANSITIONS), set(CS.STATES),
                         "有状态没定义转移（会 KeyError）")

    def test_transition_targets_are_known_states(self):
        bad = {s: sorted(t - set(CS.STATES))
               for s, t in CS.TRANSITIONS.items() if t - set(CS.STATES)}
        self.assertFalse(bad, "转移到未知状态：%s" % (bad,))

    def test_terminal_states_have_no_outgoing_edges(self):
        for s in CS.TERMINAL:
            with self.subTest(state=s):
                self.assertEqual(CS.TRANSITIONS[s], set(),
                                 "%s 是终态却还能转移" % s)

    def test_nonterminal_states_can_progress(self):
        stuck = [s for s in CS.STATES
                 if s not in CS.TERMINAL and not CS.TRANSITIONS[s]]
        self.assertEqual(stuck, [], "这些非终态没有出边（会卡死）：%s" % (stuck,))

    def test_failed_can_recover(self):
        self.assertIn(CS.RECOVERING, CS.TRANSITIONS[CS.FAILED],
                      "FAILED 无法进入恢复 = 自愈链路断了")

    def test_terminal_members_are_valid(self):
        self.assertTrue(CS.TERMINAL <= set(CS.STATES))
        self.assertEqual(len(CS.TERMINAL), 2)

    def test_states_unique(self):
        self.assertEqual(len(CS.STATES), len(set(CS.STATES)))


class TestErrors(unittest.TestCase):
    def test_api_error_carries_code_and_http(self):
        e = CS.ApiError("E_BAD_REQUEST", "bad", 422)
        self.assertEqual(e.code, "E_BAD_REQUEST")
        self.assertEqual(e.message, "bad")
        self.assertEqual(e.http, 422)
        self.assertIn("E_BAD_REQUEST", str(e))

    def test_empty_message_falls_back_to_registry(self):
        e = CS.ApiError("E_NOT_FOUND")
        self.assertEqual(e.message, CS.ERR["E_NOT_FOUND"])

    def test_all_documented_codes_have_text(self):
        for code in ("E_BAD_REQUEST", "E_NOT_FOUND", "E_INVALID_STATE",
                     "E_BUDGET_EXCEEDED", "E_INTERNAL"):
            with self.subTest(code=code):
                self.assertTrue(CS.ERR.get(code), "%s 没有错误文案" % code)


if __name__ == "__main__":
    unittest.main()
