# -*- coding: utf-8 -*-
"""SCORE-04：结构化 Skill 与按错误族配对 A/B（修前必红）。"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import core                                                 # noqa: E402
import skill_synthesis as SS                                # noqa: E402


FAILED = [
    {"seq": 1, "logical": 1, "name": "code.run_in_sandbox",
     "status": "error", "error_type": "ZeroDivisionError",
     "output": {"stderr": "ZeroDivisionError: division by zero at calc.py:12"}},
    {"seq": 2, "logical": 2, "name": "fs.read", "status": "ok",
     "output": {"text": "def ratio(a,b): return a/b"}},
]
REPAIRED = [
    {"seq": 1, "logical": 1, "name": "fs.patch", "status": "ok",
     "output": {"replaced": "return a/b -> return a/b if b else 0"}},
    {"seq": 2, "logical": 2, "name": "code.run_in_sandbox", "status": "ok",
     "output": {"returncode": 0, "stderr": ""}},
]


class TestStructuredSkill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "s.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_synthesize_extracts_structured_fields_from_pair(self):
        out = SS.synthesize(self.store, FAILED, repair_steps=REPAIRED,
                            task_type="boundary")
        sk = out["skill"]
        for field in ("error_signature", "diagnostic_steps",
                      "fix_pattern", "verification_check"):
            self.assertTrue(sk.get(field),
                            "缺结构化字段 %s：%s" % (field, sk))
        self.assertIn("ZeroDivisionError", str(sk["error_signature"]))
        self.assertIn("if b", str(sk["fix_pattern"]))

    def test_fields_survive_save_and_reload(self):
        out = SS.synthesize(self.store, FAILED, repair_steps=REPAIRED)
        sk = out["skill"]
        import skills
        skills.save(self.store, sk)
        rows = skills.list_skills(self.store)
        self.assertTrue(rows)
        self.assertTrue(rows[0].get("error_signature"))

    def test_no_repair_steps_still_has_honest_placeholders(self):
        out = SS.synthesize(self.store, FAILED)
        sk = out["skill"]
        # 没有成功轨迹时不能编造 fix_pattern，必须如实标注 unknown
        self.assertEqual(sk.get("fix_pattern"), "unknown_no_repair_trace")


class TestPairingByErrorFamily(unittest.TestCase):
    def test_ab_groups_share_family_but_not_tasks(self):
        import eval_skill_ab_real as E
        groups = E.ERROR_FAMILY_CASES
        self.assertGreaterEqual(len(groups), 2)
        for family, spec in groups.items():
            train_ids = {c["case_id"] for c in spec["train"]}
            holdout_ids = {c["case_id"] for c in spec["holdout"]}
            self.assertFalse(train_ids & holdout_ids,
                             "%s 训练/留出题目重叠" % family)

    def test_hint_contains_structured_fields(self):
        import eval_skill_ab_real as E
        hint, meta = E.build_skill_hint(None, repair_steps=REPAIRED)
        # 修复模式必须来自成功轨迹的真实动作，不是空泛套话
        self.assertIn("fs.patch", hint)
        self.assertIn("if b", hint)
        self.assertIn("结构化经验", hint)
        self.assertTrue(meta.get("structured"))


if __name__ == "__main__":
    unittest.main()
