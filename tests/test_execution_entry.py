# -*- coding: utf-8 -*-
"""评审 R01 的守门测试：门禁必须落在**真实执行点**上。

背景：`tool_contract.authorize` 与 `security.guard_tool_intent` 都写好、也都有
单测，但 `runtime.Ctx.call` 一次都没调它们。评审把两者换成都返回 deny 的替身，
工具照样执行了一次 —— 也就是说「模块通过」和「真实拦住了」根本是两件事。

这个文件只钉一件事：**判定说停，`sim.call_tool` 的调用次数必须是 0。**
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import approval      # noqa: E402
import core          # noqa: E402
import execution     # noqa: E402
import runtime       # noqa: E402
import security      # noqa: E402
import tool_contract  # noqa: E402


def _ctx(agent_type="order", gate=None, goal=None):
    d = tempfile.mkdtemp(prefix="exec_entry_")
    store = core.Store(os.path.join(d, "t.db"))
    exp = store.create_experiment(name="t", agent_type=agent_type, task_id="A1001",
                                  fault_type="none", fault_target=0, seed=7,
                                  recovery_mode="D")
    exp["user_goal"] = goal if goal is not None else "查询订单 A1001 的金额"
    run = store.create_run(exp["id"], "D", 7)
    ctx = runtime.Ctx(store, run["id"], exp, approval_gate=gate)
    return store, ctx


class TestDenyStopsExecution(unittest.TestCase):
    def test_contract_deny_means_zero_tool_calls(self):
        store, ctx = _ctx()
        with mock.patch.object(tool_contract, "authorize",
                               return_value={"decision": "deny",
                                             "reasons": ["域外地址"]}) as c, \
             mock.patch.object(runtime.sim, "call_tool",
                               return_value={"ran": True}) as call:
            with self.assertRaises(runtime.ToolError) as cm:
                ctx.call("order.get_order", {"order_id": "A1001"})
        self.assertEqual(c.call_count, 1, "契约根本没被调用")
        self.assertEqual(call.call_count, 0, "deny 了还执行了工具")
        self.assertEqual(cm.exception.error_type, "security_deny")
        store.close()

    def test_intent_deny_means_zero_tool_calls(self):
        store, ctx = _ctx()
        with mock.patch.dict(os.environ, {"AGENTLAB_SECURITY_INTENT": "1"}), \
             mock.patch.object(tool_contract, "authorize",
                               return_value={"decision": "allow"}), \
             mock.patch.object(security, "guard_tool_intent",
                               return_value={"decision": "deny",
                                             "reason": "与目标无关"}) as it, \
             mock.patch.object(runtime.sim, "call_tool",
                               return_value={"ran": True}) as call:
            with self.assertRaises(runtime.ToolError):
                ctx.call("order.get_order", {"order_id": "A1001"})
        self.assertEqual(it.call_count, 1, "意图门禁根本没被调用")
        self.assertEqual(call.call_count, 0)
        store.close()

    def test_normal_call_executes_exactly_once(self):
        store, ctx = _ctx()
        with mock.patch.object(tool_contract, "authorize",
                               return_value={"decision": "allow"}), \
             mock.patch.object(runtime.sim, "call_tool",
                               return_value={"order_id": "A1001",
                                             "total": 504.0}) as call:
            ctx.call("order.get_order", {"order_id": "A1001"})
        self.assertEqual(call.call_count, 1, "正常调用必须真的执行一次")
        store.close()

    def test_call_record_and_effect_are_written(self):
        store, ctx = _ctx()
        with mock.patch.object(tool_contract, "authorize",
                               return_value={"decision": "deny",
                                             "reasons": ["x"]}), \
             mock.patch.object(runtime.sim, "call_tool", return_value={}):
            with self.assertRaises(runtime.ToolError):
                ctx.call("order.get_order", {"order_id": "A1001"})
        self.assertEqual(len(ctx.calls), 1)
        rec = ctx.calls[0]
        self.assertEqual(rec["decision"], "deny")
        self.assertFalse(rec["executed"], "检测层不许声称执行过")
        self.assertTrue(rec["call_id"].startswith("call_"))
        # 轨迹里必须留下这次拒绝，而不是只写日志
        steps = [s for s in store.list_steps(ctx.run_id)
                 if s.get("error_type") == "security_deny"]
        self.assertEqual(len(steps), 1)
        store.close()

    def test_call_id_is_stable_across_processes(self):
        a = execution.call_id("run1", 3, "order.get_order", {"order_id": "A1001"})
        b = execution.call_id("run1", 3, "order.get_order", {"order_id": "A1001"})
        c = execution.call_id("run1", 3, "order.get_order", {"order_id": "A1002"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c, "参数不同却同 call_id，审计会串")


class TestControlledConfig(unittest.TestCase):
    def test_write_tool_without_approval_backend_refuses_to_start(self):
        with self.assertRaises(execution.ExecutionConfigError) as cm:
            execution.build_config({"AGENTLAB_EXEC_MODE": "controlled"},
                                   agent_type="order",
                                   allowed_tools=["order.settle"],
                                   write_tools={"order.settle"},
                                   has_approval_backend=False)
        self.assertIn("审批后端", str(cm.exception))

    def test_same_config_with_backend_starts(self):
        cfg = execution.build_config({"AGENTLAB_EXEC_MODE": "controlled"},
                                     agent_type="order",
                                     allowed_tools=["order.settle"],
                                     write_tools={"order.settle"},
                                     has_approval_backend=True)
        self.assertEqual(cfg["mode"], execution.CONTROLLED)
        self.assertEqual(cfg["gaps"], [])

    def test_unknown_mode_is_an_error_not_a_silent_downgrade(self):
        # 打错一个字就悄悄退回不防护，是最危险的一种「默认」
        with self.assertRaises(execution.ExecutionConfigError):
            execution.build_config({"AGENTLAB_EXEC_MODE": "enforce!"},
                                   agent_type="order",
                                   allowed_tools=["order.get_order"])

    def test_legacy_mode_keeps_historical_behaviour(self):
        cfg = execution.build_config({}, agent_type="order",
                                     allowed_tools=["order.settle"],
                                     write_tools={"order.settle"},
                                     has_approval_backend=False)
        self.assertEqual(cfg["mode"], execution.LEGACY)
        self.assertTrue(cfg["gaps"], "缺口要如实记录，不能因为不阻断就假装没有")

    def test_ctx_exports_effective_config(self):
        store, ctx = _ctx()
        exp = ctx.exec_config_export
        self.assertEqual(exp["mode"], execution.LEGACY)
        self.assertIn("contract", exp)
        self.assertIn("approval", exp)
        store.close()


class TestControlledStopsOnMissingPremises(unittest.TestCase):
    def test_missing_user_goal_goes_to_review_not_allow(self):
        store, ctx = _ctx(goal="")
        with mock.patch.dict(os.environ, {"AGENTLAB_EXEC_MODE": "controlled",
                                          "AGENTLAB_SECURITY_INTENT": "1"}), \
             mock.patch.object(tool_contract, "authorize",
                               return_value={"decision": "allow"}), \
             mock.patch.object(runtime.sim, "call_tool",
                               return_value={"ran": True}) as call:
            ctx.exec_config = execution.build_config(
                os.environ, agent_type="order",
                allowed_tools=["order.get_order"], write_tools=set(),
                has_approval_backend=False)
            with self.assertRaises(runtime.ToolError) as cm:
                ctx.call("order.get_order", {"order_id": "A1001"})
        self.assertEqual(call.call_count, 0, "缺目标就不能猜，更不能放行")
        self.assertEqual(cm.exception.error_type, "security_review")
        store.close()

    def test_judge_unavailable_goes_to_review_not_allow(self):
        store, ctx = _ctx()
        with mock.patch.dict(os.environ, {"AGENTLAB_EXEC_MODE": "controlled",
                                          "AGENTLAB_SECURITY_INTENT": "1"}), \
             mock.patch.object(tool_contract, "authorize",
                               return_value={"decision": "allow"}), \
             mock.patch.object(security, "guard_tool_intent",
                               side_effect=RuntimeError("模型连不上")), \
             mock.patch.object(runtime.sim, "call_tool",
                               return_value={"ran": True}) as call:
            ctx.exec_config = execution.build_config(
                os.environ, agent_type="order",
                allowed_tools=["order.get_order"], write_tools=set(),
                has_approval_backend=False)
            with self.assertRaises(runtime.ToolError):
                ctx.call("order.get_order", {"order_id": "A1001"})
        self.assertEqual(call.call_count, 0, "判定器挂了不等于放行")
        store.close()


class TestIntentInputCoverage(unittest.TestCase):
    """评审 R06：原来 body[:400] 截断，尾部字段对判定器完全不可见。"""

    def test_tail_field_reaches_the_judge(self):
        seen = []
        with mock.patch.dict(os.environ, {"AGENTLAB_SECURITY_MODE": "detect",
                                          "AGENTLAB_SECURITY_INTENT": "1"}), \
             mock.patch("semantic_guard.judge_alignment",
                        side_effect=lambda g, a: (seen.append(a), {
                            "decision": "allow", "status": "observed",
                            "reason": "ok", "observation": "", "thought": ""})[1]):
            security.guard_tool_intent(
                "order", "order.get_order",
                {"padding": "x" * 600, "payee": "REVIEW_TAIL_MARKER"},
                "读取订单 A1001")
        self.assertTrue(seen, "判定器没被调用")
        self.assertIn("REVIEW_TAIL_MARKER", seen[0], "尾部关键字段被截掉了")

    def test_omitted_fields_force_review_not_allow(self):
        with mock.patch.dict(os.environ, {"AGENTLAB_SECURITY_MODE": "detect",
                                          "AGENTLAB_SECURITY_INTENT": "1"}), \
             mock.patch("semantic_guard.judge_alignment",
                        return_value={"decision": "allow", "status": "observed",
                                      "reason": "ok", "observation": "",
                                      "thought": ""}):
            r = security.guard_tool_intent(
                "order", "order.get_order",
                {"padding": "y" * 5000, "note": "z" * 5000}, "读取订单")
        # 看不全就不许说 allow —— 前缀结论不能当全量结论
        self.assertEqual(r["decision"], "review")
        self.assertTrue(r["omitted_fields"], "没送判的字段必须显式列出来")

    def test_priority_order_puts_action_fields_first(self):
        view, covered, _ = security.intent_payload(
            "order.settle", {"note": "n" * 10, "amount": 900,
                             "account": "6222", "action": "transfer"})
        self.assertEqual(covered[0], "action")
        self.assertIn("account", covered)
        self.assertLess(covered.index("account"), covered.index("note"))


if __name__ == "__main__":
    unittest.main()
