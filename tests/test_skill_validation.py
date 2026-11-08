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


if __name__ == "__main__":
    unittest.main()
