# -*- coding: utf-8 -*-
"""AGENT-PRO-01-T1：显式计划 + 检查点断点续跑。

验收：多步任务第 2 步触发写操作审批挂起，审批批准后 resume 从第 2 步
断点继续完成，第 1 步不重复执行（检查点生效）。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import autonomous_agent as AA                              # noqa: E402
import core                                                 # noqa: E402
import tool_registry as TR                                 # noqa: E402


class TestPlanCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="plan-")
        self.store = core.Store(os.path.join(self.tmp, "plan.db"))
        self.addCleanup(self.store.close)
        reg = TR.ToolRegistry()
        reg.from_json_schema("kb.search", {"type": "object",
            "properties": {"q": {"type": "string"}}}, source="json_schema")
        reg.from_json_schema("order.settle", {"type": "object",
            "properties": {"order_id": {"type": "string"}}}, source="json_schema")
        # 显式声明 order.settle 为 write，供审批判定
        reg.declare_agent("react", ["kb.search", "order.settle"], replace=True)
        decl = reg.declared()
        decl["order.settle"]["side_effect"] = "write"
        decl["order.settle"]["_scope"] = "write"

        self.calls = []
        def executor(tool, args):
            self.calls.append(tool)
            return {"ok": True, "tool": tool}
        self.agent = AA.ReActAgent(goal="完成3步", registry=reg,
                                   executor=executor, enforce=False)
        self.runner = AA.PlanRunner(self.agent, self.store, "sess-plan",
                                    tenant_id="t1")

    def test_pause_at_write_step_and_resume_from_checkpoint(self):
        plan = [
            {"tool": "kb.search", "args": {"q": "step1"}},
            {"tool": "order.settle", "args": {"order_id": "A1"}},  # 写，需审批
            {"tool": "kb.search", "args": {"q": "step3"}},
        ]
        out = self.runner.start(plan)
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertEqual(out["paused_at"], 1)
        self.assertEqual(self.calls, ["kb.search"],
                         "挂起前应只执行了第1步")

        # 未审批直接 resume：仍在同一步挂起，不越过审批
        again = self.runner.resume()
        self.assertEqual(again["status"], "awaiting_approval")
        self.assertEqual(self.calls, ["kb.search"])

        # 审批后断点续跑
        self.assertEqual(self.runner.approve()["approved"], True)
        done = self.runner.resume()
        self.assertEqual(done["status"], "done")
        self.assertEqual(len(done["results"]), 3)
        # 第1步不重复执行：kb.search 总共只跑了 step1 + step3 两次
        self.assertEqual(self.calls,
                         ["kb.search", "order.settle", "kb.search"],
                         "第1步被重复执行或断点错位：%s" % self.calls)
        self.assertEqual(done["results"][1]["tool"], "order.settle")
        self.assertTrue(done["results"][1]["executed"])

    def test_agent_run_plan_entry(self):
        """plan_first 便捷入口：ReActAgent.run_plan 走同一检查点路径。"""
        agent = AA.ReActAgent(goal="g", registry=self.agent.registry,
                              executor=self.agent.executor, enforce=False,
                              plan_first=True, store=self.store,
                              session_id="entry-plan", tenant_id="t1")
        self.assertTrue(agent.plan_first)
        out = agent.run_plan([{"tool": "kb.search", "args": {"q": "x"}}])
        self.assertEqual(out["status"], "done")
        self.assertEqual(len(out["results"]), 1)

    def test_new_store_instance_can_resume_same_session(self):
        """检查点持久化：换一个 PlanRunner 实例仍能从断点续跑。"""
        plan = [{"tool": "kb.search", "args": {"q": "1"}},
                {"tool": "order.settle", "args": {"order_id": "A1"}}]
        self.runner.start(plan)  # 挂起在第2步
        runner2 = AA.PlanRunner(self.agent, self.store, "sess-plan",
                                tenant_id="t1")
        self.assertEqual(runner2.approve()["approved"], True)
        done = runner2.resume()
        self.assertEqual(done["status"], "done")
        self.assertEqual(len(done["results"]), 2)


if __name__ == "__main__":
    unittest.main()
