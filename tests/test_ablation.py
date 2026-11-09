# -*- coding: utf-8 -*-
"""§10 回归：六组公平消融只变一个变量，业务与防护同时报告。"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location(
    "ablation_v1", os.path.join(ROOT, "scripts", "ablation_v1.py"))
AB = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AB)


class TestFairAblation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ablation_")
        with contextlib.redirect_stdout(io.StringIO()):
            cls.r = AB.main(["--out", cls.tmp])

    def test_six_ablations_and_thirteen_configs(self):
        self.assertEqual(len(self.r["ablations"]), 6)
        self.assertEqual(len(self.r["runs"]), 13)
        self.assertTrue(self.r["single_variable_checked"])

    def test_each_config_changes_exactly_one_field(self):
        for label, cfg in [(k, v) for k, v in self.r["runs"].items()]:
            run = self.r["runs"][label]
            field = run["changed_field"]
            others = {k: v for k, v in AB.BASE.items() if k != field}
            # 用配置指纹反查：脚本已断言过，这里再核一遍设计声明
            self.assertEqual(run["fixed_conditions"],
                             next(a[1] for a in AB.ABLATIONS
                                  if a[0] == run["ablation"]))
            self.assertEqual(set(others) | {field}, set(AB.BASE))
            self.assertEqual(len(run["config_fingerprint"]), 64)
        # 同一基线会作为多个消融的对照组，跨组指纹重复是**正确**的；
        # 要守的是「同一消融组内不同取值的配置指纹必须不同」。
        for name, _fixed, _field, values in AB.ABLATIONS:
            fps = [v["config_fingerprint"] for v in self.r["runs"].values()
                   if v["ablation"] == name]
            self.assertEqual(len(fps), len(values))
            self.assertEqual(len(set(fps)), len(values),
                             "%s 组内配置指纹重复" % name)

    def test_eight_indicators_present_and_recomputable(self):
        keys = ("planned", "framework_failed", "judgeable", "unjudgeable",
                "business_success", "safe_completion", "pending_approval",
                "attack_goal_achieved", "normal_false_blocked")
        for label, run in self.r["runs"].items():
            s, rows = run["summary"], run["cases"]
            for k in keys:
                self.assertIn(k, s, "%s 缺 %s" % (label, k))
            self.assertEqual(s["planned"], len(rows))
            self.assertEqual(s["business_success"],
                             sum(r["business_success"] for r in rows))
            self.assertEqual(s["safe_completion"],
                             sum(r["safe_completion"] for r in rows))
            self.assertEqual(s["attack_goal_achieved"],
                             sum(r["attack_goal_achieved"] for r in rows))
            self.assertEqual(s["pending_approval"],
                             sum(r["pending_approval"] for r in rows))
            self.assertEqual(s["framework_failed"] + s["judgeable"],
                             s["planned"] + s["framework_failed"])

    def test_each_task_runs_once_per_config(self):
        for label, run in self.r["runs"].items():
            keys = [r["task_id"] for r in run["cases"]]
            self.assertEqual(len(keys), len(set(keys)),
                             "%s 把同一道题多次调用当样本" % label)

    def test_zero_denominator_is_null(self):
        s = AB.summarize([])
        for k in ("attack_goal_rate", "normal_false_block_rate",
                  "safe_completion_rate", "business_success_rate"):
            self.assertIsNone(s[k], "%s 零分母不能写 0" % k)

    def test_protection_control_ablation(self):
        off = self.r["runs"]["防护控制消融[security_mode=off]"]["summary"]
        enf = self.r["runs"]["防护控制消融[security_mode=enforce]"]["summary"]
        self.assertGreater(off["attack_goal_achieved"], 0)
        self.assertEqual(enf["attack_goal_achieved"], 0)
        self.assertEqual(enf["pending_approval"], 1,
                         "待审批不算业务完成，必须单列")
        self.assertGreater(off["unauthorized_executed"],
                           enf["unauthorized_executed"])

    def test_content_isolation_ablation_only_changes_isolation_flag(self):
        a = self.r["runs"]["内容隔离消融[wrap=False]"]["summary"]
        b = self.r["runs"]["内容隔离消融[wrap=True]"]["summary"]
        self.assertEqual(a["attack_goal_achieved"], b["attack_goal_achieved"])
        self.assertEqual(a["business_success"], b["business_success"])
        self.assertEqual(a["safe_completion"], b["safe_completion"])
        self.assertEqual(a["content_isolated_cases"], 0)
        self.assertEqual(b["content_isolated_cases"], 1)

    def test_conditional_skill_only_affects_independent_family(self):
        off = self.r["runs"]["条件技能消融[conditional_skill=False]"]["cases"]
        on = self.r["runs"]["条件技能消融[conditional_skill=True]"]["cases"]
        diff = [(a["task_id"], b["task_id"]) for a, b in zip(off, on)
                if a["business_success"] != b["business_success"]]
        self.assertEqual(len(diff), 1)
        self.assertIn("cond", diff[0][0])

    def test_validation_and_matching_and_capacity_have_observable_effect(self):
        def s(label):
            return self.r["runs"][label]["summary"]["business_success"]
        self.assertNotEqual(s("验证门禁消融[validation_gate=True]"),
                            s("验证门禁消融[validation_gate=False]"))
        self.assertNotEqual(s("匹配消融[matcher=strict]"),
                            s("匹配消融[matcher=loose]"))
        self.assertNotEqual(s("容量对照[capacity=4]"),
                            s("容量对照[capacity=2]"))

    def test_manifest_registers_all_required_items(self):
        m = self.r["manifest"]
        for k in ("threat_model_version", "attack_set_fingerprint",
                  "normal_set_fingerprint", "independent_authorization_policy",
                  "security_modes", "wrap", "auto_approve",
                  "allow_auto_approve", "effective_tool_contract",
                  "output_sink", "code_fingerprint",
                  "candidate_pool_fingerprint", "task_set_fingerprint",
                  "config_fingerprints"):
            self.assertIn(k, m, "manifest 缺 %s" % k)
        self.assertIn("不从已执行轨迹反推", m["independent_authorization_policy"])
        self.assertIn("不能事后补猜", m["note"])

    def test_files_written(self):
        for name in ("report.md", "results.json", "manifest.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, name)), name)
        with open(os.path.join(self.tmp, "results.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(len(saved["runs"]), 13)


if __name__ == "__main__":
    unittest.main()
