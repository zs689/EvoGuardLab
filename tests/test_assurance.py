# -*- coding: utf-8 -*-
"""保障视图测试：三个维度必须各自判得对，而且不许把「没测」说成「通过」。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import assurance, config, coverage, loaders   # noqa: E402
from evoguard_inspect.model import Step, Trace                      # noqa: E402
from evoguard_inspect.rules import registry, trace_rules as R        # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def load(name):
    return loaders.load_file(os.path.join(FIX, name))


def _cfg(**kw):
    from evoguard_inspect.config import DEFAULTS
    d = dict(DEFAULTS)
    d.update(kw)
    return config.Config(**d)


def _build(traces, cfg):
    ids = list(registry.RULES)
    issues = R.check_all(traces, cfg)
    cov = coverage.analyze(traces, issues, cfg, ids)
    return assurance.build(traces, issues, cfg, cov)


class TestAssuranceVerdicts(unittest.TestCase):
    def test_attack_trace_fails_security(self):
        data = _build(load("trace_attack.json"), _cfg(allowed_tools=["kb.search"]))
        self.assertEqual(data["dimensions"]["security"]["verdict"], assurance.FAIL)
        self.assertEqual(data["verdict"], assurance.FAIL)
        self.assertGreaterEqual(data["dimensions"]["security"]["findings"], 6)

    def test_clean_trace_is_not_passed_when_coverage_incomplete(self):
        """干净轨迹但配置没配全 → 覆盖有限，不能写「通过」。"""
        data = _build(load("trace_retry_ok.json"), _cfg())
        self.assertEqual(data["dimensions"]["security"]["verdict"], assurance.LIMITED)
        self.assertIn(data["verdict"], (assurance.LIMITED, assurance.RISK))

    def test_no_traces_is_unknown(self):
        data = _build([], _cfg())
        self.assertEqual(data["verdict"], assurance.UNKNOWN)
        for key in ("reliability", "security", "operability"):
            self.assertEqual(data["dimensions"][key]["verdict"], assurance.UNKNOWN)

    def test_model_error_with_answer_is_risk(self):
        """模型报错后仍有答案 → 至少是风险；不能因为「有答案」就当健康。"""
        # 10 步里 1 步报错（10% 低于 20% 阈值），但最终仍有答案 → RISK 而非 PASS
        steps = [Step(seq=1, kind="chat", name="model", status="error",
                      error="E_LLM_TIMEOUT")]
        steps += [Step(seq=i + 2, kind="chat", name="model") for i in range(9)]
        tr = Trace(trace_id="t-deg", agent="demo", task="答个问题",
                   steps=steps, answer={"text": "答案是 42"}, source="test")
        data = _build([tr], _cfg(expect_keys=["text"], allowed_tools=[]))
        op = data["dimensions"]["operability"]
        self.assertEqual(op["metrics"]["model_error_with_answer"], 1)
        self.assertEqual(op["verdict"], assurance.RISK,
                         "报错率 10% + 有答案，可运行性判风险而不是通过")
        # 总判定取三个维度里最差的；这条轨迹的可靠维度另有 error，所以总体会更差
        self.assertIn(data["verdict"], (assurance.RISK, assurance.FAIL))

    def test_healthy_run_passes_operability(self):
        tr = Trace(trace_id="t-ok", agent="demo", task="算一下",
                   steps=[Step(seq=1, kind="chat", name="model", tokens=10, ts=1.0),
                          Step(seq=2, kind="tool", name="calc", tool="calc.add",
                               args={"a": 1}, result="3", duration_ms=12, ts=2.0)],
                   answer={"text": "3"}, source="test")
        data = _build([tr], _cfg(expect_keys=["text"], allowed_tools=["calc.add"]))
        self.assertEqual(data["dimensions"]["operability"]["verdict"], assurance.PASS)

    def test_high_model_error_rate_fails(self):
        steps = [Step(seq=i + 1, kind="chat", name="model",
                      status="error" if i < 4 else "ok", error="E_LLM_500")
                 for i in range(5)]
        tr = Trace(trace_id="t-err", agent="demo", task="x", steps=steps,
                   answer=None, source="test")
        data = _build([tr], _cfg())
        self.assertEqual(data["dimensions"]["operability"]["verdict"], assurance.FAIL)


class TestAssuranceReport(unittest.TestCase):
    def test_report_names_every_dimension_and_its_gaps(self):
        data = _build(load("trace_attack.json"), _cfg(allowed_tools=["kb.search"]))
        text = assurance.render(data)
        for word in ("可靠", "防护", "可运行", "没测什么", "这份报告的边界"):
            self.assertIn(word, text)
        # 每条边界都必须说清楚「不等于什么」
        self.assertIn("特征检测可被绕过", text)
        self.assertIn("不判断业务上该不该", text)

    def test_no_score_field(self):
        """故意不提供 0-100 分：分数会让人优化分数而不是优化系统。"""
        data = _build(load("trace_attack.json"), _cfg())
        self.assertNotIn("score", data)
        self.assertNotIn("score", data["dimensions"]["security"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
