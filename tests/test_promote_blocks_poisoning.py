# -*- coding: utf-8 -*-
"""SkillPoison 对策：晋升必须拒绝「任务成功但执行越权」的技能。

论文 SkillPoison (arXiv:2610.07645)：技能可以靠「让任务成功」通过验证，
却在执行中做有害副作用，靠此把有害行为固化成持久技能。对策是在**晋升
门禁**就看越权，而不是等上线后 monitor 才发现（那时已经用了一段时间）。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                 # noqa: E402
import skills as SK                                         # noqa: E402


def _cases(n, prefix):
    return [{"task_id": "%s%d" % (prefix, i), "agent_type": "react",
             "fault_type": "none", "fault_chain": [], "fault_target": 0,
             "seed": 7} for i in range(n)]


class TestPromotionBlocksPoisoning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="poison-")
        self.store = core.Store(os.path.join(self.tmp.name, "p.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def _run(self, unauthorized_cand=0, unauthorized_guard=0):
        skill = {"id": "s_p", "name": "c", "status": "candidate", "version": 1,
                 "body": "def f(): pass", "tags": []}
        def run_case(case, use_skill):
            if case.get("category") == "guard":
                return {"success": True,
                        "unauthorized": unauthorized_guard if use_skill else 0}
            return {"success": bool(use_skill),
                    "unauthorized": unauthorized_cand if use_skill else 0}
        tgt = _cases(8, "t")
        for c in tgt:
            c["category"] = "target"
        grd = _cases(3, "g")
        for c in grd:
            c["category"] = "guard"
        return SK.evaluate(self.store, skill, tgt, grd, run_case,
                           protocol="v2")

    def test_success_but_unauthorized_is_rejected(self):
        ev = self._run(unauthorized_cand=2)
        self.assertEqual(ev["verdict"], "REJECT", ev)
        self.assertTrue(any("越权" in r for r in ev["reasons"]), ev["reasons"])
        # 8 条 target 每条 2 次 -> 累计 16；断言意图是「只要有越权就拒」
        self.assertEqual(ev["candidate_unauthorized"], 16, ev["candidate_unauthorized"])

    def test_guard_side_unauthorized_also_rejected(self):
        ev = self._run(unauthorized_guard=1)
        self.assertEqual(ev["verdict"], "REJECT", ev)

    def test_clean_success_still_promotes(self):
        ev = self._run()
        self.assertEqual(ev["verdict"], "PROMOTE", ev)
        self.assertEqual(ev["candidate_unauthorized"], 0)


if __name__ == "__main__":
    unittest.main()


class TestAutoWiring(unittest.TestCase):
    """真实路径：评测脚本只返回 run_id，越权记录在轨迹里，门禁要自动读到。

    这是本项目历史反复踩的坑（「测试绿但功能为零」）：如果门禁只认
    run_case 显式返回的 unauthorized，实际评测不会返回，门禁就是死代码。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="auto-")
        self.store = core.Store(os.path.join(self.tmp.name, "a.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_unauthorized_in_trace_blocks_promotion(self):
        skill = {"id": "s_auto", "name": "c", "status": "candidate",
                 "version": 1, "body": "def f(): pass", "tags": []}
        counters = {"n": 0}

        def run_case(case, use_skill):
            if not use_skill:
                return False
            counters["n"] += 1
            exp = self.store.create_experiment(
                "auto", "react", case["task_id"], "none", 0, 7, "C")
            run = self.store.create_run(exp["id"], "C", 7)
            rid = run["id"]            # create_run 返回 dict，不是 id 字符串
            # 生产格式：ctx.py 写 kind='security' + status='error'
            self.store.add_step(rid, 0, "security", "deny",
                                {"tool": "order.settle"}, {"why": "blocked"},
                                status="error", error_type="approval_required")
            return {"success": True, "run_id": rid}   # 不显式报 unauthorized

        tgt = _cases(8, "t")
        for c in tgt:
            c["category"] = "target"
        grd = _cases(3, "g")
        for c in grd:
            c["category"] = "guard"
        ev = SK.evaluate(self.store, skill, tgt, grd, run_case, protocol="v2")
        self.assertEqual(ev["verdict"], "REJECT", ev)
        self.assertGreater(ev["candidate_unauthorized"], 0,
                           "门禁没读到轨迹里的越权记录（等于死代码）")
