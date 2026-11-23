# -*- coding: utf-8 -*-
"""fs_sandbox 的模块级单测：路径围栏。

这一层是文件工具的**唯一**边界。路径校验漏一条，工具就能读到工作目录外的
任意文件（含密钥、SSH 私钥）。原先只有经过 agent 的间接覆盖，围栏本身的
边界（绝对路径、`..`、前缀绕过、写入开关）没有单独锁定。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import fs_sandbox as FS                                    # noqa: E402


class TestNormalizeRel(unittest.TestCase):
    def test_plain_relative_path(self):
        self.assertEqual(FS._norm_rel("a/b/c.txt"), "a/b/c.txt")
        self.assertEqual(FS._norm_rel("./a//b/./c.txt"), "a/b/c.txt")
        self.assertEqual(FS._norm_rel("a\\b\\c.txt"), "a/b/c.txt")

    def test_empty_and_non_string_rejected(self):
        for bad in ("", "   ", None, 123, ["a"]):
            with self.subTest(bad=bad):
                with self.assertRaises(FS.SandboxError) as ctx:
                    FS._norm_rel(bad)
                self.assertIn("path_empty", str(ctx.exception))

    def test_absolute_paths_rejected(self):
        for bad in ("/etc/passwd", "\\\\server\\share\\x",
                    "C:/Windows/win.ini", "D:\\data\\x.txt", "c:x"):
            with self.subTest(bad=bad):
                with self.assertRaises(FS.SandboxError) as ctx:
                    FS._norm_rel(bad)
                self.assertIn("absolute_path_rejected", str(ctx.exception))

    def test_parent_escape_rejected(self):
        for bad in ("../x", "a/../../x", "a/b/../../../etc/passwd",
                    "..\\..\\windows"):
            with self.subTest(bad=bad):
                with self.assertRaises(FS.SandboxError) as ctx:
                    FS._norm_rel(bad)
                self.assertIn("parent_escape_rejected", str(ctx.exception))

    def test_illegal_chars_rejected(self):
        # 注意 "a:b" 会先被盘符正则（^[A-Za-z]:）判成绝对路径，属预期
        for bad in ("a\x00b", "a<b", 'a"b', "a|b", "a?b", "a*b"):
            with self.subTest(bad=bad):
                with self.assertRaises(FS.SandboxError) as ctx:
                    FS._norm_rel(bad)
                self.assertIn("illegal_char_in_name", str(ctx.exception))

    def test_windows_trailing_dot_is_allowed(self):
        # 末尾点在下层 OS 会让同名目录产生歧义，但规范化层不拦（保持原样）
        self.assertEqual(FS._norm_rel("a/b."), "a/b.")


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fsbox-")
        self.sandbox = FS.SandboxExecutor(self.tmp, allow_write=False)

    def test_resolve_inside_root(self):
        rel, target = self.sandbox.resolve("sub/a.txt")
        self.assertEqual(rel, "sub/a.txt")
        self.assertTrue(target.startswith(self.sandbox.root + os.sep))
        # 不存在的路径也要能解析（读时再报 not found），不能因为不存在就放行
        self.assertTrue(target.endswith(os.path.join("sub", "a.txt")))

    def test_write_requires_explicit_flag(self):
        with self.assertRaises(FS.SandboxError) as ctx:
            self.sandbox.resolve("a.txt", for_write=True)
        self.assertIn("write_disabled", str(ctx.exception))
        sb = FS.SandboxExecutor(self.tmp, allow_write=True)
        self.assertEqual(sb.resolve("a.txt", for_write=True)[0], "a.txt")

    def test_symlink_escape_rejected(self):
        """符号链接指向根外 -> realpath 解开后必须被前缀检查拦住。"""
        outside = tempfile.mkdtemp(prefix="outside-")
        target = os.path.join(outside, "secret.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("secret")
        link = os.path.join(self.tmp, "link.txt")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("本机无法创建符号链接：%s" % exc)
        with self.assertRaises(FS.SandboxError) as ctx:
            self.sandbox.resolve("link.txt")
        self.assertIn("symlink_escape_rejected", str(ctx.exception))

    def test_prefix_sibling_directory_is_not_mistaken_for_inside(self):
        """ /tmp/box2 不能被 /tmp/box 的前缀匹配放过。"""
        sibling = self.tmp + "2"
        os.makedirs(sibling, exist_ok=True)
        self.addCleanup(lambda: os.rmdir(sibling) if os.path.isdir(sibling) else None)
        # 目录名校验拦的是 :<>"|?* 等，点号本身合法；这里构造 box/../box2
        with self.assertRaises(FS.SandboxError):
            self.sandbox.resolve("../" + os.path.basename(sibling) + "/x.txt")


if __name__ == "__main__":
    unittest.main()
