# -*- coding: utf-8 -*-
"""MOD-EVO-01：声明式写后断言（修前必红反例）。

公理 A：模型知道「改 FIELDS 要同步改 REQUIRED」却仍改漏（coupling 20%），
这是执行不仔细，不是知识不足 —— 用确定性静态检查器在写后立刻报错。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import conditional_skill as CS                              # noqa: E402
import skill_hooks as SH                                    # noqa: E402


class TestAssertionSchema(unittest.TestCase):
    """断言本身必须过条件技能门禁：只认安全原语，禁任意代码。"""

    def test_legal_assertion_accepted(self):
        skill = dict(CS.example_skill())
        skill["post_write_assertions"] = [{
            "kind": "ast_symbol_sync",
            "file": "schema.py",
            "source_symbol": "FIELDS",
            "mirror_symbol": "REQUIRED",
            "message": "FIELDS 改了但 REQUIRED 没同步",
        }]
        errors = CS.validate_schema(skill)
        self.assertEqual(errors, [], errors)

    def test_eval_in_assertion_rejected(self):
        skill = dict(CS.example_skill())
        skill["post_write_assertions"] = [{
            "kind": "regex_must_match",
            "file": "x.py",
            "pattern": "__import__('os').system('rm -rf /')",
        }]
        errors = CS.validate_schema(skill)
        self.assertTrue(errors, "任意代码特征的断言必须被拒绝")

    def test_unknown_assertion_kind_rejected(self):
        skill = dict(CS.example_skill())
        skill["post_write_assertions"] = [
            {"kind": "run_shell", "command": "pytest"}]
        errors = CS.validate_schema(skill)
        self.assertTrue(any("原语" in e or "kind" in e for e in errors),
                        errors)


class TestAssertionExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, src):
        p = os.path.join(self.tmp.name, "schema.py")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(src)
        return p

    def test_ast_symbol_sync_catches_coupling_miss(self):
        """真实 coupling bug：加了字段但忘了同步 REQUIRED。"""
        self._write("FIELDS = ('a', 'b', 'new_field')\n"
                    "REQUIRED = ('a', 'b')\n")
        r = SH.run_post_write_assertions([
            {"kind": "ast_symbol_sync", "file": "schema.py",
             "source_symbol": "FIELDS", "mirror_symbol": "REQUIRED"}],
            root=self.tmp.name)
        self.assertFalse(r["ok"], "漏改 REQUIRED 必须报错")
        self.assertIn("REQUIRED", str(r["failures"]))

    def test_ast_symbol_sync_passes_when_synced(self):
        self._write("FIELDS = ('a', 'b', 'new_field')\n"
                    "REQUIRED = ('a', 'b', 'new_field')\n")
        r = SH.run_post_write_assertions([
            {"kind": "ast_symbol_sync", "file": "schema.py",
             "source_symbol": "FIELDS", "mirror_symbol": "REQUIRED"}],
            root=self.tmp.name)
        self.assertTrue(r["ok"], r)

    def test_regex_primitives(self):
        self._write("x = 1\n")
        r = SH.run_post_write_assertions([
            {"kind": "regex_must_match", "file": "schema.py",
             "pattern": r"x\s*=\s*1"},
            {"kind": "regex_must_not_match", "file": "schema.py",
             "pattern": "eval\\("},
            {"kind": "pair_presence", "file": "schema.py",
             "first": "x =", "second": "1"},
        ], root=self.tmp.name)
        self.assertTrue(r["ok"], r)

    def test_missing_file_reports_failure_not_silent_pass(self):
        r = SH.run_post_write_assertions([
            {"kind": "ast_symbol_sync", "file": "not_here.py",
             "source_symbol": "A", "mirror_symbol": "B"}],
            root=self.tmp.name)
        self.assertFalse(r["ok"], "文件不存在不能静默当通过")




class TestHookIntegration(unittest.TestCase):
    def test_post_write_hook_runs_assertion(self):
        """写后立即报漏改符号，不等到跑测试。"""
        import tempfile, os
        d = tempfile.mkdtemp(prefix="hook-")
        with open(os.path.join(d, "schema.py"), "w", encoding="utf-8") as f:
            f.write("FIELDS = ('a', 'new')\nREQUIRED = ('a',)\n")
        skill = {
            "error_signature": "AssertionError",
            "fix_pattern": "fs.patch 局部修复",
            "verification_check": "python -m unittest",
            "post_write_assertions": [{
                "kind": "ast_symbol_sync", "file": "schema.py",
                "source_symbol": "FIELDS", "mirror_symbol": "REQUIRED"}],
        }
        hooks = SH.hooks_from_skill(skill, root=d)
        r = hooks["post_write"]("fs.patch", {}, {})
        self.assertTrue(r["assertion_failed"])
        self.assertIn("REQUIRED", str(r["failures"]))

    def test_extract_coupling_assertion_from_trace(self):
        trace = [{"output": {"stderr":
                  "FIELDS changed but REQUIRED not synced -> "
                  "field missing from REQUIRED table"}}]
        a = SH.assertions_from_trace(trace)
        self.assertTrue(a)
        self.assertEqual(a[0]["source_symbol"], "FIELDS")
        self.assertEqual(a[0]["mirror_symbol"], "REQUIRED")

if __name__ == "__main__":
    unittest.main()
