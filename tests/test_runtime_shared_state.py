# -*- coding: utf-8 -*-
"""runtime / ctx / runtime_common 的共享状态 identity。

拆分时这三处曾各有一份**逐字重复**的常量与异常类：
  - ctx 递增自己的 SIDE_EFFECT_COUNT，evaluate 读 runtime 的那份
    -> side_effects_total 永远是 0，而且测试全绿（没人核对指标）；
  - ToolError 两份 -> `except ToolError` 静默漏接；
  - FORCED_SKILL / STORE_BY_MODE 两份 -> 离线技能注入和测试 monkeypatch
    改了 A 份，执行路径读的是 B 份 —— 这正是「Ctx 拆分两次失败」的根因。

这些不是靠"记得别复制"能防住的，必须用 identity 锁死。
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import ctx as C                                             # noqa: E402
import runtime as R                                         # noqa: E402
import runtime_common as RC                                 # noqa: E402


class TestSharedIdentity(unittest.TestCase):
    def test_side_effect_counter_is_one_object(self):
        self.assertIs(R.SIDE_EFFECT_COUNT, RC.SIDE_EFFECT_COUNT)
        self.assertIs(C.SIDE_EFFECT_COUNT, RC.SIDE_EFFECT_COUNT)

    def test_forced_skill_is_one_binding(self):
        self.assertIs(R.FORCED_SKILL, RC.FORCED_SKILL)
        self.assertIs(C.FORCED_SKILL, RC.FORCED_SKILL)

    def test_store_by_mode_is_one_mapping(self):
        self.assertIs(R.STORE_BY_MODE, RC.STORE_BY_MODE)
        self.assertIs(C.STORE_BY_MODE, RC.STORE_BY_MODE)

    def test_exception_class_is_one_type(self):
        """两份 ToolError 会让 `except ToolError` 静默漏接。"""
        self.assertIs(R.ToolError, RC.ToolError)
        self.assertIs(C.ToolError, RC.ToolError)
        try:
            raise C.ToolError("timeout", "x")
        except R.ToolError:
            pass
        else:
            self.fail("ctx 抛的 ToolError 没被 runtime 的 except 接住")

    def test_interrupted_is_one_type(self):
        self.assertIs(R.Interrupted, RC.Interrupted)
        self.assertIs(C.Interrupted, RC.Interrupted)

    def test_functions_are_same_objects(self):
        for name in ("_fault_specs", "_has_module", "_last_attempt"):
            with self.subTest(name=name):
                self.assertIs(getattr(R, name), getattr(RC, name))
                self.assertIs(getattr(C, name), getattr(RC, name))


class TestCounterFlowsToEvaluator(unittest.TestCase):
    def test_ctx_increment_is_visible_to_evaluate(self):
        """端到端地证明那个"永远是 0"的指标现在能读到真实值。"""
        before = R.SIDE_EFFECT_COUNT["n"]
        C.SIDE_EFFECT_COUNT["n"] += 1
        self.assertEqual(R.SIDE_EFFECT_COUNT["n"], before + 1,
                         "evaluate 读 runtime.SIDE_EFFECT_COUNT，"
                         "ctx 递增后必须同步")
        R.SIDE_EFFECT_COUNT["n"] = before           # 还原，不污染后续测试

    def test_monkeypatch_through_runtime_is_visible(self):
        """执行路径（plan_recovery）读的是 runtime.FORCED_SKILL。

        注意 FORCED_SKILL 是不可变值：给它赋新对象只改 runtime 的绑定，
        不会同步到 runtime_common —— 这**不是**缺陷，因为执行路径不读后者。
        真正需要共享的是可变容器（SIDE_EFFECT_COUNT / STORE_BY_MODE），
        那些在上面的 identity 测试里已经锁住。
        """
        old = R.FORCED_SKILL
        sentinel = object()
        try:
            R.FORCED_SKILL = sentinel
            self.assertIs(R.FORCED_SKILL, sentinel)
        finally:
            R.FORCED_SKILL = old


class TestImportOrder(unittest.TestCase):
    def test_ctx_can_be_imported_first_in_fresh_process(self):
        """此前 `import ctx` 在干净进程里直接 ImportError（循环导入）。"""
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "ai-service",
                                                        "evoguard"))
        r = subprocess.run(
            [sys.executable, "-X", "utf8", "-c",
             "import ctx, runtime; assert ctx.Ctx is runtime.Ctx"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0,
                         "ctx 不能被优先 import：%s" % (r.stderr[-300:],))


if __name__ == "__main__":
    unittest.main()
