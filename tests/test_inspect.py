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

from evoguard_inspect import config, loaders, report   # noqa: E402
from evoguard_inspect.rules import registry              # noqa: E402
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

    def test_loop_detected(self):
        # 原名 repeated_tool_call，扩规则库时按「流程层」语义改名
        self.assertIn("loop_detected", self.hit)

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
        self.assertEqual(j["traces"], 17, "合成夹具现在是 17 条轨迹（含 6 条事故模式）")
        self.assertIn("by_severity", j["summary"])
        md = report.to_markdown(traces, issues)
        self.assertIn("Agent 离线检查报告", md)
        self.assertIn("可能是误报", md, "报告必须显示误报提示，否则用户会当成铁证")

    def test_empty_result_is_honest(self):
        """零命中时必须说明「规则集覆盖不到的风险它报不出来」。"""
        md = report.to_markdown([], [])
        self.assertIn("不等于 Agent 没问题", md)


class TestComplexAgent(unittest.TestCase):
    """复杂 Agent 场景：多写操作、循环、PII、工具幻觉、token 爆炸、参数为空。

    这些是「通用 Agent」才会遇到的形态，不是我这个 sim 特有的。
    夹具里每条轨迹只坏一处，所以「命中」必须能对上具体那条。
    """

    @classmethod
    def setUpClass(cls):
        cls.cfg = config.load(os.path.join(FIX, "inspect_config.json"))
        cls.traces = load("trace_synthetic_bad.json")
        cls.issues = R.check_all(cls.traces, cls.cfg)
        cls.by_trace = {}
        for i in cls.issues:
            cls.by_trace.setdefault(i["trace_id"], set()).add(i["rule_id"])

    def _hit(self, tid, rid):
        self.assertIn(rid, self.by_trace.get(tid, set()),
                      "%s 应命中 %s，实际命中 %s" % (tid, rid, self.by_trace.get(tid)))

    def test_pii_leak(self):
        self._hit("t-pii-0005", "pii_leak")

    def test_tool_name_unknown(self):
        self._hit("t-badtool-0006", "tool_name_unknown")

    def test_parallel_write_conflict(self):
        self._hit("t-parallel-write-0007", "parallel_write_conflict")

    def test_cycle_detected(self):
        self._hit("t-cycle-0008", "cycle_detected")

    def test_token_blowup(self):
        self._hit("t-token-0009", "token_blowup")

    def test_tool_arg_incomplete(self):
        self._hit("t-argempty-0010", "tool_arg_incomplete")

    def test_answer_schema_missing_with_config(self):
        cfg = config.Config(expect_keys=["total"])
        issues = R.check_all(self.traces, cfg)
        ids = {i["rule_id"] for i in issues if i["trace_id"] == "t-schema-0011"}
        self.assertIn("answer_schema_missing", ids)

    def test_result_unused_does_not_flood(self):
        """只有「产出了值却没被引用」才报 —— 第一版 11 条轨迹全命中，是没区分度。

        回归断言：命中数不能超过轨迹数的一半，否则说明规则又变回噪音了。
        """
        n = len([i for i in self.issues if i["rule_id"] == "tool_result_unused"])
        self.assertLessEqual(n, len(self.traces) // 2,
                             "tool_result_unused 报了 %d 次，又开始刷屏了" % n)

    def test_rule_library_shape(self):
        """规则库必须分组、且每条都有误报说明 —— 没有它用户会把警告当铁证。"""
        self.assertGreaterEqual(len(registry.RULES), 18)
        for rid, m in registry.RULES.items():
            self.assertIn(m["group"], registry.GROUPS, "%s 分组非法" % rid)
            self.assertTrue(m["false_positive"], "%s 缺误报说明" % rid)
            self.assertIn(m["severity"], ("error", "warn", "info"))

    def test_group_filter(self):
        only_flow = registry.select(groups=["flow"])
        self.assertTrue(only_flow)
        issues = R.check_all(self.traces, self.cfg, only_flow)
        self.assertEqual({i["group"] for i in issues}, {"flow"})

    def test_config_rejects_unknown_key(self):
        """配置拼错的键必须报错，静默忽略会让人以为规则生效了。"""
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"write_tool": ["x"]}, f)   # 故意少个 s
            with self.assertRaises(ValueError):
                config.load(p)
        finally:
            os.unlink(p)

    def test_write_detection_uses_config(self):
        """写操作识别必须走配置：换个 Agent 工具名不一样，硬编码就没用了。"""
        cfg = config.Config(write_tools=["billing.charge"], read_only_tools=["kb.search"])
        self.assertTrue(cfg.is_write("billing.charge"))
        self.assertFalse(cfg.is_write("kb.search"))
        self.assertTrue(config.Config().is_write("order.settle"), "关键词兜底要生效")


class TestCaseRules(unittest.TestCase):
    """公开事故模式提炼的 6 条轨迹规则。

    同样两类一起测：坏轨迹逐条命中，负例不许误报。特别是
    「工具报错 → 同工具重试 → 成功」这条轨迹，绝不能被判成「谎报成功」——
    那会把健康处理了故障的 Agent 也标红，这类误报最毁工具可信度。
    """

    NEW_RULES = {
        "self_report_contradiction", "retry_storm", "silent_stall",
        "write_timeout_ambiguous", "retry_on_permanent_error",
        "hallucination_propagation",
    }
    CASES = {
        "t-liereport-0012": "self_report_contradiction",
        "t-retrystorm-0013": "retry_storm",
        "t-stall-0014": "silent_stall",
        "t-writetimeout-0015": "write_timeout_ambiguous",
        "t-permanenterr-0016": "retry_on_permanent_error",
        "t-propagation-0017": "hallucination_propagation",
    }

    @classmethod
    def setUpClass(cls):
        cls.traces = load("trace_synthetic_bad.json")
        cls.issues = R.check_all(cls.traces, config.DEFAULT)
        cls.by_trace = {}
        for i in cls.issues:
            cls.by_trace.setdefault(i["trace_id"], set()).add(i["rule_id"])

    def _hit(self, tid, rid):
        self.assertIn(rid, self.by_trace.get(tid, set()),
                      "%s 应命中 %s，实际 %s" % (tid, rid, self.by_trace.get(tid)))

    def test_all_six_case_rules_fire(self):
        self.assertEqual(len(self.traces), 17)
        for tid, rid in self.CASES.items():
            self._hit(tid, rid)

    def test_negative_traces_no_case_false_positive(self):
        """干净 / 重试成功 / 真实幻觉轨迹，新规则一条都不许命中。"""
        for name in ("trace_clean.json", "trace_retry_ok.json",
                     "trace_hallucination.json", "trace_langsmith.json"):
            ids = {i["rule_id"] for i in R.check_all(load(name), config.DEFAULT)}
            self.assertFalse(ids & self.NEW_RULES,
                             "%s 被新规则误报：%s" % (name, ids & self.NEW_RULES))

    def test_retry_ok_is_not_lying(self):
        """回归断言：失败后同工具重试成功，说「完成」是诚实的。"""
        ids = {i["rule_id"] for i in R.check_all(load("trace_retry_ok.json"))}
        self.assertNotIn("self_report_contradiction", ids)

    def test_rule_library_shape(self):
        """扩库后 25 条轨迹规则 / 20 条静态规则，且每条都有误报说明。"""
        from evoguard_inspect.static import rules as SR
        self.assertEqual(len(registry.RULES), 25)
        self.assertEqual(len(SR.RULES), 20)
        for rid, m in list(registry.RULES.items()) + list(SR.RULES.items()):
            self.assertTrue(m["false_positive"], "%s 缺误报说明" % rid)
            self.assertIn(m["severity"], ("error", "warn", "info"))

    def test_case_rules_have_fix(self):
        for i in self.issues:
            if i["rule_id"] in self.NEW_RULES:
                self.assertTrue(i["fix"], "%s 缺修复建议" % i["rule_id"])
                self.assertTrue(i["detail"], "%s 缺事实描述" % i["rule_id"])


class TestCoverageAndExitCodes(unittest.TestCase):
    """P0 07 / 第 9 章：零命中不等于检查通过，退出码要能分开三种结果。"""

    def _trace(self, **kw):
        steps = [Step(seq=1, kind="chat", name="model", tokens=120, ts=1000.0,
                      raw={"input": "查一下发票时限"}),
                 Step(seq=2, kind="tool", name="kb.search", tool="kb.search",
                      args={"q": "发票时限"}, result="发票时限为 30 天",
                      tokens=0, ts=1001.0),
                 Step(seq=3, kind="tool", name="crm.get", tool="crm.get",
                      args={"id": "C1"}, result="客户 C1 已建档", ts=1002.0),
                 Step(seq=4, kind="chat", name="model", tokens=80, ts=1003.0,
                      raw={"input": "汇总"})]
        base = dict(trace_id="t1", agent="demo", task="查发票时限",
                    steps=steps, answer={"text": "30 天"}, meta={}, source="test")
        base.update(kw)
        return Trace(**base)

    def _cfg(self, **kw):
        from evoguard_inspect.config import DEFAULTS
        d = dict(DEFAULTS)
        d.update({"expect_keys": ["text"], "allowed_tools": ["kb.search", "crm.get"]})
        d.update(kw)
        return config.Config(**d)

    def test_requires_table_only_names_real_rules(self):
        from evoguard_inspect import coverage
        unknown = set(coverage.TRACE_REQUIRES) - set(registry.RULES)
        self.assertFalse(unknown, "条件表里出现未注册规则：%s" % unknown)

    def test_zero_hits_with_skipped_rules_is_not_pass(self):
        from evoguard_inspect import coverage
        tr = self._trace()
        cov = coverage.analyze([tr], [], config.load(None))
        self.assertGreater(cov["skipped_rules"], 0)
        self.assertEqual(cov["verdict"], "COVERAGE_LIMITED",
                         "有规则没执行时不能报「通过」")

    def test_all_runnable_and_clean_is_pass(self):
        from evoguard_inspect import coverage
        tr = self._trace()
        cov = coverage.analyze([tr], [], self._cfg())
        self.assertEqual(cov["runnable_rules"], cov["total_rules"],
                         "补齐配置与轨迹字段后应全部可执行：%s"
                         % [r["rule_id"] for r in cov["rules"] if not r["runnable"]])
        self.assertEqual(cov["verdict"], "PASS")

    def test_hit_forces_runnable_and_flags_table(self):
        from evoguard_inspect import coverage
        tr = self._trace()
        # tool_name_unknown 需要配置里的 allowed_tools；这里不配 → 条件表判它不可执行。
        fake = [{"rule_id": "tool_name_unknown", "severity": "warn", "group": "tool",
                 "trace_id": "t1", "detail": "x"}]
        cov = coverage.analyze([tr], fake, config.load(None))
        row = [r for r in cov["rules"] if r["rule_id"] == "tool_name_unknown"][0]
        self.assertTrue(row["runnable"], "命中了就说明它跑了，不能记成跳过")
        self.assertTrue(cov["table_warnings"], "条件表与行为不一致必须报出来")

    def test_empty_input_verdict(self):
        from evoguard_inspect import coverage
        cov = coverage.analyze([], [], config.load(None))
        self.assertEqual(cov["verdict"], "EMPTY_INPUT")

    def test_parse_failure_is_reported_not_swallowed(self):
        good = os.path.join(FIX, "trace_retry_ok.json")
        bad = os.path.join(FIX, "does_not_exist.json")
        traces, failures = report.inspect_files_safe([good, bad])
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("does_not_exist", failures[0]["path"])

    def test_cli_exit_codes(self):
        from evoguard_inspect import cli
        bad = os.path.join(FIX, "trace_synthetic_bad.json")
        good = os.path.join(FIX, "trace_retry_ok.json")
        devnull = open(os.devnull, "w", encoding="utf-8")
        old = sys.stdout
        try:
            sys.stdout = devnull
            self.assertEqual(cli.main(["--trace", good]), 0)
            self.assertEqual(cli.main(["--trace", bad, "--fail-on", "error"]), 3,
                             "发现问题超过阈值要给 CI 一个非 0 退出码")
            self.assertEqual(cli.main(["--trace", bad]), 0,
                             "不指定 --fail-on 时不应因为「有问题」就失败")
            self.assertEqual(cli.main(["--trace", os.path.join(FIX, "nope.json")]), 2,
                             "输入解析失败必须是 2")
            self.assertEqual(cli.main(["--trace", good,
                                       "--trace", os.path.join(FIX, "nope.json"),
                                       "--strict-input"]), 2,
                             "--strict-input 下部分失败也要报 2")
        finally:
            sys.stdout = old
            devnull.close()


if __name__ == "__main__":
    unittest.main()
