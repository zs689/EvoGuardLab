# -*- coding: utf-8 -*-
"""ZERO-06：三条沙箱路径共享同一套隔离参数与并发上限（修前必红）。"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import sandbox_executor as SB                               # noqa: E402


def _docker_daemon_up():
    """client 存在不等于 daemon 可达；离线环境里真跑容器的用例应 skip。"""
    import subprocess
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "info"], capture_output=True,
                           timeout=10, check=False)
        return r.returncode == 0
    except Exception:                                        # noqa: BLE001
        return False


class TestUnifiedArgs(unittest.TestCase):
    def test_base_args_include_env_and_labels(self):
        # 只验证隔离参数构造，不需要 daemon（离线也能跑）
        box = SB.DockerCodeSandbox(probe_daemon=False)
        args = box._base_docker_args("evo-test")
        joined = " ".join(args)
        for marker in ("--network=none", "--read-only",
                       "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=/work",
                       "HOME=/tmp", "65534:65534", "--pids-limit=64",
                       "evoguard.sandbox=1"):
            self.assertIn(marker, joined, "缺隔离参数 %s" % marker)

    @unittest.skipUnless(_docker_daemon_up(), "docker daemon 不可达")
    def test_run_workspace_no_pycache_error(self):
        box = SB.DockerCodeSandbox()
        d = tempfile.mkdtemp(prefix="ws-")
        try:
            with open(os.path.join(d, "m.py"), "w", encoding="utf-8") as f:
                f.write("X = 1\n")
            with open(os.path.join(d, "test_m.py"), "w",
                      encoding="utf-8") as f:
                f.write("from m import X\nassert X == 1\n")
            r = box.run_workspace(d, ["python", "-m", "unittest",
                                      "test_m", "-q"])
            self.assertTrue(r["ok"], r)
            self.assertNotIn("PermissionError", r.get("stderr", ""))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_all_paths_use_base_args(self):
        import inspect
        for name in ("_run_inner", "run_workspace"):
            src = inspect.getsource(getattr(SB.DockerCodeSandbox, name))
            self.assertIn("_base_docker_args", src,
                          "%s 未复用统一参数" % name)
        src = inspect.getsource(SB.docker_session)
        self.assertIn("_base_docker_args", src,
                      "docker_session 未复用统一参数")


if __name__ == "__main__":
    unittest.main()
