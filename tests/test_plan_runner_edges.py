# -*- coding: utf-8 -*-
"""PLAN-EDGE：PlanRunner 的边界与失败模式。

原有 3 个测试只覆盖「挂起->审批->续跑」主路径。这里补的是**出错时会不会
悄悄放过**：伪造审批键、跨租户审批、重复审批、无票据审批、重规划死循环。
这些才是检查点机制真正的风险面。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import autonomous_agent as AA                              # noqa: E402
import core                                                 # noqa: E402
import tool_registry as TR                                  # noqa: E402


class TestPlanEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="plan-edge-")
        self.store = core.Store(os.path.join(self.tmp, "p.db"))
        self.addCleanup(self.store.close)
        reg = TR.ToolRegistry()
        reg.from_json_schema("kb.search", {"type": "object",
            "properties": {"q": {"type": "string"}}}, source="json_schema")
        reg.from_json_schema("order.settle", {"type": "object",
            "properties": {"order_id": {"type": "string"}}}, source="json_schema")
        reg.declare_agent("react", ["kb.search", "order.settle"], replace=True)
        decl = reg.declared()
        decl["order.settle"]["side_effect"] = "write"
        decl["order.settle"]["_scope"] = "write"
        self.calls = []

        def executor(tool, args):
            self.calls.append(tool)
            return {"ok": True, "tool": tool}
        self.agent = AA.ReActAgent(goal="g", registry=reg, executor=executor,
                                   enforce=False)
        self.runner = AA.PlanRunner(self.agent, self.store, "s1",
                                    tenant_id="t1")

    def _write_step(self):
        return {"tool": "order.settle", "args": {"order_id": "A1"}}

    def _read_step(self, q="x"):
        return {"tool": "kb.search", "args": {"q": q}}

    # ---- 输入校验 ----
    def test_empty_or_nonlist_plan_rejected(self):
        for bad in ([], None, "kb.search", {"tool": "kb.search"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.runner.start(bad)

    def test_forged_reserved_keys_are_stripped(self):
        """计划里伪造 _approved 不能绕过审批。"""
        plan = [{"tool": "order.settle", "args": {"order_id": "A1"},
                 "_approved": True, "_approval_id": "fake",
                 "_status": "done"}]
        out = self.runner.start(plan)
        self.assertEqual(out.get("status"), "awaiting_approval",
                         "伪造 _approved 竟然直接放行了")
        state = self.runner._load()
        self.assertEqual(state["status"], "awaiting_approval")
        for step in state["plan"]:
            self.assertNotIn("_approved", step)
            self.assertNotIn("_approval_id", step)

    def test_non_dict_steps_dropped(self):
        out = self.runner.start([self._read_step("a"), "junk", 42,
                                 self._read_step("b")])
        state = self.runner._load()
        self.assertEqual(len(state["plan"]), 2)
        self.assertEqual(out.get("status"), "done")

    # ---- 审批安全 ----
    def test_cross_tenant_approve_rejected(self):
        self.runner.start([self._write_step()])
        with self.assertRaises(PermissionError):
            self.runner.approve(tenant_id="t2")

    def test_approve_without_ticket_raises(self):
        self.runner.start([self._write_step()])
        with self.store.conn() as c:
            c.execute("UPDATE agent_plans SET approval_id=NULL "
                      "WHERE session_id=? AND tenant_id=?", ("s1", "t1"))
        with self.assertRaises(ValueError):
            self.runner.approve()

    def test_double_approve_is_idempotent(self):
        self.runner.start([self._write_step()])
        first = self.runner.approve()
        self.assertTrue(first["approved"])
        second = self.runner.approve()
        self.assertFalse(second["approved"],
                         "重复审批应返回未生效，而不是再执行一遍")

    # ---- 检查点幂等 ----
    def test_done_plan_resume_does_not_reexecute(self):
        self.runner.start([self._read_step("a")])
        self.assertEqual(self.calls, ["kb.search"])
        out = self.runner.resume()
        self.assertEqual(out.get("status"), "done")
        self.assertEqual(self.calls, ["kb.search"],
                         "已完成计划被 resume 后又执行了一遍")

    def test_pending_plan_resume_without_approve_pauses(self):
        self.runner.start([self._write_step()])
        out = self.runner.resume()
        self.assertEqual(out.get("status"), "awaiting_approval")
        self.assertEqual(self.calls, [], "未审批的写步骤被执行了")

    # ---- 重规划上限 ----
    def test_replan_respects_max_replan(self):
        self.runner.start([self._write_step()])
        self.runner.approve()
        r1 = self.runner.replan([self._read_step("r1")], max_replan=1)
        self.assertTrue(r1["replanned"])
        r2 = self.runner.replan([self._read_step("r2")], max_replan=1)
        self.assertFalse(r2["replanned"])
        self.assertEqual(r2["reason"], "max_replan_reached")

    # ---- 反思分类 ----
    def test_reflect_classifies_failure_types(self):
        cases = {
            "tool_output_blocked: injection": "untrusted_output_injection",
            "rails deny: policy": "policy_denied",
            "pytest returncode 1 failed": "test_regression",
            "call timeout after 30s": "exec_failure",
            "E_LLM no_parseable_json": "model_output_invalid",
        }
        for reason, want in cases.items():
            with self.subTest(reason=reason):
                rec = self.runner.reflect(reason, step_index=0)
                self.assertEqual(rec["root_cause"], want, rec)


if __name__ == "__main__":
    unittest.main()
