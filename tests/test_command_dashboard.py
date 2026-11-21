# -*- coding: utf-8 -*-
"""MOD-HYG-01-T2：双态 UI（普通/指挥）回归测试。

零外部 CDN —— 离线/内网环境不能白屏（这是项目的硬约束）。
"""
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "ai-service", "api")
sys.path.insert(0, API)

import app as APP                                           # noqa: E402


class TestCommandDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_log = APP.Handler.log_message
        APP.Handler.log_message = lambda *a, **k: None
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        APP.Handler.log_message = cls.old_log

    def _get(self, path):
        import urllib.request
        # 每个请求一个 actor：避免与同进程其他 HTTP 测试共享进程内限流
        # 配额而误触发 429（与本测试要验证的 UI 无关）。
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            headers={"X-Actor": "dashboard-" + str(abs(hash(path)) % 10000)})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def test_three_pages_200(self):
        for path in ("/", "/command", "/executive"):
            code, body = self._get(path)
            self.assertEqual(code, 200, path)
            self.assertTrue(body.strip(), path)

    def test_pages_have_expected_markers(self):
        _, command = self._get("/command")
        self.assertIn("<!doctype html>", command[:200].lower())
        self.assertTrue("指挥" in command or "command" in command.lower())

    def test_command_html_has_no_external_cdn(self):
        p = os.path.join(ROOT, "frontend", "command.html")
        with open(p, encoding="utf-8") as f:
            html = f.read()
        import re
        external = re.findall(r"https?://[^\s\"'<>]+", html)
        self.assertEqual(external, [],
                         "command.html 不得引用外部 CDN：%s" % external[:3])

    def test_wheel_manifest_includes_command_html(self):
        p = os.path.join(ROOT, "pyproject.toml")
        with open(p, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("command.html", text)


if __name__ == "__main__":
    unittest.main()
