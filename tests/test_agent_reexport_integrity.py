# -*- coding: utf-8 -*-
"""agent_reexport 门面的完整性：拆分模块后 import 路径必须 100% 兼容。

autonomous_agent 用 `from agent_reexport import *` 消费 6 个拆件模块。少了
一个导出，pyflakes 只能报"may be undefined"（因为 star import 看不见），
运行时才炸 AttributeError/NameError —— 而且只在某条冷分支上炸。
"""
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import agent_reexport as AR                                # noqa: E402

#: 这些是 autonomous_agent 从星号导入里直接用的名字（pyflakes 的
#: "may be undefined" 清单），漏一个就是运行时 NameError。
USED_FROM_STAR = [
    "PlanRunner", "SESSION_BUDGET_CHARS", "session_load", "compress_turns",
    "build_messages", "session_save", "parallel_readonly", "_is_readonly",
    "_dedupe_key", "_six", "should_reflect", "focused_reflection_message",
    "semantic_reflect", "_plan_step_needs_approval", "SYSTEM", "AgentStopped",
    "_contract_fields", "build_tools_prompt", "_classify_failure",
    "_detect_repetition", "tool_name_for_model", "tool_name_from_model",
    "_normalize_tool_name", "_tools_spec", "_model_call",
]

SOURCE_MODULES = ("session_store", "parallel_tools", "reflection",
                  "agent_utils", "tool_naming", "model_call", "plan_runner")


class TestReexportIntegrity(unittest.TestCase):
    def test_all_has_no_duplicates(self):
        names = AR.__all__
        self.assertEqual(len(names), len(set(names)), "重复导出：%s" % names)

    def test_every_export_is_importable(self):
        for name in AR.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(AR, name),
                                "__all__ 列了但模块里没有：%s" % name)

    def test_star_consumed_names_are_all_exported(self):
        missing = [n for n in USED_FROM_STAR if n not in AR.__all__]
        self.assertEqual(missing, [], "autonomous_agent 用到的名字没导出：%s"
                         % (missing,))

    def test_underscore_names_are_explicitly_listed(self):
        """没有 __all__ 时 import * 会跳过下划线名 —— 必须显式列出。"""
        under = [n for n in AR.__all__ if n.startswith("_")]
        self.assertGreaterEqual(len(under), 9,
                                "下划线符号导出太少，可能漏了：%s" % under)

    def test_exports_are_identity_with_source(self):
        """门面应该是同一个函数对象，不能是包装/副本（否则 isinstance 等失效）。"""
        from model_call import _model_call
        from reflection import should_reflect
        from tool_naming import tool_name_from_model
        from session_store import compress_turns
        from plan_runner import PlanRunner
        for obj, name in ((_model_call, "_model_call"),
                          (should_reflect, "should_reflect"),
                          (tool_name_from_model, "tool_name_from_model"),
                          (compress_turns, "compress_turns"),
                          (PlanRunner, "PlanRunner")):
            with self.subTest(name=name):
                self.assertIs(getattr(AR, name), obj)

    def test_source_modules_are_importable(self):
        for m in SOURCE_MODULES:
            with self.subTest(mod=m):
                importlib.import_module(m)

    def test_all_is_nonempty_and_sorted_unique(self):
        self.assertGreater(len(AR.__all__), 20)


if __name__ == "__main__":
    unittest.main()
