# -*- coding: utf-8 -*-
"""MOD-MCP-01-T2：DockerCodeSandbox.session() 容器复用（真容器测试）。"""
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import sandbox_executor as SB                               # noqa: E402


@unittest.skipUnless(shutil.which("docker"), "本机无 docker")
class TestDockerSession(unittest.TestCase):
    def setUp(self):
        try:
            self.box = SB.DockerCodeSandbox()
        except Exception as exc:                            # noqa: BLE001
            self.skipTest("docker daemon 不可用：%s" % exc)
        self.tmp = tempfile.mkdtemp(prefix="dockersess-")
        with open(os.path.join(self.tmp, "m.py"), "w", encoding="utf-8") as f:
            f.write("def add(a,b):\n    return a+b\n")
        with open(os.path.join(self.tmp, "test_m.py"), "w",
                  encoding="utf-8") as f:
            f.write("import unittest\nfrom m import add\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_add(self):\n"
                    "        self.assertEqual(add(1,2),3)\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_reuses_container_and_cleans_up(self):
        before = set(_containers())
        with self.box.session(self.tmp) as s:
            results = [s.run(["python", "-m", "unittest", "test_m", "-q"])
                       for _ in range(3)]
            during = set(_containers())
        after = set(_containers())
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertTrue(during - before, "会话期间应有常驻容器")
        self.assertFalse(during - after, "退出会话必须清理容器")


def _containers():
    import subprocess
    try:
        r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}" ],
                           capture_output=True, timeout=10, check=False)
        return set((r.stdout or b"").decode("utf-8", "replace").splitlines())
    except Exception:                                      # noqa: BLE001
        return set()


if __name__ == "__main__":
    unittest.main()
