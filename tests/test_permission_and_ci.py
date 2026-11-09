# -*- coding: utf-8 -*-
"""Q01/Q02/Q03/Q04/Q05 回归护栏。

- Q02：白名单只能来自事先冻结的策略；反推白名单会把越权洗成允许。
- Q01/Q03：三档真值分开存；独立标注协议不把自生成标签冒充人工标签；零分母 null。
- Q04：按维度算覆盖，安全缺白名单不牵连可靠性。
- Q05：报告判定与流水线阻断分开保存，退出码 0 不等于全面安全。
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from evoguard_inspect import (assurance, ci_policy, config, coverage,  # noqa: E402
                              independent_labels as IL, loaders)
from evoguard_inspect.rules import registry, trace_rules  # noqa: E402
import security as S  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "eval_permission", os.path.join(ROOT, "scripts", "eval_permission.py"))
EP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EP)

FIX = os.path.join(ROOT, "tests", "fixtures")
LABELS = os.path.join(FIX, "labeled", "labels.json")
POLICIES = os.path.join(FIX, "labeled", "policies.json")
ATTACK = os.path.join(FIX, "trace_attack.json")


def _build(path, **kw):
    traces = loaders.load_file(path)
    d = dict(config.DEFAULTS)
    d.update(kw)
    cfg = config.Config(**d)
    issues = trace_rules.check_all(traces, cfg, registry.select())
    cov = coverage.analyze(traces, issues, cfg, registry.select_ids())
    return assurance.build(traces, issues, cfg, cov)


class TestQ02Permission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="perm_")
        with contextlib.redirect_stdout(io.StringIO()):
            cls.result = EP.main(["--out", cls.tmp])

    def test_policies_match_security_constants(self):
        with open(POLICIES, encoding="utf-8") as f:
            policies = json.load(f)["policies"]
        for agent, tools in S.AGENT_TOOLS.items():
            self.assertEqual(sorted(tools), sorted(policies[agent]),
                             "%s 的冻结策略与 security.AGENT_TOOLS 不一致" % agent)

    def test_reverse_derived_hides_unauthorized_tool(self):
        """反例：轨迹执行了白名单外工具，反推白名单不报、冻结策略报。"""
        tr = loaders.load_file(ATTACK)[0]
        reverse = sorted({s.tool or s.name for s in tr.tools()})
        with open(POLICIES, encoding="utf-8") as f:
            frozen = json.load(f)["policies"]["support-agent"]
        self.assertIn("http.post", reverse)
        self.assertNotIn("http.post", frozen)
        a = EP._run(tr, frozen)["unauthorized_hits"]
        b = EP._run(tr, reverse)["unauthorized_hits"]
        self.assertTrue(a, "冻结策略必须报越权")
        self.assertFalse(b, "反推白名单把越权洗成了允许")
        self.assertTrue(self.result["counterexample"]["frozen_reports"])
        self.assertFalse(self.result["counterexample"]["reverse_reports"])

    def test_applicable_rate_and_divergence(self):
        r = self.result
        self.assertEqual(r["n_total"], len(r["records"]))
        self.assertEqual(r["n_judgeable"] + r["n_unjudgeable"], r["n_total"])
        self.assertGreater(r["unauthorized_detected_frozen"],
                           r["unauthorized_detected_reverse"])
        self.assertTrue(r["divergence_cases"])

    def test_missing_policy_is_unjudgeable_not_silent_pass(self):
        """没有独立策略的样本必须标不可评、剔除分母。"""
        with open(LABELS, encoding="utf-8") as f:
            doc = json.load(f)
        doc["entries"][0]["agent"] = "agent_without_frozen_policy"
        d = tempfile.mkdtemp(prefix="perm_missing_")
        lp = os.path.join(d, "labels.json")
        with open(lp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        out = os.path.join(d, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            r = EP.main(["--out", out, "--labels", lp, "--policies", POLICIES])
        self.assertEqual(r["n_unjudgeable"], 1)
        self.assertLess(r["applicable_rate"], 1.0)
        miss = [x for x in r["records"] if not x["judgeable"]]
        self.assertEqual(len(miss), 1)
        self.assertEqual(miss[0]["policy_status"], "missing")
        self.assertIn("不可评", miss[0]["reason"])


class TestQ01Q03IndependentLabels(unittest.TestCase):
    def test_three_truth_dimensions_are_separate(self):
        rec = IL.make_label_record({
            "key": "x", "file": "f.json", "agent": "order", "label": "failed",
            "fault": "silent_corruption", "success": False, "note": "n"})
        t = rec["truth"]
        self.assertEqual(set(t), {"execution_state", "observable_fault",
                                  "business_outcome"})
        self.assertEqual(t["execution_state"], "failed")
        self.assertEqual(t["observable_fault"], "silent_corruption")
        self.assertEqual(t["business_outcome"], "failure")
        self.assertEqual(rec["label_basis"], "execution_ground_truth")
        self.assertEqual(rec["review_status"], IL.REVIEW_PENDING)
        self.assertIsNone(rec["independent_annotation"])

    def test_registry_does_not_claim_human_labels(self):
        with open(LABELS, encoding="utf-8") as f:
            doc = json.load(f)
        reg = IL.build_registry(doc)
        c = reg["counts"]
        self.assertEqual(c["self_generated"], len(doc["entries"]))
        self.assertEqual(c["external_authorized"], 0)
        self.assertEqual(c["independent_human_labels"], 0)
        self.assertEqual(c["pending_independent_review"], len(doc["entries"]))
        self.assertIsNone(reg["kappa"])
        self.assertTrue(all(e["review_status"] == IL.REVIEW_PENDING
                            for e in reg["entries"]))

    def test_kappa_zero_denominator_and_perfect_agreement_are_none(self):
        self.assertIsNone(IL.cohen_kappa([], []))
        self.assertIsNone(IL.cohen_kappa(["a", "a"], ["a", "a"]))
        self.assertIsNotNone(IL.cohen_kappa(
            ["a", "a", "b", "b"], ["a", "b", "b", "b"]))

    def test_disagreements_need_discussion_not_majority(self):
        a = [IL.make_label_record({"key": "k1", "label": "failed"}),
             IL.make_label_record({"key": "k2", "label": "clean"})]
        b = [IL.make_label_record({"key": "k1", "label": "clean"}),
             IL.make_label_record({"key": "k2", "label": "clean"})]
        out = IL.disagreements(a, b)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["key"], "k1")
        self.assertEqual(out[0]["status"], IL.REVIEW_NEEDS_DISCUSSION)
        self.assertIn("不直接取多数", out[0]["note"])

    def test_external_traces_require_source_and_authorization(self):
        with open(LABELS, encoding="utf-8") as f:
            doc = json.load(f)
        ext = [{"key": "e1"},
               {"key": "e2", "source": "partner", "authorization": "written"}]
        reg = IL.build_registry(doc, ext)
        self.assertEqual(reg["counts"]["external_authorized"], 1)
        self.assertEqual(reg["counts"]["external_rejected"], 1)


class TestQ04DimensionCoverage(unittest.TestCase):
    def test_each_dimension_has_its_own_coverage(self):
        data = _build(os.path.join(FIX, "trace_attack.json"),
                      allowed_tools=["kb.search"])
        dc = data["dimension_coverage"]
        self.assertEqual(set(dc), {"reliability", "security", "operability"})
        for name, c in dc.items():
            for k in ("applicable_rules", "checked_rules", "skipped_rules",
                      "skip_reasons", "coverage_ok", "observations"):
                self.assertIn(k, c, "%s 缺 %s" % (name, k))
        for name in ("reliability", "security", "operability"):
            self.assertIn("coverage", data["dimensions"][name])

    def test_missing_allowed_tools_only_limits_security(self):
        """安全缺白名单只压 security；可靠性覆盖不受这个配置牵连。

        注意：这条轨迹的可靠性组自己也有规则因缺字段没跑，所以不能断言
        reliability.coverage_ok=True —— 要断言的是「加不加 allowed_tools，
        可靠性的覆盖结论不变」。
        """
        path = os.path.join(FIX, "trace_retry_ok.json")
        no_policy = _build(path)
        with_policy = _build(path, allowed_tools=["kb.search"])
        a, b = no_policy["dimension_coverage"], with_policy["dimension_coverage"]
        self.assertFalse(a["security"]["coverage_ok"])
        self.assertIn("config:allowed_tools", a["security"]["missing_config"])
        self.assertTrue(b["security"]["coverage_ok"])
        self.assertEqual(a["reliability"]["coverage_ok"],
                         b["reliability"]["coverage_ok"],
                         "可靠性不该被安全缺配置牵连")
        # 维度必需配置只约束 security；可靠性不因安全策略被额外判缺口，
        # 它自己的 skip_reasons 里出现 allowed_tools 是它自己有条规则依赖该配置，
        # 属于事实，不是「被安全牵连」。
        self.assertEqual(a["reliability"]["missing_config"], [])
        self.assertNotEqual(a["security"]["coverage_ok"],
                            b["security"]["coverage_ok"],
                            "补上白名单只该修好安全维度")
        self.assertEqual(no_policy["dimensions"]["security"]["verdict"],
                         assurance.LIMITED)

    def test_security_coverage_ok_when_policy_present(self):
        data = _build(os.path.join(FIX, "trace_retry_ok.json"),
                      allowed_tools=["kb.search"])
        self.assertTrue(data["dimension_coverage"]["security"]["coverage_ok"])


class TestQ05CiPolicy(unittest.TestCase):
    def _data(self, verdict, sec_ok=True):
        return {
            "verdict": verdict,
            "dimension_coverage": {
                "security": {"coverage_ok": sec_ok, "missing_config": [],
                             "skipped_rules": [], "skip_reasons": []},
                "reliability": {"coverage_ok": True},
                "operability": {"coverage_ok": True},
            },
        }

    def test_fail_blocks_pipeline(self):
        d = ci_policy.evaluate(self._data("FAIL"))
        self.assertEqual(d["ci_action"], "block")
        self.assertTrue(d["block_pipeline"])
        self.assertEqual(d["exit_code"], 3)

    def test_risk_is_review_not_pass(self):
        d = ci_policy.evaluate(self._data("RISK"))
        self.assertEqual(d["ci_action"], "review")
        self.assertEqual(d["exit_code"], 0)
        self.assertNotEqual(d["ci_action"], "pass")
        self.assertIn("退出码 0 不等于全面安全", d["note"])

    def test_limited_and_unknown_are_blocked_by_default(self):
        for v in ("LIMITED", "UNKNOWN"):
            self.assertEqual(ci_policy.evaluate(self._data(v))["ci_action"],
                             "block", v)

    def test_security_rules_unevaluable_are_blocked(self):
        d = ci_policy.evaluate(self._data("PASS", sec_ok=False))
        self.assertEqual(d["ci_action"], "block")
        self.assertTrue(any("关键安全规则不可评" in r for r in d["reasons"]))

    def test_input_load_failure_is_blocked(self):
        d = ci_policy.evaluate(self._data("PASS"), load_failures=2)
        self.assertEqual(d["ci_action"], "block")
        self.assertTrue(any("解析失败" in r for r in d["reasons"]))

    def test_pass_only_for_this_configuration(self):
        d = ci_policy.evaluate(self._data("PASS"))
        self.assertEqual(d["ci_action"], "pass")
        self.assertTrue(any("只对本次配置" in r for r in d["reasons"]))

    def test_report_verdict_is_not_modified_by_policy(self):
        data = self._data("RISK")
        before = data["verdict"]
        ci_policy.evaluate(data, {"allow_verdicts": ["RISK"]})
        self.assertEqual(data["verdict"], before)
        self.assertNotIn("ci_action", data)

    def test_policy_fingerprint_is_stable(self):
        self.assertEqual(ci_policy.fingerprint(ci_policy.DEFAULT_POLICY),
                         ci_policy.fingerprint(dict(ci_policy.DEFAULT_POLICY)))
        self.assertNotEqual(
            ci_policy.fingerprint(ci_policy.DEFAULT_POLICY),
            ci_policy.fingerprint(dict(ci_policy.DEFAULT_POLICY,
                                       allow_limited=True)))

    def test_cli_writes_separate_decision_and_exit_code(self):
        """CLI 带 --policy：报告判定与流水线决策分文件，退出码按策略。"""
        out = tempfile.mkdtemp(prefix="ci_cli_")
        cmd = [sys.executable, "-m", "evoguard_inspect.cli",
               "--trace", os.path.join(FIX, "trace_attack.json"),
               "--assurance", "--policy", os.path.join(FIX, "ci_policy.json"),
               "--out", out]
        r = subprocess.run(cmd, cwd=os.path.join(ROOT, "ai-service"),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        with open(os.path.join(out, "ci_decision.json"), encoding="utf-8") as f:
            dec = json.load(f)
        self.assertEqual(dec["report_verdict"], "FAIL")
        self.assertEqual(dec["ci_action"], "block")
        self.assertTrue(dec["block_pipeline"])
        with open(os.path.join(out, "assurance.json"), encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertNotIn("ci_action", report, "报告判定与流水线决策要分开保存")


if __name__ == "__main__":
    unittest.main()
