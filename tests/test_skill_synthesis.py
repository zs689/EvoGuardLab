# -*- coding: utf-8 -*-
"""WS-04-T2：失败→诊断→合成→独立验证→晋升/回滚闭环（新领域）。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import conditional_skill as CS                              # noqa: E402
import core                                                 # noqa: E402
import skill_synthesis as SS                                # noqa: E402
import skills                                               # noqa: E402


def newdomain_cases(prefix, n=3, domain="file"):
    """全新领域任务：不在 evolution_bench 里硬编码。"""
    return [{"case_id": "%s-%s-%d" % (prefix, domain, i),
             "agent_type": "newdomain", "task_id": "%s-%s-%d" % (prefix, domain, i),
             "family_id": "%s-%d" % (domain, i),  # 每条是独立任务家族
             "fault_type": "tool_timeout", "seed": 7}
            for i in range(n)]


class TestClosedLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "s.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.steps = [
            {"seq": 1, "logical": 1, "name": "file.read", "status": "error",
             "error_type": "timeout", "output": None},
            {"seq": 2, "logical": 2, "name": "http.fetch", "status": "ok",
             "output": {"docs": ["ok"]}},
        ]

    def test_synthesis_rejects_write_tool_declaration(self):
        s = SS.synthesize(self.store, self.steps, task_type="newdomain",
                          policy={"allowed_tools": ["file.read"],
                                  "write_tools": []})
        # 候选默认声明 3 个只读工具，超出 policy 的 1 个 → 门禁拒绝
        self.assertFalse(s["accepted"]["accepted"])

    def test_full_loop_promotes_when_gain_and_no_regression(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")
        self.assertTrue(s["accepted"]["accepted"], s["accepted"])

        def runner(case, skill):
            # 目标组：baseline 失败、candidate 成功 → 增益。
            # guard 组：baseline 与 candidate 都成功 → 零负迁移可估计。
            if case["task_id"].startswith("grd"):
                return True
            return skill is not None

        out = SS.evaluate_and_promote(
            self.store, s["skill"], target, guard, runner,
            training_cases=training)
        self.assertTrue(out["promoted"])
        self.assertEqual(out["status"], "active")
        self.assertFalse(out["rolled_back"])

    def test_bad_skill_is_rejected_and_rolled_back(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")

        def runner(case, skill):
            # guard 组：baseline 成功、带 skill 反而失败 → 负迁移
            if case["task_id"].startswith("grd"):
                return skill is None
            return skill is not None

        out = SS.evaluate_and_promote(
            self.store, s["skill"], target, guard, runner,
            training_cases=training)
        self.assertFalse(out["promoted"])
        self.assertEqual(out["evaluation"]["verdict"], "REJECT")
        self.assertEqual(out["status"], "REJECT")

    def test_training_overlap_is_rejected(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")
        overlap = [training[0]] + target[1:]
        with self.assertRaises(ValueError):
            SS.evaluate_and_promote(
                self.store, s["skill"], overlap, guard,
                lambda c, sk: True, training_cases=training)

    def test_closed_loop_end_to_end(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        out = SS.closed_loop(
            self.store, self.steps,
            target_cases=target, guard_cases=guard,
            training_cases=training,
            run_case=lambda c, sk: (True if c["task_id"].startswith("grd")
                                    else sk is not None),
            task_type="newdomain")
        self.assertEqual(out["stage"], "promotion")
        self.assertTrue(out["promotion"]["promoted"])

    def test_describe_lists_new_domain(self):
        d = SS.describe()
        self.assertIn("sql.query", d["tools"])
        self.assertIn("independent_eval", d["stages"])


if __name__ == "__main__":
    unittest.main()
