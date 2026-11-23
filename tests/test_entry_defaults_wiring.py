# -*- coding: utf-8 -*-
"""安全开关必须在**运行时**读 env，不能用 import 期快照。

app_core 在模块加载时冻结 _LIGHT_GUARD_ON；而入口默认 apply_entry_defaults()
是在 main() 里才执行的，于是裸启动时轻量注入拦截永远是关的。
反过来把 apply 提到 import 之前又会污染所有测试环境（把 exec 模式切成
controlled），所以正确解是让路由在调用时读 env。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import routes_agent as RA                                  # noqa: E402


class TestRuntimeFlag(unittest.TestCase):
    def test_flag_reads_env_at_call_time(self):
        key = "EVOGUARD_LIGHT_GUARD"
        old = os.environ.get(key)
        try:
            os.environ.pop(key, None)
            self.assertFalse(RA._light_guard_on())
            os.environ[key] = "1"
            self.assertTrue(RA._light_guard_on())
            os.environ[key] = "true"
            self.assertTrue(RA._light_guard_on())
            os.environ[key] = "0"
            self.assertFalse(RA._light_guard_on())
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def test_harmful_judge_flag_same_semantics(self):
        key = "EVOGUARD_HARMFUL_JUDGE"
        old = os.environ.get(key)
        try:
            os.environ.pop(key, None)
            self.assertFalse(RA._harmful_judge_on())
            os.environ[key] = "on"
            self.assertTrue(RA._harmful_judge_on())
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


if __name__ == "__main__":
    unittest.main()
