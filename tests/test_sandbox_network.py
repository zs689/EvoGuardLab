# -*- coding: utf-8 -*-
"""PROD-04-T1：受限只读命令 + HTTP Fetch 的 SSRF/注入对抗。"""
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import sandbox_executor as SB                               # noqa: E402


class TestReadOnlyCommand(unittest.TestCase):
    def test_allowed_command_runs(self):
        r = SB.run_readonly_command(["echo", "hello"])
        self.assertEqual(r["stdout"].strip(), "hello")
        self.assertEqual(r["cmd"], "echo")

    def test_command_not_allowlisted(self):
        for cmd in ("rm", "bash", "python", "curl"):
            with self.assertRaises(SB.SandboxError):
                SB.run_readonly_command([cmd, "-rf", "/"])

    def test_shell_metachars_rejected(self):
        """管道/分号/反引号全部拒（shell=False 之外的第二道防线）。"""
        for bad in ("a; rm -rf /", "a | cat", "`id`", "$(id)", "a\nb", "a\\b"):
            with self.assertRaises(SB.SandboxError, msg=bad):
                SB.run_readonly_command(["echo", bad])

    def test_no_shell_true(self):
        """命令替换串被元字符防线挡掉，根本到不了 shell。"""
        with self.assertRaises(SB.SandboxError):
            SB.run_readonly_command(["echo", "$(whoami)"])
        # 普通文本不经 shell，原样输出
        r = SB.run_readonly_command(["echo", "literal text"])
        self.assertIn("literal text", r["stdout"])

    def test_output_cap(self):
        with self.assertRaises(SB.SandboxError):
            SB.run_readonly_command(["echo", "x" * 100], max_bytes=10)

    def test_cwd_confined(self):
        d = tempfile.mkdtemp(prefix="sbox-")
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("ok")
        r = SB.run_readonly_command(["cat", "a.txt"], cwd=d)
        self.assertIn("ok", r["stdout"])


class TestSSRF(unittest.TestCase):
    def setUp(self):
        self.net = SB.NetworkSandbox(allowed_domains=["example.com"])

    def test_private_ips_blocked(self):
        for host in ("127.0.0.1", "169.254.169.254", "10.0.0.1",
                     "192.168.1.1", "172.16.0.1", "[::1]", "localhost"):
            with self.assertRaises(SB.SandboxError, msg=host):
                self.net.check_url("http://%s/latest/meta-data" % host)

    def test_domain_not_allowlisted(self):
        with self.assertRaises(SB.SandboxError):
            self.net.check_url("http://evil.example/x")

    def test_scheme_not_allowed(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x",
                    "gopher://example.com"):
            with self.assertRaises(SB.SandboxError, msg=url):
                self.net.check_url(url)

    def test_allowed_domain_passes(self):
        host, u = self.net.check_url("https://example.com/a")
        self.assertEqual(host, "example.com")

    def test_fetch_uses_allowlist_before_network(self):
        """未白名单域名必须在发请求**之前**就拒（opener 不得被调用）。"""
        called = {"n": 0}
        def opener(url, timeout=0):
            called["n"] += 1
            raise AssertionError("不该真的发请求")
        with self.assertRaises(SB.SandboxError):
            self.net.fetch("http://127.0.0.1:8080/secret", opener=opener)
        self.assertEqual(called["n"], 0)

    def test_fetch_body_capped(self):
        class R:
            def __init__(self, b): self.b = b
            def read(self, n): return self.b[:n]
            def __enter__(self): return self
            def __exit__(self, *a): return False
        net = SB.NetworkSandbox(allowed_domains=["example.com"], max_bytes=5)
        with self.assertRaises(SB.SandboxError):
            net.fetch("https://example.com/x",
                      opener=lambda url, timeout=0: R(b"x" * 20))


if __name__ == "__main__":
    unittest.main()
