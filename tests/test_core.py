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
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import baseline    # noqa: E402
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
        """纯内存经验：只适合测结构条件，没有可核实的轨迹。"""
        base = {"id": "e1", "source_run_id": "r1", "task_type": "order", "environment": "mock",
                "failure_type": "silent_corruption", "root_cause": "silent_corruption",
                "recovery_action": "cross_verify", "recovery_result": "success",
                "evidence_refs": ["st_1"], "confidence": 0.9, "validation_status": "pending",
                "created_at": 0, "version": 1}
        base.update(kw)
        return base

    def _real(self, s, **kw):
        """构造一次真实的故障→诊断→成功恢复轨迹，再从中提炼经验。

        P0 05 之前测试直接用内存假 step_id；溯源开关一开这些「证据」
        就都该被拒。这里换成真轨迹，被测的治理语义不变。
        """
        e = s.create_experiment("t", "order", "A1001", "silent_corruption", 0, 7, "D")
        run = s.create_run(e["id"], "D", 7)
        s.set_state(run["id"], core.READY)
        s.set_state(run["id"], core.RUNNING)
        sid = s.add_step(run["id"], 1, "tool", "order.get_order", {},
                         {"total": 100}, status="mismatch", logical=1)
        did = s.add_diagnosis(run["id"], 1, "silent_corruption", {"step": sid}, 0.9)
        s.add_recovery(run["id"], did, "cross_verify", {"chain": ["cross_verify"]},
                       "auto", 1, "ok")
        exp = experience.extract(s, run["id"])
        exp.update(kw)
        return exp, run["id"], sid

    def test_ok(self):
        s = fresh()
        self.addCleanup(s.close)
        exp, _, _ = self._real(s)
        status, why = experience.validate(exp, s)
        self.assertEqual(status, experience.VALIDATED)
        self.assertEqual(why, [])

    def test_validate_without_store_cannot_claim_validated(self):
        status, why = experience.validate(self._exp())
        self.assertEqual(status, experience.PENDING,
                         "没核实来源就不能发可信资格")
        self.assertTrue(any("来源核实" in w for w in why))

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
        self.addCleanup(s.close)
        experience.ensure_schema(s)
        good, _, _ = self._real(s, id="e_good")
        bad, _, _ = self._real(s, id="e_bad", recovery_result="fail")
        experience.save(s, good)
        experience.save(s, bad)
        trusted = experience.retrieve(s, "order")
        self.assertEqual([e["id"] for e in trusted], ["e_good"],
                         "未通过校验的经验不得进入可信检索")
        loose = experience.retrieve(s, "order", include_pending=True)
        self.assertEqual([e["id"] for e in loose], ["e_good"],
                         "rejected 的经验任何时候都不该被检索到")
        with s.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM experiences WHERE validation_status='rejected'").fetchone()[0]
        self.assertEqual(n, 1, "失败经验要保留作分析资料，只是不能进入可信检索")


class TestExperienceProvenance(unittest.TestCase):
    """P0 05：证据必须能溯源，环境元数据必须从真实运行复制。

    以前的 experience 把 environment 写死 "mock"，evidence_refs 只要非空
    就放行 —— 填个假 ID、抄别的运行的 step_id 都能拿到 validated。
    """

    def setUp(self):
        self.s = fresh()
        self.addCleanup(self.s.close)
        experience.ensure_schema(self.s)

    def _build(self, provider="ollama", model_id="qwen3:4b", backend=None,
               n_diagnoses=1):
        if backend:
            os.environ["AGENTLAB_TOOL_BACKEND"] = backend
            self.addCleanup(lambda: os.environ.pop("AGENTLAB_TOOL_BACKEND", None))
        e = self.s.create_experiment("t", "order", "A1001", "silent_corruption", 0, 7, "D")
        run = self.s.create_run(e["id"], "D", 7)
        self.s.set_state(run["id"], core.READY)
        self.s.set_state(run["id"], core.RUNNING)
        sid = self.s.add_step(run["id"], 1, "model", "answer",
                              {"prompt": "算一下 A1001 的总额", "provider": provider},
                              {"text": "{}", "parsed": {}}, status="ok",
                              model_id=model_id, logical=1)
        did = None
        for _ in range(n_diagnoses):
            did = self.s.add_diagnosis(run["id"], 1, "silent_corruption",
                                       {"step": sid}, 0.9)
        self.s.add_recovery(run["id"], did, "cross_verify",
                            {"chain": ["cross_verify"]}, "auto", 1, "ok")
        return experience.extract(self.s, run["id"]), run["id"], sid

    def test_environment_is_copied_from_run_not_hardcoded(self):
        exp, _, _ = self._build()
        self.assertNotEqual(exp["environment"], "mock")
        self.assertEqual(exp["environment"], "local_model_simulated_tools")
        self.assertEqual(exp["provider"], "ollama")
        self.assertEqual(exp["model_id"], "qwen3:4b")
        for k in ("model_fingerprint", "prompt_fingerprint", "dataset_version",
                  "experiment_protocol", "tool_backend"):
            self.assertTrue(exp[k], "%s 不能为空" % k)
        meta = core.loads(exp["env_meta"], {})
        self.assertEqual(meta["env_protocol"], experience.ENV_PROTOCOL)
        self.assertEqual(meta["source_run_id"], exp["source_run_id"])
        self.assertEqual(meta["step_count"], 1)
        self.assertEqual(meta["model_step_count"], 1)
        self.assertEqual(meta["tool_backend"], "simulated")
        self.assertIn("validation_protocol", meta["experiment_protocol"])
        self.assertIn("不是模型权重哈希", meta["model_fingerprint_note"])

    def test_real_toolchain_is_distinguished_from_simulated_tools(self):
        exp, _, _ = self._build(backend="real")
        self.assertEqual(exp["environment"], "real_toolchain")
        self.assertEqual(exp["tool_backend"], "real")
        meta = core.loads(exp["env_meta"], {})
        self.assertEqual(meta["tool_backend_source"], "AGENTLAB_TOOL_BACKEND")

    def test_mock_provider_is_classified_as_mock(self):
        exp, _, _ = self._build(provider="mock", model_id="mock")
        self.assertEqual(exp["environment"], "mock_simulated_tools")

    def test_fingerprints_change_with_model_and_prompt(self):
        a, _, _ = self._build(model_id="qwen3:4b")
        b, _, _ = self._build(model_id="qwen3:8b")
        self.assertNotEqual(a["model_fingerprint"], b["model_fingerprint"])

    def test_fault_scope_survives_roundtrip_and_reaches_skill(self):
        exp, _, _ = self._build(n_diagnoses=2)
        exp["id"] = "e_multi"
        experience.save(self.s, exp)
        got = experience.retrieve(self.s, "order")[0]
        self.assertEqual(got["fault_scope"], "multi",
                         "fault_scope 必须持久化，不能读回就丢")
        self.assertEqual(got["env_meta"]["fault_scope"], "multi")
        self.assertEqual(skills.propose(got)["scope"], skills.MULTI,
                         "作用域必须能一路传到技能层")

    def test_env_meta_survives_roundtrip(self):
        exp, _, _ = self._build()
        exp["id"] = "e_env"
        experience.save(self.s, exp)
        got = experience.retrieve(self.s, "order")[0]
        self.assertEqual(got["provider"], "ollama")
        self.assertEqual(got["model_id"], "qwen3:4b")
        self.assertEqual(got["env_meta"]["dataset_version"], exp["dataset_version"])

    def test_fabricated_step_id_is_rejected(self):
        exp, _, _ = self._build()
        exp["evidence_refs"] = ["st_does_not_exist"]
        status, why = experience.validate(exp, self.s)
        self.assertEqual(status, experience.REJECTED)
        self.assertTrue(any("不存在" in w for w in why))

    def test_cross_run_reference_is_rejected(self):
        exp, _, _ = self._build()
        e2 = self.s.create_experiment("t2", "order", "A1002", "silent_corruption", 0, 7, "D")
        r2 = self.s.create_run(e2["id"], "D", 7)
        self.s.set_state(r2["id"], core.READY)
        self.s.set_state(r2["id"], core.RUNNING)
        other = self.s.add_step(r2["id"], 1, "tool", "order.get_order", {}, {},
                                logical=1)
        exp["evidence_refs"] = [other]
        status, why = experience.validate(exp, self.s)
        self.assertEqual(status, experience.REJECTED)
        self.assertTrue(any("跨运行伪造" in w for w in why),
                        "抄别的运行的 step_id 不能算证据")

    def test_diagnosis_evidence_must_be_linked(self):
        exp, rid, _ = self._build()
        unrelated = self.s.add_step(rid, 2, "tool", "order.calc_total", {},
                                    {}, logical=2)
        exp["evidence_refs"] = [unrelated]
        status, why = experience.validate(exp, self.s)
        self.assertEqual(status, experience.REJECTED)
        self.assertTrue(any("诊断" in w for w in why))

    def test_recovery_action_must_link_to_successful_recovery(self):
        exp, _, _ = self._build()
        exp["recovery_action"] = "unrelated_strategy"
        status, why = experience.validate(exp, self.s)
        self.assertEqual(status, experience.REJECTED)
        self.assertTrue(any("恢复动作" in w for w in why))

    def test_missing_success_recovery_is_rejected(self):
        exp, rid, _ = self._build()
        with self.s.conn() as c:
            c.execute("DELETE FROM recoveries WHERE run_id=?", (rid,))
        status, why = experience.validate(exp, self.s)
        self.assertEqual(status, experience.REJECTED)
        self.assertTrue(any("成功" in w for w in why))

    def test_legacy_rows_keep_null_env_columns(self):
        s2 = fresh()
        self.addCleanup(s2.close)
        with s2.conn() as c:
            c.execute("CREATE TABLE experiences(id TEXT PRIMARY KEY, source_run_id TEXT, "
                      "task_type TEXT, environment TEXT, failure_type TEXT, root_cause TEXT, "
                      "recovery_action TEXT, recovery_result TEXT, evidence_refs TEXT, "
                      "confidence REAL, validation_status TEXT, created_at REAL, version INTEGER)")
            c.execute("INSERT INTO experiences VALUES('old','r1','order','mock',"
                      "'silent_corruption','silent_corruption','cross_verify','success',"
                      "'[\"st_1\"]',0.9,'validated',0,1)")
        experience.ensure_schema(s2)
        got = experience.retrieve(s2, "order")[0]
        self.assertIsNone(got["provider"], "老库不能凭空补出新协议元数据")
        self.assertIsNone(got["fault_scope"])
        self.assertIsNone(got["env_meta"])


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


class TestBaseline(unittest.TestCase):
    """P0 06：manifest 冻结、稳定技能库指纹、防混跑与分母自洽。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "project")
        self.run = os.path.join(self.root, "experiments", "run_001")
        os.makedirs(self.run)
        code = os.path.join(self.root, "ai-service", "evoguard")
        os.makedirs(code)
        with open(os.path.join(code, "core.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        self._write_metrics()
        self._write_libraries()

    def _write_metrics(self, metrics=None):
        def suite(name, n, ok):
            return {"suite": name, "cases": n,
                    "modes": {"A": {"n": n, "ok": ok, "failed": n - ok,
                                    "unjudgeable": 0,
                                    "rate": round(100.0 * ok / n, 1)}}}
        self.metrics = metrics or {
            "meta": {"backend": "mock", "model": "mock",
                     "skillset": {"d": 1, "c": 1, "d_path": "library_d.db",
                                  "c_path": "library_c.db"}},
            "single": suite("single", 204, 40),
            "composite": suite("composite", 30, 0),
            "hard": suite("hard", 36, 8)}
        with open(os.path.join(self.run, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump(self.metrics, f, ensure_ascii=False)
        with open(os.path.join(self.run, "report.md"), "w", encoding="utf-8") as f:
            f.write("# report\n")

    def _write_libraries(self):
        for name, sid in (("library_c.db", "sk_c"), ("library_d.db", "sk_d")):
            s = core.Store(os.path.join(self.run, name), fast=True)
            skills.ensure_schema(s)
            skills.save(s, {"id": sid, "name": "n", "task_type": "order",
                            "fault_type": "missing_field", "strategy": "retry",
                            "required_tools": [], "safety": {"max_retry": 2},
                            "status": skills.CANDIDATE, "version": 1,
                            "created_at": 0, "scope": "single"})
            s.close()

    def _freeze(self, out=None, **kw):
        path = out or os.path.join(self.root, "manifests", "m1.json")
        return baseline.freeze(self.root, self.run, manifest_path=path, **kw), path

    def test_freeze_and_verify_roundtrip(self):
        m, path = self._freeze()
        self.assertEqual(m["protocol"], baseline.PROTOCOL)
        self.assertEqual(set(m["libraries"]), {"C", "D"})
        self.assertTrue(m["config_hash"] and m["library_hash"] and m["fingerprint"])
        kinds = {f["kind"] for f in m["files"]}
        self.assertTrue({"metrics", "report", "database", "code"} <= kinds)
        self.assertTrue(baseline.verify(self.root, path)["ok"])

    def test_changed_report_is_detected(self):
        m, path = self._freeze()
        with open(os.path.join(self.run, "report.md"), "a", encoding="utf-8") as f:
            f.write("手改一行\n")
        res = baseline.verify(self.root, path)
        self.assertFalse(res["ok"])
        self.assertTrue(any("report.md" in d for d in res["diffs"]))

    def test_changed_code_is_reported_as_drift_not_tampering(self):
        _, path = self._freeze()
        with open(os.path.join(self.root, "ai-service", "evoguard", "core.py"),
                  "w", encoding="utf-8") as f:
            f.write("x = 2\n")
        res = baseline.verify(self.root, path)
        self.assertTrue(res["ok"], "代码往前走一步不等于产物被篡改")
        self.assertTrue(any("core.py" in d for d in res["code_drift"]),
                        "但必须说清这份报告不是当前代码产出的")
        self.assertEqual(res["diffs"], [])

    def test_library_content_change_is_detected(self):
        _, path = self._freeze()
        s = core.Store(os.path.join(self.run, "library_d.db"), fast=True)
        skills.ensure_schema(s)
        with s.conn() as c:
            c.execute("UPDATE skills SET strategy='cross_verify' WHERE id='sk_d'")
        s.close()
        res = baseline.verify(self.root, path)
        self.assertFalse(res["ok"])
        self.assertTrue(any("技能库 D" in d for d in res["diffs"]))

    def test_library_hash_is_stable_across_reopen(self):
        p = os.path.join(self.run, "library_c.db")
        h1, n1 = baseline.library_content_hash(p)
        s = core.Store(p, fast=True)
        s.close()
        h2, n2 = baseline.library_content_hash(p)
        self.assertEqual((h1, n1), (h2, n2))

    def test_freeze_refuses_to_overwrite_history(self):
        _, path = self._freeze()
        with self.assertRaises(FileExistsError):
            baseline.freeze(self.root, self.run, manifest_path=path)
        self.assertTrue(baseline.verify(self.root, path)["ok"])

    def test_run_key_changes_with_every_dimension(self):
        case = {"case_id": "c1", "seed": 7, "mode": "D"}
        base = baseline.run_key(case, cfg_hash="cfg", lib_hash="lib")
        for changed in (dict(case, seed=8), dict(case, mode="C"),
                        dict(case, case_id="c2")):
            self.assertNotEqual(
                base, baseline.run_key(changed, cfg_hash="cfg", lib_hash="lib"))
        self.assertNotEqual(base, baseline.run_key(case, cfg_hash="cfg2", lib_hash="lib"))
        self.assertNotEqual(base, baseline.run_key(case, cfg_hash="cfg", lib_hash="lib2"))

    def test_no_mixing_rejects_foreign_and_unmarked_records(self):
        m, _ = self._freeze()
        good = [{"case_id": "c1", "protocol": m["protocol"],
                 "config_hash": m["config_hash"], "library_hash": m["library_hash"]}]
        self.assertTrue(baseline.assert_no_mixing(
            good, cfg_hash=m["config_hash"], lib_hash=m["library_hash"]))
        foreign = dict(good[0], config_hash="other")
        with self.assertRaisesRegex(baseline.BaselineMismatch, "config_hash"):
            baseline.assert_no_mixing(
                [foreign], cfg_hash=m["config_hash"], lib_hash=m["library_hash"])
        with self.assertRaisesRegex(baseline.BaselineMismatch, "缺少"):
            baseline.assert_no_mixing(
                [{"case_id": "legacy"}], cfg_hash=m["config_hash"],
                lib_hash=m["library_hash"], strict=True)
        self.assertTrue(baseline.assert_no_mixing(
            [{"case_id": "legacy"}], cfg_hash=m["config_hash"],
            lib_hash=m["library_hash"], strict=False))

    def test_load_runs_validates_against_manifest(self):
        m, _ = self._freeze()
        path = os.path.join(self.run, "runs.jsonl")
        rec = {"key": "c1", "success": True, "protocol": m["protocol"],
               "config_hash": m["config_hash"], "library_hash": m["library_hash"]}
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self.assertEqual(len(baseline.load_runs(path, m)), 1)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(rec, key="c2", library_hash="other")) + "\n")
        with self.assertRaises(baseline.BaselineMismatch):
            baseline.load_runs(path, m)

    def test_denominator_checks_catch_inconsistent_reports(self):
        self.assertTrue(baseline.check_denominators(self.metrics)["ok"])
        bad = json.loads(json.dumps(self.metrics))
        bad["single"]["modes"]["A"]["rate"] = 99.9
        self.assertFalse(baseline.check_denominators(bad)["ok"])
        bad2 = json.loads(json.dumps(self.metrics))
        bad2["composite"]["cases"] = 31
        self.assertFalse(baseline.check_denominators(bad2)["ok"])

    def test_freeze_rejects_empty_library_set(self):
        """显式传空库集合 = 路径算错。静默写下去，指纹就退化成 sha256("{}")。"""
        with self.assertRaisesRegex(ValueError, "技能库集合为空"):
            baseline.freeze(self.root, self.run, libraries={},
                            manifest_path=os.path.join(self.root, "m", "empty.json"))

    def test_summarize_and_mode_section_use_one_source(self):
        summary = baseline.summarize(self.metrics)
        self.assertTrue(summary["denominators"]["ok"])
        self.assertEqual(summary["suites"]["single"]["rates"]["A"], 19.6)
        lines = baseline.render_mode_section(self.metrics, summary)
        text = "\n".join(lines)
        self.assertIn("library_d.db", text)
        self.assertIn("固定规则表", text)
        self.assertIn("分母自洽检查：通过", text)

    def test_resume_gate_blocks_foreign_and_unmarked(self):
        m, _ = self._freeze()
        same = [{"case_id": "c1", "config_hash": m["config_hash"],
                 "library_hash": m["library_hash"]}]
        gate = baseline.check_resume(same, cfg_hash=m["config_hash"],
                                     lib_hash=m["library_hash"])
        self.assertTrue(gate["ok"])
        self.assertFalse(gate["mixed"])

        foreign = [{"case_id": "c1", "config_hash": "other",
                    "library_hash": m["library_hash"]}]
        gate = baseline.check_resume(foreign, cfg_hash=m["config_hash"],
                                     lib_hash=m["library_hash"], allow_legacy=True)
        self.assertEqual((gate["ok"], gate["reason"]), (False, "foreign"),
                         "别的配置指纹即使允许续跑也不能放行")

        other_lib = [{"case_id": "c1", "config_hash": m["config_hash"],
                      "library_hash": "other"}]
        gate = baseline.check_resume(other_lib, cfg_hash=m["config_hash"],
                                     lib_hash=m["library_hash"])
        self.assertEqual(gate["reason"], "foreign", "换了技能库同样算混基线")

        legacy = [{"case_id": "old"}]
        self.assertEqual(baseline.check_resume(
            legacy, cfg_hash=m["config_hash"])["reason"], "unmarked")
        gate = baseline.check_resume(legacy, cfg_hash=m["config_hash"],
                                     allow_legacy=True)
        self.assertTrue(gate["ok"])
        self.assertTrue(gate["mixed"], "无指纹记录续跑必须标成混合基线")

    def test_four_groups_rates_must_match_counts(self):
        metrics = {"four_groups": {"n": 204, "rates": {"A": 0.0, "B": 26.5},
                                   "counts": {"A": 0, "B": 54}}}
        self.assertTrue(baseline.check_denominators(metrics)["ok"])
        bad = {"four_groups": {"n": 204, "rates": {"A": 0.0, "B": 30.0},
                               "counts": {"A": 0, "B": 54}}}
        res = baseline.check_denominators(bad)
        self.assertFalse(res["ok"])
        self.assertTrue(any("B" in p for p in res["problems"]))

    def test_freeze_rejects_library_changed_after_hashing(self):
        cfg = {"protocol": baseline.PROTOCOL}
        h = baseline.hashes(cfg, {"D": os.path.join(self.run, "library_d.db")})
        s = core.Store(os.path.join(self.run, "library_d.db"), fast=True)
        skills.ensure_schema(s)
        with s.conn() as c:
            c.execute("UPDATE skills SET strategy='cross_verify' WHERE id='sk_d'")
        s.close()
        with self.assertRaisesRegex(baseline.BaselineMismatch, "内容已变化"):
            baseline.freeze(self.root, self.run, config=cfg, libraries=h["libraries"],
                            manifest_path=os.path.join(self.root, "m", "drift.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
