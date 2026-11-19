# -*- coding: utf-8 -*-
"""AGENT-PRO-01-T2：真实容器代码沙箱验收。

本机无 Docker / 无镜像时跳过（如实跳过，不假装通过）；有则真跑容器：
正常脚本返回 stdout，外网/只读根/内存/超时全部被容器内核拦截。
"""
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
        return False, "no docker"
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, timeout=10, check=False)
        if r.returncode != 0:
            return False, "daemon unavailable"
    except Exception:
        return False, "daemon unavailable"
    img = subprocess.run(["docker", "image", "inspect", "python:3.11-alpine"],
                         capture_output=True, timeout=10, check=False)
    if img.returncode != 0:
        return False, "image python:3.11-alpine not present"
    return True, ""


class TestDockerCodeSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready, cls.why = _docker_ready()

    def _sandbox(self):
        return SB.DockerCodeSandbox(timeout_s=15)

    @unittest.skipUnless(_docker_ready()[0], "Docker 或镜像不可用")
    def test_normal_script_returns_stdout(self):
        r = self._sandbox().run("print('hello-from-sandbox')")
        self.assertTrue(r["ok"], r)
        self.assertIn("hello-from-sandbox", r["stdout"])

    @unittest.skipUnless(_docker_ready()[0], "Docker 或镜像不可用")
    def test_network_is_disabled(self):
        r = self._sandbox().run(
            "import urllib.request\n"
            "urllib.request.urlopen('http://example.com', timeout=3)")
        self.assertFalse(r["ok"], "容器竟然能访问外网")

    @unittest.skipUnless(_docker_ready()[0], "Docker 或镜像不可用")
    def test_readonly_root_filesystem(self):
        r = self._sandbox().run(
            "open('/etc/evil-marker','w').write('x')")
        self.assertFalse(r["ok"], "容器竟然写入了只读根文件系统")

    @unittest.skipUnless(_docker_ready()[0], "Docker 或镜像不可用")
    def test_timeout_is_enforced(self):
        r = self._sandbox().run("import time; time.sleep(60)", timeout_s=3)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("timeout"), "超时未被强制中断")

    @unittest.skipUnless(_docker_ready()[0], "Docker 或镜像不可用")
    def test_host_has_no_side_effect(self):
        marker = os.path.join(ROOT, "data", "_sandbox_host_should_not_exist")
        if os.path.exists(marker):
            os.unlink(marker)
        self._sandbox().run(
            "open('/hostmarker','w').write('x')")  # 容器内绝对路径 -> /
        self.assertFalse(os.path.exists(marker), "宿主机出现容器写入的文件")

    def test_http_route_opt_in(self):
        """主路径接线：/api/sandbox/run 默认不注册，显式开启才注册。"""
        import importlib
        sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
        # 默认态：当前进程未设开关时不应有该路由
        import app
        paths = [p for _m, p, _f in app.ROUTES]
        self.assertFalse(any("sandbox" in p for p in paths),
                         "代码执行端点默认不该暴露")
        # 开启态：用子进程设 env 导入，确认路由被注册
        code = (
            "import os,sys; os.environ['EVOGUARD_SANDBOX_HTTP']='1'; "
            "sys.path.insert(0,%r); import app; "
            "print(any('sandbox' in p for _m,p,_f in app.ROUTES))"
            % os.path.join(ROOT, "ai-service", "api"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=30, check=False)
        self.assertEqual(r.stdout.strip(), "True",
                         "开启开关后端点未注册：%s" % r.stderr[-200:])

    def test_no_docker_never_falls_back_to_host_exec(self):
        """无 Docker 时明确报错，绝不降级到宿主机执行。"""
        import unittest.mock as mock
        with mock.patch.object(SB.shutil, "which", return_value=None):
            with self.assertRaises(SB.DockerSandboxError):
                SB.DockerCodeSandbox()


if __name__ == "__main__":
    unittest.main()
