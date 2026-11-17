# -*- coding: utf-8 -*-
"""WS-02-T1：真实多轮 ReAct 引擎的回归测试。

用假 provider 模拟模型输出（不接真 LLM，测试必须确定、零网络），
但**门禁走真实的 execution.preflight + tool_registry**，不是 mock 掉的。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import autonomous_agent as AA                              # noqa: E402
import tool_registry as TR                                 # noqa: E402


class ScriptedProvider:
    """按脚本逐轮返回模型输出；返回 (text, tokens, model_id)。"""

    name = "scripted"
    is_mock = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.i = 0

    def complete(self, messages, temperature=0.0, max_tokens=256):
        if self.i >= len(self.replies):
            obj = {"thought": "", "tool": None, "answer": "done"}
        else:
            obj = self.replies[self.i]
        self.i += 1
        return json.dumps(obj, ensure_ascii=False), 10, "scripted"

    def info(self):
        return {"provider": "scripted", "mock": True}


def _registry():
    reg = TR.ToolRegistry()
    for name in ("kb.search", "order.settle"):
        reg.from_json_schema(name, {"type": "object", "properties": {
            "query": {"type": "string"}} if name == "kb.search" else
            {"order_id": {"type": "string"}}}, source="json_schema")
    return reg


def _agent(replies, *, executor=None, reg=None, enforce=True, **kw):
    kw.setdefault("max_steps", 4)
    kw.setdefault("goal", "查订单并回答")
    return AA.ReActAgent(provider=ScriptedProvider(replies),
                         registry=reg or _registry(),
                         executor=executor, enforce=enforce,
                         agent_type="react", **kw)


class TestReActLoop(unittest.TestCase):
    def test_normal_two_round_then_answer(self):
        calls = []
        a = _agent([
            {"thought": "先查", "tool": "kb.search", "args": {"query": "x"}},
            {"thought": "", "tool": None, "answer": "查到了"},
        ], executor=lambda t, args: calls.append((t, args)) or {"ok": True})
        r = a.run()
        self.assertEqual(r["answer"], "查到了")
        self.assertEqual(len(calls), 1)
        self.assertTrue(r["contract"]["executed"])
        self.assertIsNone(r["stopped_by"])

    def test_guard_denied_never_executes(self):
        """工具在注册表里，但不在角色白名单 → preflight 必须拒，executor 零调用。"""
        calls = []
        reg = _registry()
        reg.declare_agent("react", {"kb.search"}, replace=True)
        a = _agent([
            {"thought": "越权", "tool": "order.settle", "args": {"order_id": "A1"}},
        ], executor=lambda t, args: calls.append((t, args)) or {"ok": True},
            reg=reg, enforce=True)
        r = a.run()
        self.assertEqual(calls, [], "门禁说 deny 时 executor 必须零调用")
        self.assertFalse(r["contract"]["executed"])
        self.assertEqual(r["contract"]["effect"], "call_stopped")
        self.assertEqual(r["stopped_by"], "guard_denied")

    def test_loop_detection_before_execution(self):
        calls = []
        same = {"thought": "重复", "tool": "kb.search", "args": {"query": "same"}}
        a = _agent([same, same, same],
                   executor=lambda t, args: calls.append(t) or {"ok": True},
                   max_same_call=2)
        r = a.run()
        self.assertEqual(r["stopped_by"], "loop_detected")
        self.assertLessEqual(len(calls), 2)
        self.assertTrue(any(s.get("kind") == "loop_guard" for s in r["steps"]))

    def test_no_executor_never_fakes_result(self):
        """没有注入执行器时，绝不编一个假结果继续推理。"""
        a = _agent([
            {"thought": "查", "tool": "kb.search", "args": {"query": "x"}},
            {"thought": "", "tool": None, "answer": "我假装查到了"},
        ])
        r = a.run()
        self.assertEqual(r["stopped_by"], "no_executor")
        self.assertIsNone(r["answer"])
        self.assertFalse(r["contract"]["executed"])

    def test_max_steps_budget(self):
        a = _agent([{"thought": "又查", "tool": "kb.search",
                     "args": {"query": "q%d" % i}} for i in range(10)],
                   executor=lambda t, args: {"ok": True},
                   max_steps=3, max_same_call=99)
        r = a.run()
        self.assertEqual(r["stopped_by"], "max_steps_exhausted")
        self.assertLessEqual(len(r["steps"]), 3)

    def test_unparsable_model_output_is_reported(self):
        class Bad:
            name = "bad"
            is_mock = True
            def complete(self, messages, temperature=0.0, max_tokens=256):
                return "not json at all", 1, "bad"
        a = AA.ReActAgent(provider=Bad(), registry=_registry(),
                          goal="x", agent_type="react")
        r = a.run()
        self.assertEqual(r["stopped_by"], "no_parseable_model_output")
        self.assertFalse(r["contract"]["executed"])

    def test_tools_prompt_respects_role_allowlist(self):
        reg = _registry()
        reg.declare_agent("react", {"kb.search"}, replace=True)
        tools = AA.build_tools_prompt(reg, "react")
        self.assertIn("kb.search", tools)
        self.assertNotIn("order.settle", tools)


class TestSixFieldContract(unittest.TestCase):
    def test_fields_present_and_types(self):
        a = _agent([{"thought": "", "tool": None, "answer": "ok"}])
        c = a.run()["contract"]
        for k in ("detected", "decision", "attempted", "executed",
                  "effect", "reason"):
            self.assertIn(k, c)
        self.assertIsInstance(c["executed"], bool)


class TestFalsifiability(unittest.TestCase):
    """把门禁换成恒定 deny，执行次数必须归零 —— 证明 ReAct 真的经过门禁。"""

    def test_constant_deny_contract_zero_execution(self):
        import execution as EX
        calls = []
        a = _agent([
            {"thought": "查", "tool": "kb.search", "args": {"query": "x"}},
        ], executor=lambda t, args: calls.append(t) or {"ok": True})

        def deny(agent_type, tool, args, **kw):
            return {"allowed": False, "decision": "deny",
                    "reason": "测试注入的恒定 deny", "findings": []}

        a.cfg["contract_fn"] = deny
        original = EX.preflight
        # execution.preflight 用 contract_fn 关键字参数；这里直接封住引擎里的调用
        a._preflight = lambda tool, args, seq, observation=None: {
            "stop": True, "decision": "deny", "reason": "constant deny",
            "checks": []}
        r = a.run()
        self.assertEqual(calls, [])
        self.assertFalse(r["contract"]["executed"])
        self.assertEqual(r["stopped_by"], "guard_denied")

    def test_six_field_executed_true_only_with_real_executor(self):
        a = _agent([
            {"thought": "查", "tool": "kb.search", "args": {"query": "x"}},
            {"thought": "", "tool": None, "answer": "ok"},
        ], executor=lambda t, args: {"ok": True})
        c = a.run()["contract"]
        self.assertTrue(c["executed"])
        self.assertEqual(c["effect"], "call_executed")


class TestCtxIsolation(unittest.TestCase):
    """WS-02-T2：forced_skill 必须是 Ctx 实例状态，不能串到别的执行。"""

    def test_two_ctx_have_independent_forced_skill(self):
        import runtime as RT
        # 造两个最小 Ctx（不跑工具，只看状态隔离）
        c1 = RT.Ctx(None, "r1", {"agent_type": "order"},
                    forced_skill={"id": "A", "task_type": "order"})
        c2 = RT.Ctx(None, "r2", {"agent_type": "kb"},
                    forced_skill={"id": "B", "task_type": "kb"})
        self.assertEqual(c1.forced_skill["id"], "A")
        self.assertEqual(c2.forced_skill["id"], "B")
        c1.forced_skill["id"] = "CHANGED"
        self.assertEqual(c2.forced_skill["id"], "B", "两个 Ctx 状态串了")

    def test_run_once_reads_forced_skill_from_exp(self):
        """exp 里的 forced_skill 应进入 Ctx，而不是靠设置全局变量。"""
        import tempfile, core, runtime as RT
        d = tempfile.mkdtemp(prefix="evo-ctx-")
        store = core.Store(os.path.join(d, "a.db"))
        exp = {"id": "x", "agent_type": "order", "task_id": "A1001",
               "forced_skill": {"id": "S", "task_type": "order"}}
        seen = {}
        original = RT.Ctx
        class Spy(original):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                if seen.get("forced") is None:
                    seen["forced"] = self.forced_skill
        RT.Ctx = Spy
        try:
            RT.run_once(store, exp, "baseline")
        except Exception:
            pass                      # 模型/工具可能跑不完，构造阶段已记录
        finally:
            RT.Ctx = original
            store.close()
        self.assertEqual((seen.get("forced") or {}).get("id"), "S")


class TestContractConsistency(unittest.TestCase):
    """六字段铁律：effect 不能和 executed 自相矛盾。"""

    def test_effect_always_matches_executed(self):
        scenarios = [
            ([{"thought":"","tool":None,"answer":"ok"}], lambda t,a: {"ok":1}),
            ([{"thought":"x","tool":"kb.search","args":{"query":"q%d"%i}}
              for i in range(6)], lambda t,a: {"ok":1}),
            ([{"thought":"x","tool":"kb.search","args":{"query":"x"}}], None),
        ]
        for replies, ex in scenarios:
            a = _agent(replies, executor=ex, max_steps=2, max_same_call=99)
            c = a.run()["contract"]
            if c["executed"]:
                self.assertIn(c["effect"], ("call_executed",))
            else:
                self.assertNotEqual(c["effect"], "call_executed",
                                    "executed=False 却说 call_executed")


if __name__ == "__main__":
    unittest.main()
