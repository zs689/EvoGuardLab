# -*- coding: utf-8 -*-
"""AGENT-KERN-01：Reflect + Rollback + Replan 的回归测试。

不依赖特定模型：用桩 executor 模拟「首修遗漏边界 -> 测试失败 -> 回滚 -> 换策略通过」。
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import approval                                             # noqa: E402
import autonomous_agent as AA                               # noqa: E402
import core                                                 # noqa: E402
import sandbox_executor as SBX                              # noqa: E402
import tool_registry as TR                                  # noqa: E402


def _run_py(code):
    """本地跑一段 Python，返回 returncode（判定测试通过/失败用）。"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       timeout=20, check=False)
    return r.returncode


def _setup():
    work = tempfile.mkdtemp(prefix="kern-")
    reg = TR.ToolRegistry()
    ex = SBX.register_coding_tools(reg, root=work, docker_sandbox=None)
    reg.declare_agent("coder", ["fs.read", "fs.write", "code.run_in_sandbox"],
                      replace=True)
    store = core.Store(os.path.join(work, "a.db"))
    gate = approval.ApprovalGate(store)
    snap = SBX.WorkspaceSnapshot(work)
    return work, reg, ex, gate, store, snap


class TestReflect(unittest.TestCase):
    def test_reflect_classifies_failure(self):
        work, reg, ex, gate, store, snap = _setup()
        self.addCleanup(store.close)
        agent = AA.ReActAgent(goal="g", registry=reg, executor=ex,
                              agent_type="coder", enforce=False)
        r = AA.PlanRunner(agent, store, "s", tenant_id="t", snapshot=snap)
        rec = r.reflect(failure_reason="tool_output_blocked", step_index=1)
        self.assertEqual(rec["root_cause"], "untrusted_output_injection")
        rec2 = r.reflect(failure_reason="test_failure(returncode=1)", step_index=2)
        self.assertEqual(rec2["next_strategy"], "rollback_and_rework")
        rec3 = r.reflect(failure_reason="rails_denied", step_index=3)
        self.assertEqual(rec3["root_cause"], "policy_denied")


class TestRollback(unittest.TestCase):
    def test_snapshot_and_rollback(self):
        work, reg, ex, gate, store, snap = _setup()
        self.addCleanup(store.close)
        ex("fs.write", {"path": "m.py", "content": "v1\n"})
        snap.snapshot("clean")
        ex("fs.write", {"path": "m.py", "content": "v2-broken\n"})
        ex("fs.write", {"path": "extra.py", "content": "junk\n"})
        out = snap.rollback()
        self.assertEqual(ex("fs.read", {"path": "m.py"})["content"], "v1\n")
        self.assertFalse(os.path.exists(os.path.join(work, "extra.py")))
        self.assertEqual(out["removed"], 1)


class TestReplan(unittest.TestCase):
    def test_replan_respects_max(self):
        work, reg, ex, gate, store, snap = _setup()
        self.addCleanup(store.close)
        agent = AA.ReActAgent(goal="g", registry=reg, executor=ex,
                              agent_type="coder", enforce=False)
        r = AA.PlanRunner(agent, store, "s2", tenant_id="t", snapshot=snap)
        r.start([{"tool": "code.run_in_sandbox", "args": {"code": "assert 1"}}])
        a = r.replan([{"tool": "code.run_in_sandbox",
                       "args": {"code": "assert 1==1"}}], max_replan=2)
        self.assertTrue(a["replanned"])
        # 再连续重规划到上限
        r.replan([{"tool": "code.run_in_sandbox", "args": {"code": "x"}}], max_replan=2)
        c = r.replan([{"tool": "code.run_in_sandbox", "args": {"code": "y"}}],
                     max_replan=2)
        self.assertFalse(c["replanned"])
        self.assertEqual(c["reason"], "max_replan_reached")


class TestFirstFixMissesBoundary(unittest.TestCase):
    """端到端（不依赖特定模型）：首修遗漏边界 -> 测试失败 -> 回滚 -> 换策略通过。"""

    def test_reflect_rollback_replan_then_pass(self):
        work, reg, ex, gate, store, snap = _setup()
        self.addCleanup(store.close)
        # 一个带边界的真实小函数：count_pos 必须只数正数
        good = "def count_pos(xs):\n    return sum(1 for x in xs if x > 0)\n"
        buggy = "def count_pos(xs):\n    return len(xs)\n"   # 遗漏负值边界

        # 首修：写了 buggy（等于没修），测试会失败
        snap.snapshot("before")
        ex("fs.write", {"path": "mod.py", "content": buggy})
        self.assertNotEqual(_run_py(buggy + "\nassert count_pos([-1,2,3]) == 2\n"),
                            0, "首修应失败")

        # 反思 + 回滚脏改动
        agent = AA.ReActAgent(goal="g", registry=reg, executor=ex,
                              agent_type="coder", enforce=False)
        r = AA.PlanRunner(agent, store, "s3", tenant_id="t", snapshot=snap)
        refl = r.reflect("test_failure(returncode=1)", 0)
        self.assertEqual(refl["next_strategy"], "rollback_and_rework")
        snap.rollback()
        self.assertFalse(os.path.exists(os.path.join(work, "mod.py")),
                         "回滚未清掉脏文件")

        # 换策略：写正确版本，测试通过
        ex("fs.write", {"path": "mod.py", "content": good})
        self.assertEqual(_run_py(good + "\nassert count_pos([-1,2,3]) == 2\n"),
                         0, "换策略后应通过")


if __name__ == "__main__":
    unittest.main()
