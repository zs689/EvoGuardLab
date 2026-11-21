# -*- coding: utf-8 -*-
"""ZERO-05-T2：coupling 精确行号反馈（修前必红反例）。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import eval_env_invariant_bench as B                        # noqa: E402


class TestPreciseFeedback(unittest.TestCase):
    def _work(self):
        d = tempfile.mkdtemp(prefix="prec-")
        with open(os.path.join(d, "registry.py"), "w", encoding="utf-8") as f:
            f.write("# 字段声明\n"
                    "FIELDS = ['id']\n"          # line 2
                    "# 必填表\n"
                    "REQUIRED = {'id': True}\n"  # line 4
                    "def is_valid(v):\n    return True\n")
        with open(os.path.join(d, "solution.py"), "w", encoding="utf-8") as f:
            f.write("import registry\n\n"
                    "def add_field(name):\n"
                    "    registry.FIELDS.append(name)\n")
        return d

    def test_probe_reports_exact_line_numbers(self):
        d = self._work()
        try:
            r = B.coupling_probe(d, precise=True)
            self.assertFalse(r["ok"])
            msg = " ".join(r["problems"])
            self.assertIn("registry.py", msg)
            self.assertIn("FIELDS", msg)
            self.assertIn("REQUIRED", msg)
            # 必须有真实行号，不是凭空编的
            self.assertIn("L2", msg)
            self.assertIn("L4", msg)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_plain_probe_still_works(self):
        d = self._work()
        try:
            r = B.coupling_probe(d)       # 不传 precise 也照旧
            self.assertFalse(r["ok"])
            self.assertTrue(r["problems"])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
