"""Validation counterexamples: paired regressions, empty sets and independence."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "ai-service", "evoguard"))
import core
import skills


def cases(prefix, n=3):
    """默认 3 题（工程证据下限；统计功效见 chance_p_value 披露）。"""
    return [{"case_id": prefix + str(i), "agent_type": "order",
             "task_id": prefix + str(i), "fault_type": "missing_field", "seed": 7}
            for i in range(n)]


class TestSkillValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "validation.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.skill = {"id": "candidate", "strategy": "retry"}
        self.target, self.guard = cases("t"), cases("g")

    def runner(self, case, skill):
        success = case["task_id"].startswith("g") or skill is not None
        return success

    def evaluate(self, **kwargs):
        return skills.evaluate(self.store, self.skill, self.target, self.guard,
                               self.runner, **kwargs)

    def test_swapped_successes_cannot_cancel_regressions(self):
        self.guard = cases("g", 4)
        def runner(case, skill):
            if case["task_id"].startswith("t"):
                return skill is not None
            return (int(case["task_id"][1:]) < 2) == (skill is None)
        self.runner = runner
        result = self.evaluate()
        self.assertEqual(result["verdict"], "REJECT")
        self.assertEqual(result["neg_transfer_rate"], 1.0)
        self.assertEqual(result["guard_regressions"], 2)
        self.assertEqual(result["guard_improvements"], 2)
        self.assertEqual(result["guard_regression_fraction"], 0.5)

    def test_empty_guard_is_not_safe(self):
        self.guard = []
        result = self.evaluate()
        self.assertEqual(result["verdict"], "NEEDS_REVIEW")
        self.assertIsNone(result["neg_transfer_rate"])

    def test_empty_target_is_not_zero_gain(self):
        self.target = []
        result = self.evaluate()
        self.assertEqual(result["verdict"], "NEEDS_REVIEW")
        self.assertIsNone(result["gain_pp"])

    def test_no_baseline_success_is_unestimable(self):
        self.runner = lambda c, sk: sk is not None
        result = self.evaluate()
        self.assertEqual(result["verdict"], "NEEDS_REVIEW")
        self.assertIsNone(result["neg_transfer_rate"])

    def test_one_validation_task_needs_review(self):
        self.target = self.target[:1]
        self.assertEqual(self.evaluate()["verdict"], "NEEDS_REVIEW")

    def test_repeated_seeds_do_not_create_independent_tasks(self):
        self.target = [dict(self.target[0], case_id="repeat" + str(i), seed=i)
                       for i in range(3)]
        self.assertEqual(self.evaluate()["verdict"], "NEEDS_REVIEW")

    def test_enough_distinct_tasks_pass_operational_gate(self):
        result = self.evaluate()
        self.assertEqual(result["verdict"], "PROMOTE")
        self.assertEqual(result["guard_retained"], 3)
        self.assertEqual(result["guard_both_failed"], 0)
        self.assertEqual(result["protocol_version"], skills.VALIDATION_PROTOCOL)

    def test_duplicate_case_id_rejected_before_running(self):
        self.guard[0]["case_id"] = self.target[0]["case_id"]
        self.runner = lambda c, sk: self.fail("invalid split must not run")
        with self.assertRaisesRegex(ValueError, "case_id"):
            self.evaluate()

    def test_duplicate_recipe_with_different_id_is_rejected(self):
        self.target[1] = dict(self.target[0], case_id="alias")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.evaluate()

    def test_training_task_overlap_rejected(self):
        train = [dict(self.target[0], case_id="train", fault_type="other", seed=99)]
        with self.assertRaisesRegex(ValueError, "training"):
            self.evaluate(training_cases=train)

    def test_explicit_family_overlap_rejected(self):
        self.target[0]["family_id"] = "business-entity"
        train = [dict(cases("train")[0], family_id="business-entity")]
        with self.assertRaisesRegex(ValueError, "training"):
            self.evaluate(training_cases=train)

    def test_pairs_keep_seed_run_id_and_cost_and_survive_reload(self):
        base = self.runner
        self.runner = lambda c, sk: {"success": base(c, sk),
                                    "run_id": c["case_id"] + ("-after" if sk else "-before"),
                                    "cost": {"tokens": 12}}
        result = self.evaluate()
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM skill_evaluations WHERE id=?",
                            (result["id"],)).fetchone()
        saved = json.loads(row["details"])
        self.assertEqual(saved["pairs"], result["pairs"])
        self.assertEqual(row["protocol_version"], skills.VALIDATION_PROTOCOL)
        pair = saved["pairs"][0]
        self.assertEqual(pair["seed"], 7)
        self.assertFalse(pair["baseline_success"])
        self.assertTrue(pair["candidate_success"])
        self.assertEqual(pair["baseline_run_id"], "t0-before")
        self.assertEqual(pair["candidate_cost"], {"tokens": 12})

    def test_false_result_mapping_is_not_truthy_success(self):
        self.runner = lambda c, sk: {"success": False, "run_id": "failure"}
        result = self.evaluate()
        self.assertEqual(result["after_rate"], 0.0)
        self.assertNotEqual(result["verdict"], "PROMOTE")

    def test_legacy_evaluations_remain_marked_legacy(self):
        with self.store.conn() as c:
            c.execute("CREATE TABLE skill_evaluations(id TEXT PRIMARY KEY, skill_id TEXT, "
                      "target_n INTEGER, before_rate REAL, after_rate REAL, gain_pp REAL, "
                      "guard_n INTEGER, neg_transfer_rate REAL, verdict TEXT, created_at REAL)")
            c.execute("INSERT INTO skill_evaluations VALUES "
                      "('old','candidate',1,0,1,100,0,0,'PROMOTE',0)")
        self.evaluate()
        with self.store.conn() as c:
            old = c.execute("SELECT * FROM skill_evaluations WHERE id='old'").fetchone()
        self.assertIsNone(old["protocol_version"])
        self.assertEqual(old["verdict"], "PROMOTE")

    def test_guard_baseline_success_count_also_requires_independent_tasks(self):
        base = self.runner
        self.runner = lambda c, sk: False if c["task_id"] == "g2" else base(c, sk)
        result = self.evaluate()
        self.assertEqual(result["neg_transfer_rate"], 0.0)
        self.assertEqual(result["guard_both_failed"], 1)
        self.assertEqual(result["guard_baseline_success_tasks"],
                         len(self.guard) - 1)
        self.assertEqual(result["verdict"], "NEEDS_REVIEW")

    def test_repeated_family_is_not_three_independent_tasks(self):
        for case in self.target:
            case["family_id"] = "same-business"
        self.assertEqual(self.evaluate()["verdict"], "NEEDS_REVIEW")

    def test_runner_mutation_does_not_change_candidate_or_paired_input(self):
        original = self.target[0].copy()
        def runner(c, sk):
            self.assertEqual(c["seed"], 7)
            c["seed"] = 99
            if sk:
                sk["strategy"] = "mutated"
            return self.runner(c, sk)
        skills.evaluate(self.store, self.skill, self.target, self.guard, runner)
        self.assertEqual(self.target[0], original)
        self.assertEqual(self.skill["strategy"], "retry")

    def test_tuple_result_keeps_run_id_and_does_not_count_failure_as_success(self):
        base = self.runner
        self.runner = lambda c, sk: (base(c, sk), "tuple-run")
        result = self.evaluate()
        self.assertEqual(result["before_rate"], 0.0)
        self.assertEqual(result["pairs"][0]["baseline_run_id"], "tuple-run")


class TestPromotionGate(unittest.TestCase):
    """P0 03：晋升入口自身必须核对评估、正文指纹、验证集指纹与当前指针。

    这里的反例都直接打在 promote 上，不依赖调用方自觉先 evaluate。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "gate.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        skills.ensure_schema(self.store)
        self.skill = {"id": "sk_gate", "name": "遇到 X 用 Y", "task_type": "order",
                      "fault_type": "missing_field", "strategy": "retry",
                      "required_tools": [], "safety": {"max_retry": 2},
                      "status": skills.CANDIDATE, "version": 1, "created_at": 0,
                      "scope": "single"}
        skills.save(self.store, self.skill)
        self.target, self.guard = cases("t"), cases("g")
        self.train = [{"case_id": "train0", "agent_type": "order",
                       "task_id": "train0", "fault_type": "other", "seed": 7}]

    def runner(self, case, skill):
        if case["case_id"].startswith("t"):
            return skill is not None
        return True

    def evaluate(self):
        return skills.evaluate(self.store, self.skill, self.target, self.guard,
                               self.runner, training_cases=self.train)

    def promote(self, **kwargs):
        return skills.promote(self.store, self.skill["id"], **kwargs)

    def state(self):
        with self.store.conn() as c:
            row = dict(c.execute("SELECT status,version,active_version,"
                                 "promoted_from_evaluation FROM skills WHERE id=?",
                                 (self.skill["id"],)).fetchone())
            n = c.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_id=?",
                          (self.skill["id"],)).fetchone()[0]
        row["snapshots"] = n
        return row

    def assert_rejected(self, code, **kwargs):
        before = self.state()
        with self.assertRaises(skills.PromotionRejected) as cm:
            self.promote(**kwargs)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.state(), before, "被拒的晋升不得留下任何写库痕迹")
        self.assertIn(code, str(cm.exception))
        return cm.exception

    def test_no_evaluation_is_rejected(self):
        e = self.assert_rejected("NO_EVALUATION")
        self.assertIsNone(skills.lookup(self.store, "order", "missing_field", "D"))
        audit = skills.audit_promotion(self.store, self.skill["id"])
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["code"], "NO_EVALUATION")
        self.assertIn("NO_EVALUATION", skills.promotion_failure_text(audit))
        self.assertIn("处理建议", skills.promotion_failure_text(audit))
        self.assertIn("重新运行", str(e))

    def test_valid_evaluation_promotes_with_immutable_snapshot(self):
        res = self.evaluate()
        self.assertEqual(res["verdict"], "PROMOTE")
        self.assertEqual(self.promote(), 2)
        st = self.state()
        self.assertEqual(st["status"], skills.ACTIVE)
        self.assertEqual(st["version"], 2)
        self.assertEqual(st["active_version"], 2)
        self.assertEqual(st["promoted_from_evaluation"], res["id"])
        self.assertEqual(st["snapshots"], 2, "旧版本快照必须保留")
        with self.store.conn() as c:
            ev = c.execute("SELECT consumed_by FROM skill_evaluations WHERE id=?",
                           (res["id"],)).fetchone()
        self.assertEqual(ev["consumed_by"], "2")

    def test_repeated_promote_is_idempotent(self):
        res = self.evaluate()
        self.assertEqual(self.promote(), 2)
        self.assertEqual(self.promote(evaluation_id=res["id"]), 2,
                         "同一评估重复请求不能继续升版本")
        self.assertEqual(self.promote(), 2)
        self.assertEqual(self.state()["snapshots"], 2)
        self.assertEqual(self.state()["version"], 2)

    def test_candidate_changed_after_evaluation_invalidates_it(self):
        self.evaluate()
        with self.store.conn() as c:
            c.execute("UPDATE skills SET strategy=? WHERE id=?",
                      ("cross_verify", self.skill["id"]))
        self.assert_rejected("CANDIDATE_CONTENT_CHANGED")

    def test_scope_change_also_invalidates_evaluation(self):
        self.evaluate()
        with self.store.conn() as c:
            c.execute("UPDATE skills SET scope=? WHERE id=?", ("multi", self.skill["id"]))
        self.assert_rejected("CANDIDATE_CONTENT_CHANGED")

    def test_empty_guard_cannot_promote_even_if_verdict_is_flipped(self):
        self.guard = []
        res = self.evaluate()
        self.assertEqual(res["verdict"], "NEEDS_REVIEW")
        with self.store.conn() as c:
            c.execute("UPDATE skill_evaluations SET verdict='PROMOTE' WHERE id=?",
                      (res["id"],))
        self.assert_rejected("EVALUATION_VERDICT")
        audit = skills.audit_promotion(self.store, self.skill["id"])
        self.assertEqual(audit["measured"]["guard_tasks"], 0)

    def test_tiny_validation_set_cannot_promote_even_if_verdict_is_flipped(self):
        self.target = self.target[:1]
        res = self.evaluate()
        self.assertEqual(res["verdict"], "NEEDS_REVIEW")
        with self.store.conn() as c:
            c.execute("UPDATE skill_evaluations SET verdict='PROMOTE' WHERE id=?",
                      (res["id"],))
        self.assert_rejected("EVALUATION_VERDICT")
        audit = skills.audit_promotion(self.store, self.skill["id"])
        self.assertEqual(audit["measured"]["target_tasks"], 1)

    def test_expired_evaluation_is_rejected(self):
        res = self.evaluate()
        with self.store.conn() as c:
            c.execute("UPDATE skill_evaluations SET created_at=? WHERE id=?",
                      (core.now() - skills.MAX_EVALUATION_AGE_S - 60, res["id"]))
        self.assert_rejected("EVALUATION_EXPIRED")

    def test_legacy_evaluation_without_paired_details_is_rejected(self):
        with self.store.conn() as c:
            c.execute("INSERT INTO skill_evaluations(id,skill_id,target_n,before_rate,"
                      "after_rate,gain_pp,guard_n,neg_transfer_rate,verdict,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      ("ske_legacy", self.skill["id"], 3, 0, 1, 100, 3, 0,
                       "PROMOTE", core.now()))
        self.assert_rejected("EVALUATION_PROTOCOL_MISMATCH")

    def test_tampered_validation_cases_are_detected(self):
        res = self.evaluate()
        with self.store.conn() as c:
            d = json.loads(c.execute("SELECT details FROM skill_evaluations WHERE id=?",
                                     (res["id"],)).fetchone()[0])
            d["validation_cases"]["target"][0] = d["validation_cases"]["guard"][0]
            c.execute("UPDATE skill_evaluations SET details=? WHERE id=?",
                      (json.dumps(d, ensure_ascii=False, sort_keys=True), res["id"]))
        self.assert_rejected("EVALUATION_HASH_MISMATCH")

    def test_tampered_pairs_are_detected_even_when_hash_is_recomputed(self):
        res = self.evaluate()
        with self.store.conn() as c:
            d = json.loads(c.execute("SELECT details FROM skill_evaluations WHERE id=?",
                                     (res["id"],)).fetchone()[0])
            d["pairs"][0]["baseline_success"] = True
            c.execute("UPDATE skill_evaluations SET details=? WHERE id=?",
                      (json.dumps(d, ensure_ascii=False, sort_keys=True), res["id"]))
        # 验证集指纹没动，但配对成功率变了 → 样本/明细自洽检查要拦住。
        self.assert_rejected("EVALUATION_SAMPLE_INSUFFICIENT")

    def test_evaluation_of_another_skill_is_rejected(self):
        other = dict(self.skill, id="sk_other")
        skills.save(self.store, other)
        res = skills.evaluate(self.store, other, self.target, self.guard,
                              self.runner, training_cases=self.train)
        with self.assertRaises(skills.PromotionRejected) as cm:
            skills.promote(self.store, self.skill["id"], evaluation_id=res["id"])
        self.assertEqual(cm.exception.code, "EVALUATION_SKILL_MISMATCH")
        self.assertEqual(self.state()["snapshots"], 1)

    def test_missing_evaluation_id_is_rejected(self):
        self.evaluate()
        before = self.state()
        with self.assertRaises(skills.PromotionRejected) as cm:
            self.promote(evaluation_id="ske_does_not_exist")
        self.assertEqual(cm.exception.code, "EVALUATION_NOT_FOUND")
        self.assertEqual(self.state(), before)

    def test_audit_is_read_only(self):
        res = self.evaluate()
        audit = skills.audit_promotion(self.store, self.skill["id"])
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["evaluation_id"], res["id"])
        self.assertFalse(audit["idempotent"])
        self.assertEqual(audit["measured"]["gain_pp"], 100.0)
        self.assertEqual(audit["measured"]["neg_transfer_rate"], 0.0)
        self.assertEqual(self.state()["version"], 1, "体检不得改动当前指针")
        self.assertEqual(self.promote(), 2)
        audit2 = skills.audit_promotion(self.store, self.skill["id"])
        self.assertTrue(audit2["ok"])
        self.assertTrue(audit2["idempotent"])

    def test_replay_stays_idempotent_after_evaluation_expires(self):
        res = self.evaluate()
        self.assertEqual(self.promote(), 2)
        with self.store.conn() as c:
            c.execute("UPDATE skill_evaluations SET created_at=? WHERE id=?",
                      (core.now() - skills.MAX_EVALUATION_AGE_S - 60, res["id"]))
        self.assertEqual(self.promote(evaluation_id=res["id"]), 2,
                         "重放旧请求不是一次新晋升，仍应幂等返回已启用版本")


class TestStrictLibraryPipeline(unittest.TestCase):
    """门禁必须贯通到真实训练入口，不能只在 skills.promote 里生效。

    real_model_train.build_library 以前直接把行 UPDATE 成 active/v2，
    等于绕过整个晋升链路；这里验证 D 库现在确实由一次 promote 产生。
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "ai-service"))
        import real_model_train as pipeline
        self.pipeline = pipeline
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = core.Store(os.path.join(self.tmp.name, "train.db"))
        self.addCleanup(self.src.close)
        skills.ensure_schema(self.src)
        self.skill = {"id": "sk_pipeline", "name": "pipeline", "task_type": "order",
                      "fault_type": "missing_field", "strategy": "retry",
                      "required_tools": [], "safety": {"max_retry": 2},
                      "status": skills.CANDIDATE, "version": 1, "created_at": 0,
                      "scope": "single"}
        skills.save(self.src, self.skill)
        self.res = skills.evaluate(
            self.src, self.skill, cases("t"), cases("g"),
            lambda c, sk: (sk is not None) if c["case_id"].startswith("t") else True,
            training_cases=[{"case_id": "train0", "agent_type": "order",
                             "task_id": "train0", "fault_type": "other", "seed": 7}])
        self.assertEqual(self.res["verdict"], "PROMOTE")

    def test_strict_library_is_produced_by_a_real_promotion(self):
        dst = os.path.join(self.tmp.name, "library_d.db")
        n = self.pipeline.build_library(
            self.src, dst, [(self.skill["id"], self.res["id"])], "严格", strict=True)
        self.assertEqual(n, 1)
        with self.src.conn() as c:
            ev = dict(c.execute("SELECT consumed_by FROM skill_evaluations WHERE id=?",
                                (self.res["id"],)).fetchone())
            row = dict(c.execute("SELECT status,version,active_version,"
                                 "promoted_from_evaluation FROM skills WHERE id=?",
                                 (self.skill["id"],)).fetchone())
            snaps = c.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_id=?",
                              (self.skill["id"],)).fetchone()[0]
        self.assertEqual(ev["consumed_by"], "2", "评估必须被这次晋升消费")
        self.assertEqual(row["status"], skills.ACTIVE)
        self.assertEqual(row["version"], 2)
        self.assertEqual(row["active_version"], 2)
        self.assertEqual(row["promoted_from_evaluation"], self.res["id"])
        self.assertEqual(snaps, 2)
        lib = core.Store(dst)
        try:
            with lib.conn() as c:
                out = dict(c.execute("SELECT * FROM skills WHERE id=?",
                                     (self.skill["id"],)).fetchone())
            self.assertEqual(out["status"], skills.ACTIVE)
            self.assertEqual(out["version"], 2)
            self.assertEqual(out["active_version"], 2)
            self.assertEqual(out["promoted_from_evaluation"], self.res["id"])
        finally:
            lib.close()

    def test_strict_library_rejects_unvalidated_skill(self):
        other = dict(self.skill, id="sk_unvalidated")
        skills.save(self.src, other)
        dst = os.path.join(self.tmp.name, "library_reject.db")
        with self.assertRaises(skills.PromotionRejected) as cm:
            self.pipeline.build_library(self.src, dst, [(other["id"], None)],
                                        "严格", strict=True)
        self.assertEqual(cm.exception.code, "NO_EVALUATION")
        self.assertFalse(os.path.exists(dst), "门禁拒绝时不应产出半套技能库")

    def test_loose_control_library_does_not_rewrite_source_evidence(self):
        with self.src.conn() as c:
            before = [tuple(r) for r in c.execute(
                "SELECT id,status,version FROM skills ORDER BY id").fetchall()]
        dst = os.path.join(self.tmp.name, "library_c.db")
        n = self.pipeline.build_library(self.src, dst, [self.skill["id"]],
                                        "放宽对照组", strict=False)
        self.assertEqual(n, 1)
        with self.src.conn() as c:
            after = [tuple(r) for r in c.execute(
                "SELECT id,status,version FROM skills ORDER BY id").fetchall()]
        self.assertEqual(before, after, "C 只是对照组，不该伪造源库的晋升状态")
        lib = core.Store(dst)
        try:
            with lib.conn() as c:
                out = dict(c.execute("SELECT status,version,promoted_from_evaluation "
                                     "FROM skills WHERE id=?",
                                     (self.skill["id"],)).fetchone())
            self.assertEqual(out["status"], skills.ACTIVE)
            self.assertEqual(out["version"], 2)
            self.assertIsNone(out["promoted_from_evaluation"],
                              "放宽库没有评估来源，不能冒充验证过")
        finally:
            lib.close()


if __name__ == "__main__":
    unittest.main()
