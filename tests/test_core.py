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
  7. 策略链：链可存可取、复合故障单策略救不回而组合能救回
  8. 流程不变量：首尾颠倒能检出、正常流程不误报
  9. 对抗性验证：有害候选技能会伤到 C，但挡在 D 之外
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


class TestChain(unittest.TestCase):
    """策略链不是装饰：要能存、能取，且单策略确实救不回复合故障。"""

    def test_as_chain_normalizes_all_legacy_forms(self):
        self.assertEqual(core.as_chain(["retry", "cross_verify"]),
                         ["retry", "cross_verify"])
        self.assertEqual(core.as_chain('["retry","cross_verify"]'),
                         ["retry", "cross_verify"])
        self.assertEqual(core.as_chain("cross_verify"), ["cross_verify"])
        self.assertEqual(core.as_chain('"cross_verify"'), ["cross_verify"])
        self.assertEqual(core.as_chain(None), [])

    def test_chain_skill_roundtrip(self):
        s = fresh()
        skills.ensure_schema(s)
        sk = {"id": "sk_chain", "name": "组合", "task_type": "order",
              "fault_type": "missing_field",
              "strategy": ["fallback_tool", "cross_verify"],
              "required_tools": [], "safety": {"max_retry": 2},
              "status": skills.CANDIDATE, "version": 1, "created_at": 0}
        skills.save(s, sk)
        skills.promote(s, sk["id"])
        got = skills.lookup(s, "order", "missing_field", "D")
        self.assertIsNotNone(got)
        self.assertEqual(core.as_chain(got["strategy"]),
                         ["fallback_tool", "cross_verify"],
                         "组合链落库后必须能完整取回，不能被压成单策略")

    def test_composite_needs_chain(self):
        """复合故障：最优单策略全灭，两步链能救回。"""
        s = fresh()
        skills.ensure_schema(s)
        c = sim.composite_cases("test")[0]
        at = c["agent_type"]

        def run(chain):
            exp = s.create_experiment("t", at, c["task_id"], c["fault_type"], 0, 7, "D")
            exp["fault_chain"] = c["fault_chain"]
            runtime.FORCED_SKILL = {"id": "probe", "task_type": at,
                                    "strategy": chain}
            try:
                return runtime.run_once(s, exp, "D")["success"] == 1
            finally:
                runtime.FORCED_SKILL = None

        singles = ("retry", "cross_verify", "fallback_tool",
                   "reformat", "replay_checkpoint")
        self.assertFalse(any(run([st]) for st in singles),
                         "任何单策略都不该救回复合故障，否则策略链没有增量")
        self.assertTrue(run(["fallback_tool", "cross_verify"]),
                        "两步策略链必须能逐个拔掉两个坏点")

    def test_repair_requires_diagnosis_authorization(self):
        """修复动作必须受本轮诊断根因授权，不能顺手把沿途故障全修了。"""
        s = fresh()
        c = sim.composite_cases("test")[0]
        exp = s.create_experiment("t", c["agent_type"], c["task_id"],
                                  c["fault_type"], 0, 7, "D")
        exp["fault_chain"] = c["fault_chain"]
        runtime.FORCED_SKILL = {"id": "p", "task_type": c["agent_type"],
                                "strategy": ["cross_verify"]}
        try:
            r = runtime.run_once(s, exp, "D")
        finally:
            runtime.FORCED_SKILL = None
        self.assertEqual(r["success"], 0,
                         "首轮根因不在 cross_verify 覆盖内时不该越权修复")


class TestPlanOrderInvariant(unittest.TestCase):
    """流程不变量：独立于任务真值，检出首尾颠倒且不误报正常流程。"""

    def _steps(self, plan):
        return [{"seq": 1, "logical": 1, "kind": "tool",
                 "name": "task.make_plan", "status": "ok",
                 "output": {"goal_key": "x", "steps": plan}}]

    def test_reversed_plan_detected(self):
        plan = ["收集本周数据", "生成周报草稿", "提交审核"]
        v = invariants.check_all(self._steps(list(reversed(plan))), "planner")
        self.assertIn("INV-PLAN-ORDER", [x["rule_id"] for x in v],
                      "首尾阶段颠倒必须被抓到")

    def test_normal_plan_passes(self):
        plan = ["收集本周数据", "生成周报草稿", "提交审核"]
        self.assertEqual(invariants.check_all(self._steps(plan), "planner"), [])

    def test_known_blind_spot_is_honest(self):
        # 纯执行型流程首尾都没有阶段信号 —— 这是已知盲区，
        # 测试把它固定下来，防止以后有人偷偷把规则改成「一律报错」来刷召回率。
        plan = ["创建账号", "分配设备"]
        self.assertEqual(invariants.check_all(self._steps(list(reversed(plan))),
                                              "planner"), [])


class TestAdversarialGovernance(unittest.TestCase):
    """对抗性验证：塞一个有害候选技能，C 会吃到、D 必须挡住。

    这是「严格验证有用」的正面证据。四组对照里 C=D 时，
    光看成功率说不清治理价值，必须主动投毒来测。
    """

    def test_harmful_candidate_hurts_C_not_D(self):
        s = fresh()
        skills.ensure_schema(s)
        # 有害技能：声称修 missing_field，实际直接转人工（必然失败）
        bad = {"id": "sk_bad", "name": "有害技能", "task_type": "order",
               "fault_type": "missing_field", "strategy": "human_takeover",
               "required_tools": [], "safety": {}, "status": skills.CANDIDATE,
               "version": 9, "created_at": 0}
        skills.save(s, bad)

        # 用**单故障**：D 不依赖技能库也能靠规则表救回，
        # 这样 C/D 的差距就纯粹来自「有没有吃到有害候选」，没有别的干扰。
        c = {"agent_type": "order", "task_id": "A1001",
             "fault_type": "missing_field", "seed": 7}

        def run(mode):
            exp = s.create_experiment("t", c["agent_type"], c["task_id"],
                                      c["fault_type"], 0, 7, mode)
            runtime.FORCED_SKILL = None
            return runtime.run_once(s, exp, mode)["success"] == 1

        # C 的宽松检索（只看 agent）会命中这个 candidate
        self.assertIsNotNone(skills.lookup(s, "order", "missing_field", "C"),
                             "C 必须能看到未验证候选，否则退化实验不成立")
        # D 只认 active + 精确匹配，投毒的 candidate 进不来
        self.assertIsNone(skills.lookup(s, "order", "missing_field", "D"),
                          "D 绝不能用到未经独立验证的候选技能")
        self.assertFalse(run("C"), "投毒后 C 应当失败")
        self.assertTrue(run("D"), "D 不该被有害候选技能影响")


if __name__ == "__main__":
    unittest.main(verbosity=2)
