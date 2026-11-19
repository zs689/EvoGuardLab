# -*- coding: utf-8 -*-
"""SCORE-03：语义 reflect / fs.patch / Schema 错误回传（修前必红反例）。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import autonomous_agent as AA                               # noqa: E402
import sandbox_executor as SB                                # noqa: E402
import gateway as GW                                         # noqa: E402


class TestSemanticReflect(unittest.TestCase):
    def test_model_semantic_reflect_revises_plan(self):
        """provider 可用时，语义归因应给出 root_cause 与 revised_steps。"""
        class P:
            def complete(self, messages, **kw):
                self.msgs = messages
                return ('{"root_cause":"wrong_arg","bad_assumption":'
                        '"假设 page 从 0 起","revised_steps":'
                        '["read file","fix page offset","run tests"]}',
                        1, "fake")
        p = P()
        r = AA.semantic_reflect(
            "AssertionError at page offset: expected 0 got 3", provider=p)
        self.assertTrue(r["model_backed"])
        self.assertEqual(r["root_cause"], "wrong_arg")
        self.assertEqual(len(r["revised_steps"]), 3)
        self.assertEqual(p.msgs[0]["role"], "planner",
                         "语义反思必须走 role=planner")
        # 失败上下文（stderr / 原计划）必须进 prompt
        joined = str(p.msgs)
        self.assertIn("AssertionError", joined)

    def test_rule_fallback_without_provider(self):
        r = AA.semantic_reflect("timeout on tool.call", provider=None)
        self.assertFalse(r["model_backed"])
        self.assertTrue(r["root_cause"])
        self.assertTrue(r["revised_steps"])

    def test_malformed_model_output_falls_back(self):
        class Bad:
            def complete(self, messages, **kw):
                return "not json at all", 1, "fake"
        r = AA.semantic_reflect("timeout", provider=Bad())
        self.assertFalse(r["model_backed"])
        self.assertTrue(r["root_cause"])

    def test_revised_steps_validated(self):
        """模型给的非字符串/空步骤必须被过滤，不能污染计划。"""
        class P:
            def complete(self, messages, **kw):
                return ('{"root_cause":"x","revised_steps":'
                        '["ok", 123, "", null, "another"]}', 1, "fake")
        r = AA.semantic_reflect("fail", provider=P())
        self.assertEqual(r["revised_steps"], ["ok", "another"])


class TestFsPatch(unittest.TestCase):
    def test_unique_match_replaces_only_one_spot(self):
        d = tempfile.mkdtemp(prefix="patch-")
        p = os.path.join(d, "a.py")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("x = 1\ny = 2\nx = 1\n")
        r = SB.fs_patch(p, "y = 2", "y = 3", root=d)
        self.assertTrue(r["ok"])
        with open(p, encoding="utf-8") as f:
            c = f.read()
        self.assertIn("y = 3", c)
        self.assertEqual(c.count("x = 1"), 2, "无关行被改了")

    def test_duplicate_match_rejected(self):
        d = tempfile.mkdtemp(prefix="patch-")
        p = os.path.join(d, "a.py")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("x = 1\nx = 1\n")
        with self.assertRaises(SB.SandboxError):
            SB.fs_patch(p, "x = 1", "x = 9", root=d)

    def test_no_match_rejected(self):
        d = tempfile.mkdtemp(prefix="patch-")
        p = os.path.join(d, "a.py")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("x = 1\n")
        with self.assertRaises(SB.SandboxError):
            SB.fs_patch(p, "not here", "x", root=d)


class _NullConn:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        return self
    def fetchone(self):
        return None
    def fetchall(self):
        return []


if __name__ == "__main__":
    unittest.main()
