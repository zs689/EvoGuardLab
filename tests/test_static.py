# -*- coding: utf-8 -*-
"""静态检查测试：不执行 Agent，只看定义。

两类一起测（同轨迹检查器）：
  1. 坏定义 → 该报的都报出来；
  2. 好定义 → 零命中（误报是这类工具的死因）。

跑法：python -m unittest tests.test_static -v
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import config                        # noqa: E402
from evoguard_inspect.static import loader, rules as SR    # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def defn(name):
    return loader.load_definition(os.path.join(FIX, name))


class TestBadDefinition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = defn("agent_bad.json")
        cls.issues = SR.check(cls.d)
        cls.ids = {i["rule_id"] for i in cls.issues}

    def test_high_risk_rules_fire(self):
        for rid in ("tool_write_no_approval", "unsafe_shell_tool",
                    "prompt_pii", "prompt_no_refusal_boundary",
                    "tool_desc_missing", "no_timeout", "no_max_iterations"):
            self.assertIn(rid, self.ids, "坏定义应命中 %s" % rid)

    def test_evidence_present(self):
        for i in self.issues:
            self.assertTrue(i["evidence"] or i["detail"], "缺少证据：%s" % i)
            self.assertTrue(i["fix"], "缺少修复建议：%s" % i)
            self.assertTrue(i["false_positive"], "缺少误报说明：%s" % i)

    def test_all_rules_have_metadata(self):
        self.assertGreaterEqual(len(SR.RULES), 16)
        for rid, m in SR.RULES.items():
            self.assertIn(m["group"], SR.GROUPS)
            self.assertTrue(m["false_positive"], "%s 缺误报说明" % rid)


class TestGoodDefinition(unittest.TestCase):
    def test_no_false_positive(self):
        """写得合格的定义必须零命中 —— 否则用户会直接关掉这个工具。

        注意：这不是「改数据让测试过」。规则库从 16 条扩到 20 条后，
        「合格」的标准本身提高了：agent_good.json 被补上了退避/熔断、
        单次运行预算、幂等键、工具结果不可信声明。缺这些，在 2025-2026
        年的公开事故里就是会出事的（见 case_rules.py 的案例对照）。
        """
        issues = SR.check(defn("agent_good.json"))
        self.assertEqual(issues, [], "合格定义被误报：%s" % [i["rule_id"] for i in issues])


class TestSecretAndPrompt(unittest.TestCase):
    def test_secret_in_prompt(self):
        issues = SR.check(defn("agent_secret.json"))
        ids = {i["rule_id"] for i in issues}
        self.assertIn("prompt_secret_leak", ids)

    def test_missing_prompt(self):
        d = loader.from_json({"name": "x", "tools": [{"name": "a", "description": "d"}]})
        self.assertIn("prompt_missing", {i["rule_id"] for i in SR.check(d)})


class TestRepoScan(unittest.TestCase):
    """仓库模式：只读文本、不执行，且置信度必须是 low。"""

    @classmethod
    def setUpClass(cls):
        cls.d = loader.load_repo(os.path.join(FIX, "mini_repo"))

    def test_extracts_prompt_and_tools(self):
        self.assertIn("运维助手", self.d.system_prompt)
        names = {t.name for t in self.d.tools}
        self.assertTrue(names, "应提取到工具")

    def test_confidence_is_low(self):
        self.assertEqual(self.d.confidence, "low",
                         "源码提取必须标 low，否则用户会把猜测当事实")

    def test_shell_tool_flagged(self):
        ids = {i["rule_id"] for i in SR.check(self.d)}
        self.assertIn("unsafe_shell_tool", ids)

    def test_loader_never_executes(self):
        """仓库加载器只能读文本：源码里放一行会写文件的语句，验证它没被执行。"""
        import tempfile
        marker = os.path.join(tempfile.gettempdir(), "evoguard_pwned_marker")
        if os.path.exists(marker):
            os.remove(marker)
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "evil.py"), "w", encoding="utf-8") as f:
            f.write("open(%r, 'w').write('pwned')\n" % marker)
        loader.load_repo(root)
        self.assertFalse(os.path.exists(marker), "仓库加载器执行了被检代码！")


class TestConfigKeys(unittest.TestCase):
    def test_unknown_key_rejected(self):
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"write_tool": []}, f)
            with self.assertRaises(ValueError):
                config.load(p)
        finally:
            os.unlink(p)


class TestFrameworkAdapters(unittest.TestCase):
    """提取器必须认识各框架**特有的写法**。

    这些片段来自真实框架示例。第一版只认「大写常量赋值三引号 prompt」和
    `@tool` 装饰器，扫 AutoGen / PydanticAI 时一个定义都拿不到 —— 那不算
    「适配市面上几乎所有 agent」，所以逐个补上并固化成回归测试。
    """
    from evoguard_inspect.static import model as SM  # noqa: E402

    def _def(self, code):
        return self.SM.from_repo_files([("agent.py", code)], source="agent.py")

    def test_autogen_system_message(self):
        d = self._def('agent = AssistantAgent(\n'
                      '    name="assistant",\n'
                      '    system_message="You are a helpful AI assistant.",\n)')
        self.assertIn("helpful AI assistant", d.system_prompt)

    def test_google_adk_instruction(self):
        d = self._def('root_agent = LlmAgent(\n'
                      '    instruction="You are a support agent. Only answer from the knowledge base.",\n)')
        self.assertIn("knowledge base", d.system_prompt)

    def test_crewai_role_goal_backstory(self):
        d = self._def('researcher = Agent(\n'
                      '    role="Senior Research Analyst",\n'
                      '    goal="Find accurate and current market data",\n)')
        self.assertTrue(d.system_prompt)

    def test_pydanticai_prefixed_tool_decorator(self):
        d = self._def('@agent.tool\n'
                      'def get_weather(ctx: RunContext[Deps], city: str) -> str:\n'
                      '    """Get the weather for a city."""\n'
                      '    return "sunny"\n')
        names = [t.name for t in d.tools]
        self.assertIn("get_weather", names)
        self.assertTrue(next(t for t in d.tools if t.name == "get_weather").description,
                        "docstring 要一起抓，否则 tool_desc_missing 会刷屏")

    def test_multiline_string_concatenation(self):
        d = self._def('agent = Agent(\n'
                      '    system_prompt=(\n'
                      '        "You are a billing assistant. "\n'
                      '        "Only answer from tool results."\n'
                      '    ),\n)')
        self.assertIn("billing assistant", d.system_prompt)
        self.assertIn("tool results", d.system_prompt)

    def test_confidence_is_always_low_for_repo(self):
        self.assertEqual(self._def('SYSTEM_PROMPT = """' + "x" * 40 + '"""').confidence,
                         "low")


class TestNeedsHighConfidence(unittest.TestCase):
    """「缺失型」规则只在显式定义上生效，源码猜测对象不下这类结论。"""

    def test_missing_rules_skip_low_confidence(self):
        d = loader.from_json({
            "name": "x", "system_prompt": "你是助手，负责查询资料。",
            "tools": [{"name": "shell.exec", "description": "执行命令"}],
            "config": {},
        })
        ids_high = {i["rule_id"] for i in SR.check(d)}
        self.assertIn("no_timeout", ids_high, "显式定义缺配置应当报")

        d.confidence = "low"
        ids_low = {i["rule_id"] for i in SR.check(d)}
        self.assertNotIn("no_timeout", ids_low, "源码里没读到 ≠ 运行时没有")
        self.assertNotIn("no_max_iterations", ids_low)
        self.assertIn("unsafe_shell_tool", ids_low,
                      "「存在型」证据在低置信度下仍然成立")

    def test_all_missing_rules_are_marked(self):
        for rid in ("no_timeout", "no_retry_policy", "no_max_iterations",
                    "no_output_schema", "prompt_missing",
                    "prompt_no_refusal_boundary", "tool_desc_missing"):
            self.assertTrue(SR.RULES[rid].get("needs_high"),
                            "%s 应该标 needs_high" % rid)


class TestCaseRulesStatic(unittest.TestCase):
    """4 条从公开事故来的静态规则：能命中和「不乱报」都要测。"""

    NEW_RULES = {
        "prompt_no_untrusted_boundary", "retry_without_backoff",
        "no_run_budget", "write_tool_no_idempotency",
    }

    def test_bad_definition_fires_case_rules(self):
        ids = {i["rule_id"] for i in SR.check(defn("agent_bad.json"))}
        for rid in ("prompt_no_untrusted_boundary", "no_run_budget",
                    "write_tool_no_idempotency"):
            self.assertIn(rid, ids, "坏定义应命中 %s" % rid)

    def test_low_confidence_definition_is_skipped(self):
        """仓库扫描提取的定义是猜的，不能拿它判「有没有写边界/预算」。

        不跳过的话，扫 9 个框架示例时会必然刷屏 —— 那是提取器的局限，
        不是被检对象的问题。
        """
        d = loader.from_json({
            "name": "repo-guess",
            "system_prompt": "你是一个助手",
            "tools": [{"name": "web.fetch", "description": "抓取网页"},
                      {"name": "order.settle", "description": "结算订单"}],
            "config": {"max_retries": 3},
        })
        d.confidence = "low"
        ids = {i["rule_id"] for i in SR.check(d)}
        self.assertFalse(ids & self.NEW_RULES,
                         "低置信度定义不该下这类结论：%s" % (ids & self.NEW_RULES))

    def test_good_definition_still_clean(self):
        """20 条规则全开，生产级定义依然零命中。"""
        ids = {i["rule_id"] for i in SR.check(defn("agent_good.json"))}
        self.assertEqual(ids, set(), "合格定义被误报：%s" % ids)

    def test_case_rules_are_registered(self):
        self.assertEqual(len(SR.RULES), 20)
        self.assertTrue(self.NEW_RULES.issubset(set(SR.RULES)))


class TestStaticCoverage(unittest.TestCase):
    """静态侧的「规则适用性」：低置信度定义上跑不了的规则必须报出来。"""

    def test_requires_table_only_names_real_rules(self):
        from evoguard_inspect import coverage
        unknown = set(coverage.STATIC_REQUIRES) - set(SR.RULES)
        self.assertFalse(unknown, "条件表里出现未注册规则：%s" % unknown)

    def test_good_definition_runs_every_rule(self):
        from evoguard_inspect import coverage
        d = loader.load_any(os.path.join(FIX, "agent_good.json"))
        issues = SR.check(d)
        cov = coverage.analyze_static([d], issues, config.DEFAULT)
        missing = [r["rule_id"] for r in cov["rules"] if not r["runnable"]]
        self.assertEqual(missing, [], "显式定义应让 20 条规则全部可执行")
        self.assertEqual(cov["verdict"], "PASS")
        self.assertEqual(issues, [])

    def test_low_confidence_cannot_claim_pass(self):
        from evoguard_inspect import coverage
        from evoguard_inspect.static.model import AgentDef, ToolDef
        d = AgentDef(name="guessed", system_prompt="你是一个查询助手，查不到就说查不到。",
                     tools=[ToolDef(name="kb.search", description="检索知识库",
                                    parameters={"properties": {"q": {"type": "string"}}})],
                     config={"timeout_s": 10, "max_iterations": 5,
                             "token_budget_per_run": 20000},
                     source="repo/x.py", source_kind="repo", confidence="low")
        cov = coverage.analyze_static([d], [], config.DEFAULT)
        self.assertGreater(cov["skipped_rules"], 0, "源码猜出来的定义不可能全规则可查")
        self.assertEqual(cov["verdict"], "COVERAGE_LIMITED",
                         "低置信度零命中不能写成通过")

    def test_static_report_carries_coverage(self):
        import tempfile
        from evoguard_inspect import coverage, report
        from evoguard_inspect.static.model import AgentDef, ToolDef
        d = AgentDef(name="a", system_prompt="x" * 60,
                     tools=[ToolDef(name="kb.search", description="d")],
                     config={}, source="t.json", confidence="high")
        issues = SR.check(d)
        cov = coverage.analyze_static([d], issues, config.DEFAULT)
        with tempfile.TemporaryDirectory() as tmp:
            j, m = report.write_static(tmp, [d], issues, [], cov)
            with open(j, encoding="utf-8") as f:
                data = json.load(f)
            with open(m, encoding="utf-8") as f:
                text = f.read()
            self.assertEqual(data["verdict"], cov["verdict"])
            self.assertEqual(data["coverage"]["total_rules"], 20)
            self.assertIn("规则适用性", text)


if __name__ == "__main__":
    unittest.main()
