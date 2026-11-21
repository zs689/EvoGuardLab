# -*- coding: utf-8 -*-
"""MOD-AGENT-01-T2：纯标准库 ast 符号检查器（修前必红反例）。

实测 coupling 域 NameError：模型写的 solution.py 直接引用 registry.FIELDS
却没 import —— 进 Docker 前用 1ms 静态检查就能抓住。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import sandbox_executor as SB                               # noqa: E402


class TestCodeSymbols(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, rel, src):
        p = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(p) or self.tmp.name, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(src)
        return p

    def test_syntax_error_detected(self):
        self._write("bad.py", "def f(:\n    pass\n")
        r = SB.check_symbols(self.tmp.name)
        self.assertFalse(r["ok"])
        self.assertTrue(any("SyntaxError" in e or "syntax" in e.lower()
                            for e in r["errors"]), r)

    def test_cross_file_undefined_name_detected(self):
        """真实 coupling bug：用了 FIELDS 但没 import registry。"""
        self._write("registry.py", "FIELDS = ['id']\nREQUIRED = {'id': True}\n")
        self._write("solution.py",
                    "def add_field(name):\n"
                    "    if name not in FIELDS:\n"
                    "        FIELDS.append(name)\n"
                    "    REQUIRED[name] = True\n")
        r = SB.check_symbols(self.tmp.name, files=["solution.py"])
        self.assertFalse(r["ok"], "未定义的 FIELDS/REQUIRED 必须报出来")
        joined = " ".join(r["errors"])
        self.assertIn("FIELDS", joined)
        self.assertIn("REQUIRED", joined)

    def test_properly_imported_module_passes(self):
        self._write("registry.py", "FIELDS = ['id']\n")
        self._write("solution.py",
                    "import registry\n\n"
                    "def add_field(name):\n"
                    "    registry.FIELDS.append(name)\n")
        r = SB.check_symbols(self.tmp.name, files=["solution.py"])
        self.assertTrue(r["ok"], r)

    def test_plain_self_contained_module_passes(self):
        self._write("calc.py",
                    "def add(a, b):\n    return a + b\n\n"
                    "assert add(1, 2) == 3\n")
        r = SB.check_symbols(self.tmp.name, files=["calc.py"])
        self.assertTrue(r["ok"], r)

    def test_only_checks_requested_files(self):
        self._write("bad.py", "def f(:\n")
        self._write("good.py", "x = 1\n")
        r = SB.check_symbols(self.tmp.name, files=["good.py"])
        self.assertTrue(r["ok"], "不该检查未请求的文件")


if __name__ == "__main__":
    unittest.main()
