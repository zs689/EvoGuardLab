# -*- coding: utf-8 -*-
"""EVO-DEEP-02：在线无人值守自主学习闭环。

必须证明三件事（不手动调用 synthesize）：
  1) 第 1 次跑任务：踩坑 -> 后续修复成功 -> 系统**自动**提炼并落库；
  2) 第 2 次同类任务：自动加载该规约（注入数 > 0），不需要人工喂 hint；
  3) 规约退化时能自动回滚/禁用（复用 monitor_and_rollback）。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import autonomous_agent as AA                               # noqa: E402
import core                                                 # noqa: E402
import skill_synthesis as SS                                # noqa: E402
import skills                                               # noqa: E402
import tool_rails as TRL                                    # noqa: E402
import tool_registry as TR                                  # noqa: E402


class ScriptedProvider:
    """按脚本依次返回工具调用，最后给答案。"""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def complete(self, messages, **kw):
        self.prompts.append(messages)
        item = self.script.pop(0) if self.script else {"answer": "done"}
        return json.dumps(item, ensure_ascii=False), 1, "scripted"


def _executor(tool, args):
    src = str((args or {}).get("src") or "")
    if src == "bad":
        return {"ok": False, "stderr": "KeyError: 'user_name'"}
    return {"ok": True, "stdout": "ok"}


def _rails():
    return TRL.load({"code.run": {"call": [{"scanner": "hidden_ascii"}],
                                  "result": [{"scanner": "hidden_ascii"}]}},
                    known_tools={"code.run"})


def _registry():
    TR.REGISTRY.reset()
    TR.REGISTRY.from_json_schema(
        "code.run", {"type": "object",
                     "properties": {"src": {"type": "string"}}},
        scope=TR.READ_ONLY, source="test", identities=["react"])
    return TR.REGISTRY


FAIL_THEN_FIX = [
    {"tools": [{"tool": "code.run", "args": {"src": "bad"}}]},
    {"tools": [{"tool": "code.run", "args": {"src": "good"}}]},
    {"answer": "done"},
]


class TestOnlineLearningLoop(unittest.TestCase):
    def setUp(self):
        self.store = core.Store(os.path.join(
            tempfile.mkdtemp(prefix="online-learn-"), "x.db"))
        self.reg = _registry()

    def tearDown(self):
        TR.REGISTRY.reset()

    def test_run1_learns_from_fail_then_fix(self):
        agent = AA.ReActAgent(
            goal="处理订单", provider=ScriptedProvider(FAIL_THEN_FIX),
            registry=self.reg, executor=_executor, max_steps=3,
            rails=_rails(), store=self.store, auto_learn=True,
            learn_task_type="order_desk", enforce=False)
        out = agent.run()
        learn = out.get("learn") or {}
        self.assertTrue(learn.get("learned"), learn)
        self.assertTrue(learn.get("skill_id"))
        # 规约真的落库了，且带具体证据
        snap = SS.latest_snapshot(self.store, learn["skill_id"])
        self.assertTrue(snap["env_invariants"])
        self.assertIn("user_name",
                      json.dumps(snap["env_invariants"], ensure_ascii=False))

    def test_run2_auto_injects_learned_rule(self):
        first = AA.ReActAgent(
            goal="处理订单", provider=ScriptedProvider(FAIL_THEN_FIX),
            registry=self.reg, executor=_executor, max_steps=3,
            rails=_rails(), store=self.store, auto_learn=True,
            learn_task_type="order_desk", enforce=False)
        first.run()
        second = AA.ReActAgent(
            goal="再处理一单", provider=ScriptedProvider([{"answer": "ok"}]),
            registry=self.reg, executor=_executor, max_steps=2,
            rails=_rails(), store=self.store, auto_learn=True,
            learn_task_type="order_desk", enforce=False)
        out2 = second.run()
        self.assertGreater(out2["learn"]["injected"], 0,
                           "第二次同类任务必须自动加载已学规约")
        joined = json.dumps(second.provider.prompts, ensure_ascii=False)
        self.assertIn("user_name", joined, "规约没有真的进到提示里")

    def test_no_pair_no_learning(self):
        """只有成功、没有失败：不该凭空学出规约。"""
        agent = AA.ReActAgent(
            goal="跑一次", provider=ScriptedProvider(
                [{"tools": [{"tool": "code.run", "args": {"src": "good"}}]},
                 {"answer": "done"}]),
            registry=self.reg, executor=_executor, max_steps=2,
            rails=_rails(), store=self.store, auto_learn=True,
            learn_task_type="order_desk", enforce=False)
        out = agent.run()
        self.assertFalse(out["learn"]["learned"])
        self.assertEqual(out["learn"]["reason"], "no_fail_then_fix_pair")

    def test_auto_learn_off_keeps_behavior_unchanged(self):
        agent = AA.ReActAgent(
            goal="处理订单", provider=ScriptedProvider(FAIL_THEN_FIX),
            registry=self.reg, executor=_executor, max_steps=3,
            rails=_rails(), store=self.store, enforce=False)
        out = agent.run()
        self.assertNotIn("learn", out, "默认关闭时不能改变返回结构")

    def test_rollback_stops_injection(self):
        """第 3 步：技能被回滚/停用后，后续任务不能再注入它的规约。"""
        agent = AA.ReActAgent(
            goal="处理订单", provider=ScriptedProvider(FAIL_THEN_FIX),
            registry=self.reg, executor=_executor, max_steps=3,
            rails=_rails(), store=self.store, auto_learn=True,
            learn_task_type="order_desk", enforce=False)
        sid = agent.run()["learn"]["skill_id"]
        self.assertTrue(sid)
        # 回滚/停用（monitor_and_rollback 在退化时就是这么做的）
        skills.disable(self.store, sid)
        after = AA.ReActAgent(
            goal="再处理一单", provider=ScriptedProvider([{"answer": "ok"}]),
            registry=self.reg, executor=_executor, max_steps=2,
            rails=_rails(), store=self.store, auto_learn=True,
            learn_task_type="order_desk", enforce=False)
        out = after.run()
        self.assertEqual(out["learn"]["injected"], 0,
                         "停用后仍注入 -> 回滚无效")

    def test_degradation_judgement_detects_drop(self):
        """退化判定本身：成功率骤降必须判 degraded（回滚的触发条件）。"""
        base = {"success_rate": 0.9, "n": 40, "unauthorized": 0}
        degraded, reasons = SS._judge_degradation(
            base, {"n": 40, "success_rate": 0.1, "unauthorized": 0,
                   "warnings": 38, "runs": 40})
        self.assertTrue(degraded, reasons)


if __name__ == "__main__":
    unittest.main()
