# -*- coding: utf-8 -*-
"""MOD-EVO-01 补救：运行时契约探针（修前必红反例）。

静态 AST 看不到「运行时改 registry 模块对象」，所以改漏 REQUIRED 时
断言全绿、隐藏测试全红。探针实际执行一次 add_field，直接检查两处
登记状态，给出精确的「哪个表没改」。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import eval_env_invariant_bench as B                        # noqa: E402


def _work(src):
    d = tempfile.mkdtemp(prefix="probe-")
    with open(os.path.join(d, "registry.py"), "w", encoding="utf-8") as f:
        f.write("FIELDS = ['id']\nREQUIRED = {'id': True}\n"
                "def is_valid(v):\n    return True\n")
    with open(os.path.join(d, "solution.py"), "w", encoding="utf-8") as f:
        f.write(src)
    return d


class TestCouplingRuntimeProbe(unittest.TestCase):
    def test_detects_missing_required(self):
        """真 bug：只 append FIELDS，没登记 REQUIRED。"""
        d = _work("import registry\n\n"
                  "def add_field(name):\n"
                  "    registry.FIELDS.append(name)\n")
        try:
            r = B.coupling_probe(d)
            self.assertFalse(r["ok"])
            self.assertTrue(any("REQUIRED" in x for x in r["problems"]), r)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_detects_missing_fields(self):
        d = _work("import registry\n\n"
                  "def add_field(name):\n"
                  "    registry.REQUIRED[name] = True\n")
        try:
            r = B.coupling_probe(d)
            self.assertFalse(r["ok"])
            self.assertTrue(any("FIELDS" in x for x in r["problems"]), r)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_both_synced_passes(self):
        d = _work("import registry\n\n"
                  "def add_field(name):\n"
                  "    registry.FIELDS.append(name)\n"
                  "    registry.REQUIRED[name] = True\n")
        try:
            r = B.coupling_probe(d)
            self.assertTrue(r["ok"], r)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_import_error_reported_not_silent(self):
        """模型忘了 import registry —— 探针必须如实报错。"""
        d = _work("def add_field(name):\n    FIELDS.append(name)\n")
        try:
            r = B.coupling_probe(d)
            self.assertFalse(r["ok"])
            self.assertTrue(r["problems"])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
