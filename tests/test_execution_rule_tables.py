# -*- coding: utf-8 -*-
"""execution / tool_contract / tool_rails 三张规则表的一致性。

扣分点写得很直白："三处规则表分散，改一处要同步三处"。这类缺陷的危险在于
**不会报错**：scanner 名拼错 -> 检查静默跳过；flow 写错 -> 规则从不执行；
契约工具没 schema -> 参数无约束。功能测试全绿也发现不了。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import execution as EX                                     # noqa: E402
import tool_contract as TC                                 # noqa: E402
import tool_rails as TR                                    # noqa: E402


def _scanner_names(flows):
    out = set()
    for flow in flows.values():
        for rule in (flow or []):
            if isinstance(rule, str):
                out.add(rule)
            elif isinstance(rule, dict):
                out.add(rule.get("scanner"))
    return out


class TestRailsTable(unittest.TestCase):
    def test_all_scanner_names_are_builtin(self):
        builtin = set(TR._builtin_scanners())
        missing = {}
        for tool, flows in TR.DEFAULT_RAILS.items():
            bad = _scanner_names(flows) - builtin
            if bad:
                missing[tool] = sorted(x for x in bad if x)
        self.assertFalse(missing, "拼错的 scanner 会静默跳过检查：%s" % (missing,))

    def test_flow_keys_valid(self):
        bad = {t: set(f) - set(TR.FLOWS)
               for t, f in TR.DEFAULT_RAILS.items()
               if set(f) - set(TR.FLOWS)}
        self.assertFalse(bad, "未知 flow 的规则永不执行：%s" % (bad,))

    def test_spec_dict_keys_allowed(self):
        bad = []
        for tool, flows in TR.DEFAULT_RAILS.items():
            for flow, rules in flows.items():
                for r in rules or []:
                    if isinstance(r, dict):
                        extra = set(r) - set(TR._ALLOWED_SPEC_KEYS)
                        if extra:
                            bad.append((tool, flow, sorted(extra)))
        self.assertEqual(bad, [], "非法规则字段：%s" % (bad,))

    def test_rails_cover_all_declared_schemas(self):
        """有 schema 的工具都要有护栏配置（哪怕是空规则），否则新增工具裸奔。"""
        missing = set(TC.TOOL_SCHEMAS) - set(TR.DEFAULT_RAILS)
        self.assertFalse(missing, "工具无护栏配置：%s" % (sorted(missing),))

    def test_rails_statuses_are_distinct(self):
        self.assertEqual(len({TR.SAFE, TR.UNSAFE, TR.ERROR,
                              TR.NOT_DECLARED}), 4,
                         "护栏状态必须互不相同（尤其 ERROR 不能算 SAFE）")


class TestContractTable(unittest.TestCase):
    def test_every_contract_has_schema(self):
        missing = set(TC.DEFAULT_CONTRACTS) - set(TC.TOOL_SCHEMAS)
        self.assertFalse(missing, "契约工具缺参数 schema：%s"
                         % (sorted(missing),))

    def test_contract_fields_present(self):
        need = {"identities", "allowed_domains", "allowed_resource_pattern",
                "writable_paths", "max_amount", "methods", "side_effect",
                "requires_approval"}
        for tool, c in TC.DEFAULT_CONTRACTS.items():
            with self.subTest(tool=tool):
                self.assertTrue(need <= set(c),
                                "%s 契约缺字段：%s" % (tool, need - set(c)))

    def test_approval_tools_are_real_tools(self):
        bad = [t for t, c in TC.DEFAULT_CONTRACTS.items()
               if c.get("requires_approval") and t not in TC.TOOL_SCHEMAS]
        self.assertEqual(bad, [], "需审批的工具不存在：%s" % (bad,))

    def test_write_tools_have_constraints(self):
        """有副作用的工具要么要求审批，要么限制可写路径/金额。"""
        loose = []
        for tool, c in TC.DEFAULT_CONTRACTS.items():
            if c.get("side_effect") == "none":
                continue
            constrained = (c.get("requires_approval") or c.get("writable_paths")
                           or c.get("max_amount") is not None)
            if not constrained:
                loose.append(tool)
        self.assertEqual(loose, [], "写工具无任何约束：%s" % (loose,))

    def test_schema_version_pins(self):
        self.assertTrue(TC.CONTRACT_VERSION.startswith("tool-contract-"))
        self.assertTrue(TC.SCHEMA_VERSION.startswith("tool-schema-"))


class TestExecutionDefaults(unittest.TestCase):
    def test_fail_closed_defaults(self):
        self.assertEqual(EX.DEFAULT_ON_MISSING_GOAL, EX.REVIEW)
        self.assertEqual(EX.DEFAULT_ON_JUDGE_UNAVAILABLE, EX.REVIEW)

    def test_stop_set_includes_deny_and_review(self):
        self.assertIn(EX.DENY, EX.STOP)
        self.assertIn(EX.REVIEW, EX.STOP)
        self.assertNotIn(EX.ALLOW, EX.STOP, "STOP 里混入 ALLOW 会放行危险动作")

    def test_config_version_pinned(self):
        self.assertEqual(EX.CONFIG_VERSION, "exec-config-v1")


if __name__ == "__main__":
    unittest.main()
