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


def validated_promote(store, skill, prefix="p0"):
    """让旧测试也经过真实晋升门禁，而不是绕过它。

    P0 03 之前这三处测试直接 promote，等于假设「调用方会自觉先验证」——
    正是 Word 计划 3.3 指出的漏洞。这里只准备一份满足 paired-v2 门槛的
    独立证据，被测行为（版本不覆盖 / 回滚状态 / 组合链取回）保持不变。
    """
    at, ft = skill["task_type"], skill["fault_type"]
    target = [{"case_id": "%s-t%d" % (prefix, i), "agent_type": at,
               "task_id": "%s-task%d" % (prefix, i), "fault_type": ft, "seed": 7}
              for i in range(3)]
    guard = [{"case_id": "%s-g%d" % (prefix, i), "agent_type": at,
              "task_id": "%s-guard%d" % (prefix, i), "fault_type": "unrelated_fault",
              "seed": 7} for i in range(3)]
    train = [{"case_id": "%s-train" % prefix, "agent_type": at,
              "task_id": "%s-train-task" % prefix, "fault_type": "other", "seed": 7}]

    def runner(case, sk):
        if case["case_id"].startswith(prefix + "-t"):
            return sk is not None       # target：baseline 全败、候选全胜 → 100pp
        return True                     # guard：两侧全胜 → 负迁移 0

    res = skills.evaluate(store, skill, target, guard, runner, training_cases=train)
    if res["verdict"] != "PROMOTE":
        raise AssertionError("测试夹具未构造出可晋升评估：%s" % res["reasons"])
    return skills.promote(store, skill["id"], evaluation_id=res["id"])


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
        new_ver = validated_promote(s, sk)
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

    def test_rollback_and_disable_are_different_operations(self):
        """回滚 = 恢复旧版正文并继续启用；撤销 = 不再参与 D，但不改正文。"""
        s = fresh()
        skills.ensure_schema(s)
        sk = self._skill()
        skills.save(s, sk)
        validated_promote(s, sk)
        self.assertEqual(skills.rollback(s, sk["id"]), 1)
        with s.conn() as c:
            row = dict(c.execute("SELECT status,version,active_version,strategy "
                                 "FROM skills WHERE id=?", (sk["id"],)).fetchone())
        self.assertEqual(row["status"], skills.ACTIVE,
                         "回滚是换回旧内容继续用，不是撤销启用")
        self.assertEqual(row["active_version"], 1)
        self.assertEqual(row["strategy"], "cross_verify")
        self.assertIsNotNone(skills.active_snapshot(s, sk["id"]),
                             "当前指针必须能追到一份不可变快照")

        skills.disable(s, sk["id"])
        self.assertIsNone(skills.lookup(s, "order", "silent_corruption", "D"),
                          "撤销启用后才不再参与 D 模式")
        with s.conn() as c:
            st = c.execute("SELECT status,strategy FROM skills WHERE id=?",
                           (sk["id"],)).fetchone()
        self.assertEqual(st["status"], skills.DISABLED)
        self.assertEqual(st["strategy"], "cross_verify",
                         "撤销不得顺手改掉正文，那是回滚的职责")


class TestSkillLookupDeterminism(unittest.TestCase):
    """C 模式的「取哪一条」必须由显式次序决定，不能靠数据库扫描顺序。

    这条测试存在的理由很具体：技能库里的 version 通常都是 2（晋升一次），
    所以 C 的 `task_type` 匹配一定命中多条并列行。只写 `ORDER BY version DESC`
    时 SQLite 返回**最先扫到的那条**（= 最早插入的），
    于是「C 的成功率」变成了技能库插入顺序的产物 —— 一次真实模型全量评测里，
    复合故障 C 恰好因为这个原因停在 40%，差点被解读成「经验在真实模型下失效」。
    """

    def _mk(self, sid, fault, created_at):
        return {"id": sid, "name": sid, "task_type": "order", "fault_type": fault,
                "strategy": "cross_verify", "required_tools": [],
                "safety": {"max_retry": 2}, "status": skills.ACTIVE,
                "version": 2, "created_at": created_at}

    def test_C_picks_latest_not_first_inserted(self):
        s = fresh()
        skills.ensure_schema(s)
        # 故意先插最旧，再插最新：旧实现会返回 sk_old
        skills.save(s, self._mk("sk_old", "stale_data", 100.0))
        skills.save(s, self._mk("sk_mid", "missing_field", 200.0))
        skills.save(s, self._mk("sk_new", "wrong_tool", 300.0))
        hit = skills.lookup(s, "order", "anything", "C")
        self.assertEqual(hit["id"], "sk_new",
                         "并列版本时必须取最近创建的，不能取最早插入的")

    def test_D_requires_fault_match(self):
        s = fresh()
        skills.ensure_schema(s)
        skills.save(s, self._mk("sk_old", "stale_data", 100.0))
        skills.save(s, self._mk("sk_new", "wrong_tool", 300.0))
        self.assertEqual(skills.lookup(s, "order", "stale_data", "D")["id"], "sk_old",
                         "D 必须按故障类型精确命中，不能被「最新」抢走")
        self.assertIsNone(skills.lookup(s, "order", "tool_timeout", "D"),
                          "没有对应故障的技能时 D 不能拿别的故障技能顶替")

    def test_scope_separates_multi_from_single(self):
        """复合学到的链不能占住单故障的键位。

        真实故障：kb/retrieval_failure 上放了一条两步链之后，
        单故障 retrieval_failure 的 6 个用例全挂了（本该用一步 fallback_tool）。
        所以第一轮恢复必须优先单故障技能，第二轮起才优先多故障技能。
        """
        s = fresh()
        skills.ensure_schema(s)
        single = self._mk("sk_single", "retrieval_failure", 100.0)
        single.update(scope="single", strategy="fallback_tool")
        multi = self._mk("sk_multi", "retrieval_failure", 200.0)   # 故意更新
        multi.update(scope="multi", strategy=["fallback_tool", "cross_verify"])
        skills.save(s, single)
        skills.save(s, multi)

        first = skills.lookup(s, "order", "retrieval_failure", "D", scope="single")
        self.assertEqual(first["id"], "sk_single",
                         "第一轮必须用单故障技能，哪怕多故障技能更新")
        second = skills.lookup(s, "order", "retrieval_failure", "D", scope="multi")
        self.assertEqual(second["id"], "sk_multi",
                         "第二轮起才该用复合学到的链")


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
        validated_promote(s, sk)
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


class TestFullRollback(unittest.TestCase):
    """P0 04 验收：v1 单步 / v2 两步且 scope 不同，回滚后实际行为必须等于 v1。

    只检查 version 数字变化不算通过 —— 必须看运行时真正选中的策略、
    recoveries 里落下的执行轨迹，以及技能声明的审批约束。
    """

    def setUp(self):
        self.s = fresh()
        self.addCleanup(self.s.close)
        skills.ensure_schema(self.s)
        self.v1 = {"id": "sk_rollback", "name": "v1 单步", "task_type": "order",
                   "fault_type": "missing_field", "strategy": "fallback_tool",
                   "required_tools": [],
                   "safety": {"max_retry": 1, "require_approval_for_write": True,
                              "max_chain": 1},
                   "status": skills.CANDIDATE, "version": 1, "created_at": 0,
                   "scope": skills.SINGLE,
                   "decision_params": {"confidence_min": 0.9}}
        skills.save(self.s, self.v1)
        self.task_id = sim.test_tasks("order")[0]["task_id"]
        runtime.FORCED_SKILL = None
        self.addCleanup(self._reset_runtime)

    def _reset_runtime(self):
        runtime.FORCED_SKILL = None

    def _trace(self):
        """跑一次真实恢复，取运行时落下的轨迹（不是只读数据库配置）。"""
        exp = self.s.create_experiment("rb", "order", self.task_id,
                                       "missing_field", 0, 7, "D")
        run = runtime.run_once(self.s, exp, "D")
        recs = self.s.list_recoveries(run["id"])
        self.assertTrue(recs, "这次运行必须真的产生一条恢复轨迹")
        return recs[0]

    def _body(self, row):
        return {"name": row["name"], "task_type": row["task_type"],
                "fault_type": row["fault_type"],
                "strategy": core.as_chain(row["strategy"]),
                "required_tools": core.loads(row["required_tools"], []),
                "safety": core.loads(row["safety"], {}),
                "scope": row["scope"],
                "decision_params": core.loads(row["decision_params"], None)}

    def _promote_v2(self):
        """把当前行改成两步 / multi，再走一次完整晋升门禁。"""
        skills.revise(self.s, self.v1["id"], name="v2 两步",
                      strategy=["fallback_tool", "cross_verify"],
                      scope=skills.MULTI,
                      required_tools=["order.get_order"],
                      safety={"max_retry": 3, "require_approval_for_write": False,
                              "max_chain": 3},
                      decision_params={"confidence_min": 0.4})
        current = skills.lookup(self.s, "order", "missing_field", "C")
        self.assertIsNotNone(current, "revise 后应留下未启用候选")
        return validated_promote(self.s, current)

    def test_rollback_restores_actual_v1_behavior(self):
        validated_promote(self.s, self.v1)          # v2 = v1 正文，active
        v3 = self._promote_v2()                     # v3 = 两步 / multi，active
        self.assertEqual(v3, 3)

        before = skills.lookup(self.s, "order", "missing_field", "D", scope="multi")
        self.assertEqual(core.as_chain(before["strategy"]),
                         ["fallback_tool", "cross_verify"])
        self.assertEqual(before["scope"], skills.MULTI)
        self.assertEqual(self._trace()["params"]["skill_version"], 3)

        ver = skills.rollback(self.s, self.v1["id"], to_version=2)
        self.assertEqual(ver, 2)

        after = skills.lookup(self.s, "order", "missing_field", "D", scope="multi")
        self.assertEqual(self._body(after)["strategy"], ["fallback_tool"],
                         "回滚后实际选中的策略必须等于 v1")
        self.assertEqual(after["scope"], skills.SINGLE)
        self.assertEqual(self._body(after)["required_tools"], [])
        self.assertEqual(self._body(after)["safety"], self.v1["safety"],
                         "审批约束必须一起还原")
        self.assertEqual(self._body(after)["decision_params"],
                         self.v1["decision_params"])
        self.assertEqual(after["name"], "v1 单步")

        # 运行时决策与真实执行轨迹都要落回 v1，不只是 DB 里的正文。
        plan = runtime.plan_recovery(self.s, "order", "missing_field", "D",
                                     scope="multi")
        self.assertEqual(plan["strategy_chain"], ["fallback_tool"])
        self.assertEqual(plan["params"]["skill_version"], 2)
        rec = self._trace()
        self.assertEqual(rec["strategy"], "fallback_tool")
        self.assertEqual(rec["params"]["chain"], ["fallback_tool"])
        self.assertEqual(rec["params"]["skill_version"], 2)

        # 历史、评估、审计都不能被删；指针必须能追到不可变快照。
        snap = skills.active_snapshot(self.s, self.v1["id"])
        self.assertEqual(int(snap["version"]), 2)
        self.assertEqual(core.as_chain(core.loads(snap["snapshot"], {})["strategy"]),
                         ["fallback_tool"])
        with self.s.conn() as c:
            n_snap = c.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_id=?",
                               (self.v1["id"],)).fetchone()[0]
            n_eval = c.execute("SELECT COUNT(*) FROM skill_evaluations WHERE skill_id=?",
                               (self.v1["id"],)).fetchone()[0]
            ev = c.execute("SELECT detail FROM evolution_events WHERE subject_id=? "
                           "AND kind='skill_rolled_back'",
                           (self.v1["id"],)).fetchone()
        self.assertGreaterEqual(n_snap, 3, "旧快照不得删除")
        self.assertGreaterEqual(n_eval, 2, "两次评估都得保留")
        self.assertIsNotNone(ev, "必须追加 rollback 事件")
        self.assertIn("恢复整份正文", ev["detail"])

    def test_revise_invalidates_previous_evaluation(self):
        validated_promote(self.s, self.v1)
        skills.revise(self.s, self.v1["id"], strategy="cross_verify")
        audit = skills.audit_promotion(self.s, self.v1["id"])
        self.assertFalse(audit["ok"])
        self.assertIn("CANDIDATE_CONTENT_CHANGED", audit["failed_codes"],
                      "改了正文的草稿不能拿旧评估放行")
        self.assertIn("CANDIDATE_CONTENT_CHANGED",
                      skills.promotion_failure_text(audit))
        with self.assertRaises(skills.PromotionRejected):
            skills.promote(self.s, self.v1["id"])
        self.assertIsNone(skills.active_snapshot(self.s, self.v1["id"]),
                          "草稿阶段没有正在执行的版本指针")

    def test_rollback_default_picks_previous_active_version(self):
        validated_promote(self.s, self.v1)
        self._promote_v2()
        self.assertEqual(skills.rollback(self.s, self.v1["id"]), 2)

    def test_rollback_to_unknown_version_is_rejected(self):
        validated_promote(self.s, self.v1)
        with self.assertRaises(KeyError):
            skills.rollback(self.s, self.v1["id"], to_version=99)
        self.assertEqual(skills.active_snapshot(self.s, self.v1["id"])["version"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
