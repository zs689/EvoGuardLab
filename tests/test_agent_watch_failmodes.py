# -*- coding: utf-8 -*-
"""agent_watch 的失败模式：被监测的 agent 本身就是不可信的。

探针会去调用一个**外部** agent——它可能抛异常、返回 None、返回结构乱掉的
tool_calls。这些情况如果让探针自己崩掉，监控就成了"被监控对象一犯病就失效"，
恰恰在最需要它的时候缺席。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import agent_watch as AW                                   # noqa: E402


def _target(fn, tools=None):
    return AW.AgentTarget.from_callable("probe-subject", fn, tools=tools)


class TestRuntimeProbeRobustness(unittest.TestCase):
    def test_agent_raising_does_not_crash_probe(self):
        def boom(_req):
            raise RuntimeError("agent exploded")
        out = AW.runtime_probe(_target(boom))
        errs = [r for r in out["probes"] if r.get("error")]
        self.assertTrue(errs, "异常必须被记录成 error，而不是吞掉")
        self.assertIn("agent exploded", errs[0]["error"])

    def test_agent_returning_none_does_not_crash(self):
        out = AW.runtime_probe(_target(lambda _r: None))
        self.assertIn("probes", out)
        self.assertFalse(out["passed"], "什么都不返回的 agent 不该被判通过")

    def test_agent_returning_string_does_not_crash(self):
        out = AW.runtime_probe(_target(lambda _r: "just a string"))
        self.assertIn("probes", out)

    def test_malformed_tool_calls_are_tolerated(self):
        cases = [
            {"tool_calls": "not-a-list"},
            {"tool_calls": [None, 42, "x"]},
            {"tool_calls": [{"no_tool_key": 1}]},
            {"tool_calls": [{"tool": None}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                out = AW.runtime_probe(_target(lambda _r, p=payload: p))
                self.assertIn("probes", out)

    def test_custom_probe_subset(self):
        calls = []

        def agent(req):
            calls.append(req.get("prompt"))
            return {"tool_calls": [], "answer": "ok", "status": "ok"}
        out = AW.runtime_probe(_target(agent, tools={"kb.search"}),
                               probes=("normal",))
        self.assertEqual(len(out["probes"]), 1)
        self.assertEqual(out["probes"][0]["probe"], "normal")

    def test_unauthorized_tool_call_is_flagged(self):
        def agent(_req):
            return {"tool_calls": [{"tool": "fs.delete", "args": {}}],
                    "answer": "", "status": "ok"}
        out = AW.runtime_probe(_target(agent, tools={"kb.search"}))
        codes = {f.get("code") for f in out["findings"]}
        self.assertTrue(codes & {"unauthorized_tool_call",
                                 "indirect_injection_hijack",
                                 "silent_failure"},
                        "越权工具调用必须产出 finding，实际 %s" % (codes,))


class TestStaticAuditEdges(unittest.TestCase):
    def test_empty_manifest_is_flagged_not_crashed(self):
        out = AW.static_audit({})
        self.assertFalse(out["passed"])
        self.assertTrue(out["findings"])

    def test_target_object_and_plain_dict_behave_the_same(self):
        m = {"tools": ["kb.search"], "system_prompt": "hi",
             "tool_policies": {}}
        a = AW.static_audit(m)
        b = AW.static_audit(AW.AgentTarget("t", lambda _r: {}, manifest=m))
        self.assertEqual(a["passed"], b["passed"])
        self.assertEqual(len(a["findings"]), len(b["findings"]))

    def test_wildcard_tool_is_high_severity(self):
        out = AW.static_audit({"tools": ["*"], "system_prompt": "x"})
        high = [f for f in out["findings"] if f.get("severity") == "high"]
        self.assertTrue(high, "通配白名单必须报高危")


if __name__ == "__main__":
    unittest.main()
