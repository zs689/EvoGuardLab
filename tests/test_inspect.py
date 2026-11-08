# -*- coding: utf-8 -*-
"""离线检查器测试：证明「抓得住」和「不冤枉」。

这两件事必须一起测。只测命中，规则可以乱报；只测不误报，规则可以什么都不报。
所以夹具分三类：
  1. 合成的「已知错误」→ 每条规则都必须命中；
  2. 真实成功轨迹 → 必须零命中；
  3. 真实「工具报错后重试成功」→ 不许报 error_swallowed（这是最容易被误伤的场景）。

跑法（在 EvoGuardLab 目录下）：
    python -m unittest tests.test_inspect -v
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import loaders, report          # noqa: E402
from evoguard_inspect.model import Step, Trace        # noqa: E402
from evoguard_inspect.rules import trace_rules as R    # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def load(name):
    return loaders.load_file(os.path.join(FIX, name))


class TestSyntheticHitsEveryRule(unittest.TestCase):
    """合成夹具里的四类错误，每条规则都要抓到。"""

    @classmethod
    def setUpClass(cls):
        cls.issues = R.check_all(load("trace_synthetic_bad.json"))
        cls.hit = {i["rule_id"] for i in cls.issues}

    def test_error_swallowed(self):
        self.assertIn("error_swallowed", self.hit)

    def test_write_before_read(self):
        self.assertIn("write_before_read", self.hit)

    def test_repeated_tool_call(self):
        self.assertIn("repeated_tool_call", self.hit)

    def test_empty_answer(self):
        self.assertIn("empty_answer", self.hit)

    def test_hallucinated_number(self):
        self.assertIn("hallucinated_number", self.hit)

    def test_every_issue_has_evidence(self):
        """每条报告都必须能指到具体轨迹；有 step 的要给步号。"""
        for i in self.issues:
            self.assertTrue(i["trace_id"], "缺 trace_id：%s" % i)
            self.assertIn(i["severity"], ("error", "warn", "info"))
            self.assertTrue(i["detail"], "缺事实描述：%s" % i)


class TestNoFalsePositive(unittest.TestCase):
    """干净轨迹不许报警 —— 误报是这类工具的头号死因。"""

    def test_clean_trace_is_silent(self):
        issues = R.check_all(load("trace_clean.json"))
        self.assertEqual(issues, [], "干净轨迹被误报：%s" % issues)

    def test_retry_success_is_not_swallowed(self):
        """工具报错后**重试成功**，不算「吞掉错误」。

        这是最容易被误伤的场景：只判「有 error 就报警」的规则会把
        正确处理了故障的 Agent 也标成问题。
        """
        issues = R.check_all(load("trace_retry_ok.json"))
        ids = [i["rule_id"] for i in issues]
        self.assertNotIn("error_swallowed", ids)

    def test_real_hallucination_is_caught(self):
        """真实数据里那条工具输出幻觉必须抓到（否则规则形同虚设）。"""
        issues = R.check_all(load("trace_hallucination.json"))
        self.assertIn("hallucinated_number", {i["rule_id"] for i in issues})


class TestLoaders(unittest.TestCase):
    def test_detect_otel(self):
        with open(os.path.join(FIX, "trace_clean.json"), encoding="utf-8") as f:
            self.assertEqual(loaders.detect(json.load(f)), "otel")

    def test_detect_langsmith(self):
        with open(os.path.join(FIX, "trace_langsmith.json"), encoding="utf-8") as f:
            self.assertEqual(loaders.detect(json.load(f)), "langsmith")

    def test_langsmith_end_to_end(self):
        """LangSmith 导出也能被同一套规则检查 —— 这就是「适配别的 Agent」的意义。"""
        traces = load("trace_langsmith.json")
        self.assertEqual(len(traces), 1)
        t = traces[0]
        self.assertEqual(t.agent, "demo-langgraph-agent")
        self.assertEqual(len(t.tools()), 1)
        self.assertEqual(t.tools()[0].tool, "order.get_order")
        self.assertEqual(t.answer, {"total": 999.0})
        ids = {i["rule_id"] for i in R.check_all(traces)}
        self.assertIn("hallucinated_number", ids, "999 不在任何工具输出里，应被抓到")

    def test_unwrap_nested_answer(self):
        """答案常被包一层 {"parsed": ...}，不拆开数字规则会失效（踩过）。"""
        from evoguard_inspect.loaders.otel import unwrap_answer
        self.assertEqual(unwrap_answer({"parsed": {"total": 1}, "text": "x"}), {"total": 1})
        self.assertEqual(unwrap_answer({"total": 2}), {"total": 2})

    def test_unknown_doc_raises(self):
        """认不出的格式必须明确报错，不能静默返回空列表 ——
        静默返回 0 条轨迹会被误读成「没问题」。"""
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"foo": 1}, f)
            with self.assertRaises(ValueError):
                loaders.load_file(p)
        finally:
            os.unlink(p)


class TestRuleIsolation(unittest.TestCase):
    """单条规则崩了不能拖垮整份报告 —— 否则一个边界情况就让检查器全废。"""

    def test_crashing_rule_is_contained(self):
        def boom(trace):
            raise RuntimeError("故意炸")
        old = list(R.ALL_RULES)
        try:
            R.ALL_RULES.insert(0, boom)
            t = Trace(trace_id="t1", agent="a", task="x",
                      steps=[Step(1, "tool", "kb.search", tool="kb.search", result="{}")])
            issues = R.check(t)
            self.assertTrue(any(i["rule_id"] == "rule_crashed" for i in issues))
        finally:
            R.ALL_RULES[:] = old


class TestReportShape(unittest.TestCase):
    def test_json_and_markdown(self):
        traces = load("trace_synthetic_bad.json")
        issues = R.check_all(traces)
        j = report.to_json(traces, issues)
        self.assertEqual(j["traces"], 4)
        self.assertIn("by_severity", j["summary"])
        md = report.to_markdown(traces, issues)
        self.assertIn("Agent 离线检查报告", md)
        self.assertIn("可能是误报", md, "报告必须显示误报提示，否则用户会当成铁证")

    def test_empty_result_is_honest(self):
        """零命中时必须说明「规则集覆盖不到的风险它报不出来」。"""
        md = report.to_markdown([], [])
        self.assertIn("不等于 Agent 没问题", md)


if __name__ == "__main__":
    unittest.main()
