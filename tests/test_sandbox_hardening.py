# -*- coding: utf-8 -*-
"""SBX-SEC-01 红队反例（先红后绿）。

1. 容器内 os.getuid() 非 0（65534 nobody）+ tmpfs；
2. 有界并发信号量：超限快速 sandbox_overloaded；
3. app.py 对 timeout_s 硬上限 clamp（86400 -> 400 E_BAD_REQUEST）；
4. code_guard 静态扫描审计。
"""
import inspect
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import sandbox_executor as SB                                # noqa: E402


def _docker_ready():
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, timeout=10, check=False)
        if r.returncode != 0:
            return False
        img = subprocess.run(["docker", "image", "inspect", "python:3.11-alpine"],
                             capture_output=True, timeout=10, check=False)
        return img.returncode == 0
    except Exception:
        return False


class TestSandboxHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = _docker_ready()

    @unittest.skipUnless(_docker_ready(), "Docker 或镜像不可用")
    def test_container_runs_as_nobody(self):
        box = SB.DockerCodeSandbox()
        r = box.run("import os; print(os.getuid())")
        self.assertTrue(r["ok"], r)
        self.assertNotEqual(r["stdout"].strip(), "0",
                            "容器仍以 root 运行")
        self.assertEqual(r["stdout"].strip(), "65534")

    def test_concurrency_semaphore_exists(self):
        """有界并发：模块级信号量存在，超限时 run 快速拒绝。"""
        import inspect
        src = inspect.getsource(SB.DockerCodeSandbox.run)
        self.assertIn("acquire", src, "run() 未使用并发信号量")
        self.assertTrue(hasattr(SB, "_SANDBOX_SEMAPHORE") or
                        hasattr(SB.DockerCodeSandbox, "_sem"),
                        "无全局并发信号量")

    def _app_src(self):
        # FINAL-04-T1：app.py 已拆分，静态检查读合并源码
        parts = []
        for name in ("app.py", "app_core.py", "routes_agent.py",
                     "routes_enterprise.py"):
            with open(os.path.join(ROOT, "ai-service", "api", name),
                      encoding="utf-8") as f:
                parts.append(f.read())
        return "\n".join(parts)

    def test_timeout_hard_limit_in_app(self):
        """app.py handler 对 timeout_s 做 1..MAX clamp，86400 返回 400。"""
        handler_src = self._app_src()
        self.assertIn("MAX_SANDBOX_TIMEOUT", handler_src,
                      "app.py 无超时硬上限常量")
        seg = handler_src.split("def h_sandbox_run")[1].split("def ")[0]
        self.assertTrue("400" in seg or "E_BAD_REQUEST" in seg,
                        "超时越界未返回 400")

    def test_code_guard_scan_in_handler(self):
        """入容器前 code_guard.scan_code 静态扫描审计。"""
        handler_src = self._app_src()
        seg = handler_src.split("def h_sandbox_run")[1].split("def ")[0]
        self.assertIn("code_guard", seg, "未接入 code_guard 静态扫描")

    def test_huge_timeout_returns_400_over_http(self):
        """验收：/api/sandbox/run 收到 timeout_s=86400 返回 400 E_BAD_REQUEST。"""
        import json as _json
        import threading
        import urllib.error
        import urllib.request
        sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
        import app as APP
        # 真实起服务（sandbox 路由默认注册与否不影响：这里直接测 handler 逻辑）
        srv = APP.ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        old_log = APP.Handler.log_message
        APP.Handler.log_message = lambda *a, **k: None
        try:
            # 强制临时注册沙箱路由（测试隔离，用完还原）
            had = any("sandbox" in p for _m, p, _f in APP.ROUTES)
            if not had:
                APP.ROUTES.append(("POST", r"^/api/sandbox/run$",
                                   "h_sandbox_run"))
            url = "http://127.0.0.1:%d/api/sandbox/run" % srv.server_address[1]
            req = urllib.request.Request(
                url, data=_json.dumps({"code": "print(1)",
                                       "timeout_s": 86400}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("86400 超时未被拒绝")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)
                body = _json.loads(e.read().decode())
                self.assertIn("E_BAD_REQUEST", str(body))
            finally:
                if not had:
                    APP.ROUTES.pop()
        finally:
            APP.Handler.log_message = old_log
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)

    def test_concurrency_overload_is_fast(self):
        """验收：并发信号量耗尽时 run() 快速抛 SandboxOverloaded。"""
        if SB._SANDBOX_SEMAPHORE is None:
            self.skipTest("并发限制被显式关闭")
        sem = SB._SANDBOX_SEMAPHORE
        acquired = []
        n = SB._MAX_CONCURRENT
        for _ in range(n):
            sem.acquire(blocking=False)
            acquired.append(True)
        try:
            fake = SB.DockerCodeSandbox.__new__(SB.DockerCodeSandbox)
            with self.assertRaises(SB.SandboxOverloaded):
                fake.run("print(1)")
        finally:
            for _ in acquired:
                sem.release()


if __name__ == "__main__":
    unittest.main()
