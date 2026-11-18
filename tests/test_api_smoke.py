# -*- coding: utf-8 -*-
"""看板服务（`ai-service/api/app.py`）的冒烟测试。

这个服务是 README 里让人 `python ai-service/api/app.py` 直接打开的门面，
但此前一条测试都没有。它同时承担两个职责：把实验数据吐给前端、把前端静态
文件发出去。后者是典型的「一个 `../` 就能读走任意文件」的位置。

这里在随机端口起真实 HTTP 服务（127.0.0.1），只改数据库与报告路径指向临时目录，
不碰仓库里的 data/，跑完关掉。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import app as APP  # noqa: E402


class _ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="api_")
        cls._saved = {}
        # 把会写盘/读仓库存量的路径全部指向临时目录
        for name in ("DB", "METRICS", "REAL_METRICS", "REAL_CHECKPOINT",
                     "REAL_REPORT", "REAL_ANALYSIS", "REAL_ATTRIBUTION",
                     "REPORT"):
            if hasattr(APP, name):
                cls._saved[name] = getattr(APP, name)
                setattr(APP, name, os.path.join(cls.tmp, os.path.basename(
                    getattr(APP, name))))
        # 服务默认把每个请求写 stderr，测试里没意义，先静音（跑完还原）
        cls._saved_log = APP.Handler.log_message
        APP.Handler.log_message = lambda *a, **k: None
        cls.srv = APP.ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        APP.Handler.log_message = cls._saved_log
        for k, v in cls._saved.items():
            setattr(APP, k, v)
        import shutil
        shutil.rmtree(cls.tmp, True)

    def _req(self, path, method="GET", body=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def _json(self, path):
        code, headers, raw = self._req(path)
        self.assertEqual(code, 200, "%s 返回 %s" % (path, code))
        return json.loads(raw.decode("utf-8"))


class TestEndpoints(_ServerCase):
    def test_health(self):
        d = self._json("/api/health")
        self.assertTrue(d["ok"])
        self.assertIn("db", d)
        # 红队 P1-2：安全模式与「输出门禁是否拦截」必须显式声明，
        # 不能让 detected 亮灯被误读成已拦截。
        sec = d["security"]
        self.assertIn(sec["mode"], ("off", "audit", "enforce"))
        self.assertEqual(sec["output_gate_enforces"],
                         sec["mode"] == "enforce")
        self.assertTrue(sec["note"])

    def test_meta_covers_dataset_and_modes(self):
        d = self._json("/api/meta")
        for k in ("agent_types", "fault_types", "modes", "strategies",
                  "tasks", "composites"):
            self.assertIn(k, d)
        self.assertEqual(sorted(d["modes"]), ["A", "B", "C", "D", "baseline"])

    def test_taxonomy_and_report_never_500(self):
        for path in ("/api/taxonomy", "/api/report"):
            code, _h, raw = self._req(path)
            self.assertEqual(code, 200)
            self.assertIn("markdown", json.loads(raw.decode()))

    def test_metrics_missing_gives_404_not_fake_numbers(self):
        code, _h, raw = self._req("/api/metrics")
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(raw.decode())["error"]["code"],
                         "E_NOT_FOUND")

    def test_real_model_without_checkpoint_is_not_ready(self):
        d = self._json("/api/real-model")
        self.assertFalse(d["ready"])
        self.assertIsNone(d["metrics"])

    def test_skills_and_experiences_readable_on_empty_db(self):
        for path in ("/api/skills", "/api/experiences", "/api/runs"):
            code, _h, raw = self._req(path)
            self.assertEqual(code, 200, path)
            self.assertIn("items", json.loads(raw.decode()), path)

    def test_unknown_route_and_unknown_run_are_404(self):
        self.assertEqual(self._req("/api/nope")[0], 404)
        self.assertEqual(self._req("/api/runs/does_not_exist")[0], 404)

    def test_post_run_rejects_bad_parameters(self):
        """空体走默认值（这是前端「点一下就跑」的便利），但非法参数必须 400。"""
        bads = [{"agent_type": "nope"},
                {"mode": "Z"},
                {"fault_type": "no_such_fault"},
                {"fault_chain": [{"type": "no_such_fault"}]},
                {"fault_chain": ["not-a-dict"]}]
        for body in bads:
            code, _h, raw = self._req("/api/run", method="POST", body=body)
            self.assertEqual(code, 400, "参数 %r 未被拒绝" % body)
            self.assertEqual(json.loads(raw.decode())["error"]["code"],
                             "E_BAD_REQUEST")

    def test_post_run_with_empty_body_uses_defaults(self):
        code, _h, raw = self._req("/api/run", method="POST", body=None)
        self.assertEqual(code, 200)
        self.assertIn("run", json.loads(raw.decode()))

    def test_post_run_with_malformed_json_is_400(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/run" % self.port,
            data=b"{not json", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(raw.decode())["error"]["code"],
                         "E_BAD_REQUEST")


class TestStaticAndHeaders(_ServerCase):
    def test_index_is_served(self):
        code, headers, raw = self._req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<", raw)

    def test_traversal_cannot_read_outside_frontend(self):
        for path in ("/../ai-service/api/app.py",
                     "/static/../../ai-service/api/app.py",
                     "/..%2f..%2fREADME.md",
                     "/%2e%2e/%2e%2e/README.md",
                     "/../.git/config"):
            code, _h, raw = self._req(path)
            self.assertNotEqual(code, 200, "%s 居然被读出来了" % path)
            self.assertNotIn(b"EvoGuardLab", raw[:2000],
                             "%s 返回了仓库内容" % path)

    def test_security_headers_on_every_response(self):
        for path in ("/", "/api/health", "/api/nope"):
            _c, headers, _r = self._req(path)
            lower = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(lower.get("x-content-type-options"), "nosniff", path)
            self.assertEqual(lower.get("x-frame-options"), "DENY", path)
            self.assertIn("default-src 'self'", lower.get("content-security-policy", ""))
            self.assertEqual(lower.get("cache-control"), "no-store", path)

    def test_json_errors_are_not_rendered_as_html(self):
        _c, headers, raw = self._req("/api/nope")
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(json.loads(raw.decode())["error"]["code"], "E_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
