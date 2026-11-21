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


class TestUnifiedArgs(unittest.TestCase):
    def test_base_args_include_env_and_labels(self):
        box = SB.DockerCodeSandbox()
        args = box._base_docker_args("evo-test")
        joined = " ".join(args)
        for marker in ("--network=none", "--read-only",
                       "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=/work",
                       "HOME=/tmp", "65534:65534", "--pids-limit=64",
                       "evoguard.sandbox=1"):
            self.assertIn(marker, joined, "缺隔离参数 %s" % marker)

    @unittest.skipUnless(shutil.which("docker"), "无 docker")
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
