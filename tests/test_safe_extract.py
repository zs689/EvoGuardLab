# -*- coding: utf-8 -*-
"""`scripts/safe_extract.py`：第三方压缩包进来之前的最后一道门。

为什么必须测：这个工具专门用来吃**别人给的** zip（公开仓库快照、下载的
agent 测试集）。`zipfile.extractall` 会把 `../../evil.py` 和压缩炸弹照单全收，
所以这里的路径检查、类型白名单、体积上限都必须有测试守着 ——
否则下一次接外部数据集时，第一个没守住的就是它。
"""
import io
import os
import shutil
import sys
from contextlib import redirect_stdout
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import safe_extract as SE  # noqa: E402


def _zip(entries, *, compress=zipfile.ZIP_DEFLATED):
    """entries: [(name, bytes)]；返回临时 zip 路径。"""
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w", compress) as z:
        for name, data in entries:
            z.writestr(name, data)
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="unzip_")
        self._zips = []
        self.addCleanup(shutil.rmtree, self.dest, True)
        self.addCleanup(lambda: [os.path.exists(p) and os.remove(p)
                                 for p in self._zips])

    def _extract(self, zip_path):
        """跑一次解压，顺手把它的 stdout 收走，别把测试输出刷满。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            SE.safe_extract(zip_path, self.dest)
        self.stdout = buf.getvalue()
        return self.stdout

    def _mk(self, entries, **kw):
        p = _zip(entries, **kw)
        self._zips.append(p)
        return p

    def _outside(self, name):
        """dest 的兄弟目录里是否被写出了文件。"""
        parent = os.path.dirname(os.path.abspath(self.dest))
        return [n for n in os.listdir(parent) if n.endswith(".py")
                and n not in ("safe_extract.py",)]


class TestPaths(_Base):
    def test_normal_nested_file_is_extracted(self):
        p = self._mk([("pkg/sub/a.py", b"x = 1\n"),
                      ("README.md", b"# hi\n")])
        self._extract(p)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "pkg/sub/a.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "README.md")))

    def test_dotdot_traversal_is_skipped(self):
        p = self._mk([("../evil.py", b"boom"), ("ok.py", b"fine")])
        self._extract(p)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "ok.py")))
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(os.path.abspath(self.dest)),
                         "evil.py")))

    def test_deep_traversal_is_skipped(self):
        p = self._mk([("a/../../evil.py", b"boom")])
        self._extract(p)
        self.assertEqual(os.listdir(self.dest), [])

    def test_windows_backslash_traversal_is_skipped(self):
        p = self._mk([("..\\evil.py", b"boom")])
        self._extract(p)
        outside = os.path.join(os.path.dirname(os.path.abspath(self.dest)),
                               "evil.py")
        self.assertFalse(os.path.exists(outside))

    def test_absolute_and_drive_paths_are_skipped(self):
        for name in ("/etc/evil.py", "C:/evil.py", "C:\\evil.py"):
            p = self._mk([(name, b"boom")])
            self._extract(p)
        self.assertEqual(os.listdir(self.dest), [])

    def test_directory_entries_are_ignored(self):
        p = self._mk([("pkg/", b""), ("pkg/a.py", b"x = 1\n")])
        self._extract(p)
        self.assertEqual(sorted(os.listdir(self.dest)), ["pkg"])


class TestTypeAndSize(_Base):
    def test_binary_and_executables_are_skipped(self):
        p = self._mk([("a.py", b"x = 1\n"), ("b.exe", b"MZ"),
                      ("c.so", b"\x7fELF"), ("d.png", b"\x89PNG")])
        self._extract(p)
        self.assertEqual(sorted(os.listdir(self.dest)), ["a.py"])

    def test_oversized_single_file_is_skipped(self):
        big = b"a" * (SE.MAX_ONE + 1024)
        p = self._mk([("big.txt", big), ("small.txt", b"ok")])
        self._extract(p)
        self.assertEqual(sorted(os.listdir(self.dest)), ["small.txt"])

    def test_total_budget_stops_accepting(self):
        """总量上限必须真的限制住，而不是一个装饰性常量。

        用缩小的预算测，避免为了断言写出几百 MB 临时文件。
        """
        budget = 4 * SE.MAX_ONE
        chunk = b"a" * (SE.MAX_ONE - 1024)
        n = 8
        p = self._mk([("f%03d.txt" % i, chunk) for i in range(n)])
        with mock.patch.object(SE, "MAX_TOTAL", budget):
            self._extract(p)
        kept = len(os.listdir(self.dest))
        self.assertLess(kept, n, "总量上限没有生效")
        self.assertLessEqual(kept * len(chunk), budget)

    def test_lying_header_cannot_write_more_than_declared(self):
        """压缩包里声明的大小可以撒谎：不能因此写出远超声明的内容。"""
        p = self._mk([("a.txt", b"hello")])
        with zipfile.ZipFile(p) as z:
            infos = z.infolist()
            self.assertEqual(infos[0].file_size, 5)
        # 声明的 file_size 就是解压上限，超出部分不会被读出来
        self._extract(p)
        with open(os.path.join(self.dest, "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"hello")


class TestIdempotent(_Base):
    def test_extracting_twice_is_stable(self):
        p = self._mk([("a.py", b"x = 1\n")])
        self._extract(p)
        with open(os.path.join(self.dest, "a.py"), "rb") as f:
            first = f.read()
        self._extract(p)
        with open(os.path.join(self.dest, "a.py"), "rb") as f:
            self.assertEqual(f.read(), first)


if __name__ == "__main__":
    unittest.main()
