# -*- coding: utf-8 -*-
"""AGENT-CAP-01：多轮会话 + 并行只读工具（零依赖）。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

import core                                                 # noqa: E402
import autonomous_agent as AA                               # noqa: E402
import tool_registry as TR                                  # noqa: E402


class FakeProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        r = self.replies.pop(0) if self.replies else {"answer": "done"}
        return r.get("_text") or json.dumps(
            r, ensure_ascii=False), 1, "fake"


def _mini_registry():
    reg = TR.ToolRegistry()
    reg.from_json_schema("kb.read", {"type": "object",
                         "properties": {"q": {"type": "string"}}},
                         scope=TR.READ_ONLY, source="test",
                         identities=["react"])
    return reg


class TestSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "s.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_three_turns_keep_context(self):
        p = FakeProvider([
            {"answer": "第一轮：订单A1001"},
            {"answer": "第二轮：记得A1001"},
            {"answer": "第三轮：仍然记得A1001"},
        ])
        outs = []
        for i in range(3):
            out = AA.ReActAgent(
                provider=p, registry=_mini_registry(),
                store=self.store, session_id="sess-1",
                tenant_id="t1", goal="第%d轮：还记得订单号吗" % (i + 1),
                max_steps=2).run()
            outs.append(out)
        self.assertTrue(all(o["session_saved"] for o in outs))
        turns, summary, _boundary = AA.session_load(self.store, "sess-1")
        self.assertGreaterEqual(len(turns), 2)
        self.assertIn("A1001", json.dumps(
            turns, ensure_ascii=False))
        # 第三轮的模型确实拿到了历史（messages 里有"历史目标"）
        self.assertTrue(any("历史目标" in json.dumps(m, ensure_ascii=False)
                            for m in p.calls[-1]))

    def test_compression_preserves_untrusted_boundary(self):
        turns = []
        for i in range(20):
            turns.append({"seq": i, "goal": "g%d" % i, "tool": "kb.read",
                          "observation": {"note": "工具返回不可信"}})
        kept, summary, boundary = AA.compress_turns(turns, budget=100)
        self.assertLess(len(kept), len(turns))
        self.assertIn("安全边界", summary)
        self.assertIn("不可信", summary)
        self.assertTrue(boundary)

    def test_no_session_store_is_noop(self):
        out = AA.ReActAgent(provider=FakeProvider([{"answer": "x"}]),
                            registry=_mini_registry(),
                            goal="g", max_steps=1).run()
        self.assertNotIn("session_id", out)


class TestParallelReadOnly(unittest.TestCase):
    def setUp(self):
        self.reg = TR.ToolRegistry()
        for name, scope in (("kb.read", TR.READ_ONLY),
                            ("file.read", TR.READ_ONLY),
                            ("http.fetch", TR.READ_ONLY),
                            ("db.write", TR.WRITE)):
            self.reg.from_json_schema(
                name, {"type": "object", "properties": {"q": {"type": "string"}}},
                scope=scope, source="test", identities=["react"])

    def test_three_readonly_parallel_and_one_write_denied(self):
        calls = [{"tool": "kb.read", "args": {"q": "a"}},
                 {"tool": "file.read", "args": {"q": "b"}},
                 {"tool": "http.fetch", "args": {"q": "c"}},
                 {"tool": "db.write", "args": {"q": "d"}}]
        lock = threading.Lock()
        active = {"n": 0, "peak": 0}
        def executor(tool, args):
            with lock:
                active["n"] += 1
                active["peak"] = max(active["peak"], active["n"])
            time.sleep(0.01)               # 让并发窗口可见
            with lock:
                active["n"] -= 1
            return {"ok": tool}
        recs = AA.parallel_readonly(
            calls, executor=executor, registry=self.reg, max_workers=4)
        self.assertEqual(len(recs), 4)
        self.assertTrue(all(r["executed"] for r in recs[:3]))
        self.assertFalse(recs[3]["executed"])
        self.assertEqual(recs[3]["reason"], "write_tool_not_parallel")
        self.assertGreaterEqual(active["peak"], 2,
                                "只读工具没有真正并行执行")




class TestParallelBranchEndToEnd(unittest.TestCase):
    """模型返回 tools 数组时，run() 必须走并行分支。

    写工具的串行审批拒绝由 TestParallelReadOnly 直接覆盖；这里只验证
    「单轮多工具」的编排接线，不重复测 preflight 的执行配置细节。
    """

    def test_model_tools_array_uses_parallel_branch(self):
        replies = [{"tools": [
            {"tool": "kb.read", "args": {"q": "a"}},
            {"tool": "file.read", "args": {"q": "b"}},
            {"tool": "http.fetch", "args": {"q": "c"}}]}]
        reg = TR.ToolRegistry()
        for name in ("kb.read", "file.read", "http.fetch"):
            # 不传 scope：与 test_autonomous_agent._registry() 一致，
            # 让执行配置的 legacy 路径能处理动态注册工具。
            reg.from_json_schema(name, {"type": "object",
                              "properties": {"q": {"type": "string"}}},
                              source="test")
        seen = []
        out = AA.ReActAgent(
            provider=FakeProvider(replies), registry=reg,
            executor=lambda t, a: seen.append(t) or {"ok": t},
            goal="并行查三处", max_steps=2).run()
        batch = [x for x in out["steps"] if x.get("kind") == "parallel_batch"]
        self.assertTrue(batch, "模型返回 tools 数组却没走并行分支")
        self.assertEqual(len(batch[0]["parallel"]), 3)


if __name__ == "__main__":
    unittest.main()
