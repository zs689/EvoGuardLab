# -*- coding: utf-8 -*-
"""EVO-DEEP-01-T1：环境规约变更时，旧规约必须退役而不是新旧并列。

第一性原理：真实业务规约会演变（分页从 1-based 改成 0-based）。若只在
列表里追加，注入时两条互相矛盾的规则会同时给模型，等于教它自相矛盾。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                 # noqa: E402
import skill_synthesis as SS                                # noqa: E402
import skills                                               # noqa: E402


def _trace(stderr, seq=1):
    return {"seq": seq, "logical": seq, "name": "code.run_in_sandbox",
            "status": "error", "error_type": "AssertionError",
            "output": {"stderr": stderr}}


class TestInvariantDrift(unittest.TestCase):
    def setUp(self):
        self.store = core.Store(os.path.join(
            tempfile.mkdtemp(prefix="drift-"), "x.db"))
        out = SS.synthesize(
            self.store,
            [_trace("page index must start at 1, got 0")],
            task_type="pagination", require_info_gain=True)
        self.sid = out["skill"]["id"]
        skills.save(self.store, out["skill"])

    def test_slot_key_is_stable_across_versions(self):
        a = SS._slot_key("pagination", "分页从 1 开始")
        b = SS._slot_key("pagination", "分页从 0 开始")
        self.assertEqual(a, b, "同一件事的不同版本必须落同一个槽位")

    def test_reversal_supersedes_old_rule(self):
        r = SS.refine_skill(
            self.store, self.sid,
            [_trace("page index must start at 0, got 1")])
        self.assertTrue(r["accepted"]["accepted"], r["accepted"])
        self.assertTrue(r["superseded"], "0-based 规约必须让 1-based 退役")
        active = SS.active_invariants(r["skill"])
        rules = " ".join(x["rule"] for x in active)
        self.assertIn("从 0 开始", rules)
        self.assertNotIn("从 1 开始", rules,
                         "旧规约仍在活跃集里 -> 注入时会自相矛盾")

    def test_snapshot_keeps_superseded_rule_for_rollback(self):
        r = SS.refine_skill(
            self.store, self.sid,
            [_trace("page index must start at 0, got 1")])
        snap = r["skill"]
        old = [x for x in snap["env_invariants"] if x.get("superseded_by")]
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]["superseded_by"], snap["version"])
        # 完整快照仍保留旧规则原文，回滚才有意义
        self.assertIn("从 1 开始", old[0]["rule"])

    def test_unrelated_slot_is_not_superseded(self):
        r = SS.refine_skill(
            self.store, self.sid,
            [_trace("limit exceeds 37 must raise ValueError")])
        self.assertTrue(r["accepted"]["accepted"])
        self.assertFalse(r["superseded"],
                         "不同槽位（limit 上限）不该让分页起点规约退役")
        self.assertEqual(len(SS.active_invariants(r["skill"])), 2)


if __name__ == "__main__":
    unittest.main()
