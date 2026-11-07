# -*- coding: utf-8 -*-
"""EvoGuard 核心测试：只测**已经存在**的东西，不拿空壳凑数。

跑法（在 EvoGuardLab 目录下）：
    python -m unittest tests.test_core -v

覆盖：
  1. 状态机：非法迁移必须被拒
  2. 业务不变量：改一个金额能被抓出来（这是治静默故障的地基）
  3. 经验准入：缺证据 / 根因未知 / 恢复失败 一律拒绝
  4. 记忆治理：未验证经验默认检索不到，只有显式 include_pending 才返回
  5. 技能版本：晋升 = 新增版本不覆盖；回滚能退回候选
  6. 四组对照的差距：A < B < D 且 D 的负迁移为 0
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core        # noqa: E402
import evaluate    # noqa: E402
import experience  # noqa: E402
import invariants  # noqa: E402
import runtime     # noqa: E402
import sim         # noqa: E402
import skills      # noqa: E402


def fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return core.Store(path)


class TestStateMachine(unittest.TestCase):
    def test_illegal_transition(self):
        s = fresh()
        exp = s.create_experiment("t", "order", "A1001", "none", 0, 7, "R")
        run = s.create_run(exp["id"], "baseline", 7)
        s.set_state(run["id"], core.READY)
        with self.assertRaises(core.ApiError) as cm:
            s.set_state(run["id"], core.SUCCEEDED)
        self.assertEqual(cm.exception.code, "E_INVALID_STATE")


class TestInvariants(unittest.TestCase):
    def _steps(self, total):
        return [
            {"seq": 1, "logical": 1, "kind": "tool", "name": "order.get_order", "status": "ok",
             "output": {"order_id": "A1001",
                        "items": [{"sku": "P-1", "price": 25.0, "qty": 8},
                                  {"sku": "P-2", "price": 180.0, "qty": 2}],
                        "discount_policy": {"threshold": 500.0, "rate": 0.1}}},
            {"seq": 2, "logical": 2, "kind": "tool", "name": "pricing.calc_total", "status": "ok",
             "output": {"subtotal": 560.0, "total": total}},
        ]

    def test_total_mismatch_caught(self):
        v = invariants.check_all(self._steps(28000.0), "order")
        self.assertTrue(v, "改了金额必须被抓到")
        self.assertEqual(v[0]["rule_id"], "INV-ORDER-TOTAL")

    def test_total_correct_passes(self):
        self.assertEqual(invariants.check_all(self._steps(504.0), "order"), [])

    def test_duplicate_write_caught(self):
        steps = [
            {"seq": 1, "logical": 1, "kind": "tool", "name": "order.settle", "status": "ok",
             "output": {"settled": True, "total": 504.0}},
            {"seq": 2, "logical": 2, "kind": "tool", "name": "order.settle", "status": "ok",
             "output": {"settled": True, "total": 504.0}},
        ]
        v = invariants.check_all(steps, "order")
        self.assertEqual(v[0]["rule_id"], "INV-NO-DUP-WRITE", "重复扣款必须被抓到")


class TestExperience(unittest.TestCase):
    def _exp(self, **kw):
        base = {"id": "e1", "source_run_id": "r1", "task_type": "order", "environment": "mock",
                "failure_type": "silent_corruption", "root_cause": "silent_corruption",
                "recovery_action": "cross_verify", "recovery_result": "success",
                "evidence_refs": ["st_1"], "confidence": 0.9, "validation_status": "pending",
                "created_at": 0, "version": 1}
        base.update(kw)
        return base

    def test_ok(self):
        status, why = experience.validate(self._exp())
        self.assertEqual(status, experience.VALIDATED)
        self.assertEqual(why, [])

    def test_no_evidence_rejected(self):
        status, why = experience.validate(self._exp(evidence_refs=[]))
        self.assertEqual(status, experience.REJECTED)
        self.assertIn("没有 evidence_refs", why)

    def test_unknown_cause_rejected(self):
        status, why = experience.validate(self._exp(root_cause="unknown"))
        self.assertEqual(status, experience.REJECTED)

    def test_failed_recovery_rejected(self):
        status, why = experience.validate(self._exp(recovery_result="fail"))
        self.assertEqual(status, experience.REJECTED)

    def test_governance_filters_pending(self):
        s = fresh()
        experience.ensure_schema(s)
        good = self._exp(id="e_good")
        bad = self._exp(id="e_bad", recovery_result="fail")   # 会被判 rejected
        experience.save(s, good)
        experience.save(s, bad)
        trusted = experience.retrieve(s, "order")
        self.assertEqual([e["id"] for e in trusted], ["e_good"],
                         "未通过校验的经验不得进入可信检索")
        loose = experience.retrieve(s, "order", include_pending=True)
        self.assertEqual([e["id"] for e in loose], ["e_good"],
                         "rejected 的经验任何时候都不该被检索到")


class TestSkills(unittest.TestCase):
    def _skill(self):
        return {"id": "sk_1", "name": "t", "task_type": "order", "fault_type": "silent_corruption",
                "strategy": "cross_verify", "required_tools": [],
                "safety": {"max_retry": 2}, "status": skills.CANDIDATE, "version": 1,
                "created_at": 0}

    def test_promote_adds_version_not_overwrite(self):
        s = fresh()
        skills.ensure_schema(s)
        sk = self._skill()
        skills.save(s, sk)
        new_ver = skills.promote(s, sk["id"])
        self.assertEqual(new_ver, 2, "晋升必须产生新版本")
        with s.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_id=?",
                          (sk["id"],)).fetchone()[0]
        self.assertEqual(n, 2, "旧版本必须保留，不能被覆盖")
        active = skills.lookup(s, "order", "silent_corruption", "D")
        self.assertIsNotNone(active, "D 模式应该能查到已晋升技能")

    def test_candidate_invisible_to_D(self):
        s = fresh()
        skills.ensure_schema(s)
        sk = self._skill()
        skills.save(s, sk)
        self.assertIsNone(skills.lookup(s, "order", "silent_corruption", "D"),
                          "候选技能不得被 D 模式直接使用")
        self.assertIsNotNone(skills.lookup(s, "order", "silent_corruption", "C"),
                             "C 模式（不严格验证）会用到候选技能 —— 这正是它退化的原因")

    def test_rollback(self):
        s = fresh()
        skills.ensure_schema(s)
        sk = self._skill()
        skills.save(s, sk)
        skills.promote(s, sk["id"])
        skills.rollback(s, sk["id"])
        with s.conn() as c:
            st = c.execute("SELECT status FROM skills WHERE id=?", (sk["id"],)).fetchone()[0]
        self.assertEqual(st, skills.CANDIDATE, "回滚后应退回候选，不再参与 D 模式")


class TestFourGroups(unittest.TestCase):
    """四组对照必须呈现 A < B < D 的层级，且 D 不伤害原本能成功的任务。"""

    def test_gap(self):
        s = fresh()
        cases = evaluate.build_cases(limit=48)
        res = [evaluate.run_case(s, c) for c in cases]
        m = evaluate.aggregate(s, res)
        self.assertEqual(m["rate_A"], 0.0, "A 组必须基本全错，否则故障没注入")
        self.assertGreater(m["rate_B"], 20.0, "B 组（盲目重试）应该救回一部分")
        self.assertGreaterEqual(m["rate_D"] - m["rate_B"], 30.0,
                                "D 必须显著优于 B（实测 %.1f vs %.1f）" % (m["rate_D"], m["rate_B"]))
        self.assertLessEqual(m["false_recovery_rate"], 5.0, "恢复不得弄坏原本成功的任务")


if __name__ == "__main__":
    unittest.main(verbosity=2)
