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

class TestPreCommitGate(unittest.TestCase):
    """DEEP-02-T1：脏文件不得落盘（写前拦截），并给精确修复提示。"""

    ASSERT = [{"kind": "ast_symbol_sync", "file": "schema.py",
               "source_symbol": "FIELDS", "mirror_symbol": "REQUIRED"}]
    DIRTY = "FIELDS = ['id', 'status']\nREQUIRED = {'id': True}\n"
    CLEAN = ("FIELDS = ['id', 'status']\n"
             "REQUIRED = {'id': True, 'status': True}\n")

    def test_dirty_write_is_blocked_before_disk(self):
        with tempfile.TemporaryDirectory(prefix="precommit-") as d:
            r = SH.run_pre_commit_check(
                self.ASSERT, root=d, tool="fs.write",
                args={"path": "schema.py", "content": self.DIRTY})
            self.assertTrue(r["blocked"], "不同步的写入竟然放行")
            self.assertIn("REQUIRED", r["detail"])
            self.assertFalse(os.path.exists(os.path.join(d, "schema.py")),
                             "写前拦截失败：脏文件已经落盘")

    def test_precise_fix_has_symbol_and_line(self):
        with tempfile.TemporaryDirectory(prefix="precommit-") as d:
            r = SH.run_pre_commit_check(
                self.ASSERT, root=d, tool="fs.write",
                args={"path": "schema.py", "content": self.DIRTY})
            fix = " ".join(r.get("fixes") or [])
            self.assertIn("status", fix, "最小补丁没指出缺失的具体字段")
            self.assertIn("REQUIRED", fix)
            self.assertIn("schema.py:2", fix, "没给出可定位的行号")

    def test_consistent_write_passes(self):
        with tempfile.TemporaryDirectory(prefix="precommit-") as d:
            r = SH.run_pre_commit_check(
                self.ASSERT, root=d, tool="fs.write",
                args={"path": "schema.py", "content": self.CLEAN})
            self.assertFalse(r["blocked"], r["detail"])

    def test_patch_preview_uses_old_new(self):
        with tempfile.TemporaryDirectory(prefix="precommit-") as d:
            with open(os.path.join(d, "schema.py"), "w",
                      encoding="utf-8") as f:
                f.write("FIELDS = ['id']\nREQUIRED = {'id': True}\n")
            r = SH.run_pre_commit_check(
                self.ASSERT, root=d, tool="fs.patch",
                args={"path": "schema.py",
                      "old_text": "FIELDS = ['id']",
                      "new_text": "FIELDS = ['id', 'status']"})
            self.assertTrue(r["blocked"], "fs.patch 的拟写预览没生效")
            with open(os.path.join(d, "schema.py"), encoding="utf-8") as f:
                self.assertIn("['id']", f.read(), "原文件被写脏了")

    def test_hook_exposes_pre_commit(self):
        skill = {"error_signature": "AssertionError",
                 "fix_pattern": "fs.patch 局部修复",
                 "verification_check": "python -m unittest",
                 "post_write_assertions": self.ASSERT}
        hooks = SH.hooks_from_skill(skill, root=".")
        self.assertIsNotNone(hooks.get("pre_commit"))

    def test_unavailable_content_is_not_fake_passed(self):
        """拿不到拟写内容时不能假放行，必须显式标 proposed_unavailable。"""
        r = SH.run_pre_commit_check(
            self.ASSERT, root=".", tool="fs.write", args={"path": "x.py"})
        self.assertTrue(r.get("proposed_unavailable"))
        self.assertFalse(r["blocked"])


if __name__ == "__main__":
    unittest.main()
