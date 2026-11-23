# -*- coding: utf-8 -*-
"""退化监控的 unauthorized 计数：只算真被门禁拒绝的动作。

ctx.py 在**审批通过**后也会写一条 security_effects：
  detected=1, decision='allow', executed=1, effect='approval_consumed'
旧口径 `executed=1 AND effect!='unchanged'` 把这种合法写当成越权，
于是"合法写越多 -> 越容易被误回滚"。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                # noqa: E402
import security as SEC                                     # noqa: E402
import skills as SK                                        # noqa: E402
import skill_synthesis as SS                               # noqa: E402


def _effect(c, rid, eid, decision, effect, tool="order.settle",
            detected=1, executed=1):
    c.execute(
        "INSERT INTO security_effects(id,ts,run_id,where_,tool,detected,"
        "decision,action,attempted,executed,effect,reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, 1.0, rid, "approval_gate", tool, detected, decision,
         "approved" if decision == "allow" else "blocked", 1, executed,
         effect, "test"))


class TestUnauthorizedCount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mon-unauth-")
        self.store = core.Store(os.path.join(self.tmp, "m.db"))
        SK.ensure_schema(self.store)
        SEC.ensure_schema(self.store)
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO runs(id,experiment_id,mode,status,seed,started_at,"
                "success) VALUES('r1','e1','C','done',1,1.0,1)")
        self.sk = {"id": "s1", "task_type": "order"}

    def tearDown(self):
        self.store.close()

    def _stats(self):
        return SS.collect_online_stats(self.store, self.sk, since=0)

    def test_approved_write_is_not_unauthorized(self):
        with self.store.conn() as c:
            _effect(c, "r1", "e1", "allow", "approval_consumed")
        self.assertEqual(self._stats()["unauthorized"], 0)

    def test_denied_executed_is_unauthorized(self):
        with self.store.conn() as c:
            _effect(c, "r1", "e2", "deny", "state_changed", executed=1)
        self.assertEqual(self._stats()["unauthorized"], 1)

    def test_output_encode_block_counts_without_tool_name(self):
        with self.store.conn() as c:
            _effect(c, "r1", "e3", "encode", "body_encoded", tool=None)
        self.assertEqual(self._stats()["unauthorized"], 1)

    def test_report_only_is_not_unauthorized(self):
        with self.store.conn() as c:
            _effect(c, "r1", "e4", "report", "reported_only", detected=0)
        self.assertEqual(self._stats()["unauthorized"], 0)


if __name__ == "__main__":
    unittest.main()
