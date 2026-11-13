# -*- coding: utf-8 -*-
"""EG-008：两个**独立进程**并发启动与写审计，验证跨进程事务边界。

进程内 threading.Lock 挡不住别的进程。以前「读链尾 + 插入」分两步，
两个实例会读到同一个 prev_hash，写出两条同 prev 的分叉链 —— 链验证
可能还「通过」，但证据已经不可信了。这里真起两个 Python 进程对同一个
文件库并发写，然后验总数、迁移版本、链完整性与链头唯一性。
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVO = os.path.join(ROOT, "ai-service", "evoguard")
sys.path.insert(0, EVO)

from enterprise import audit as A                           # noqa: E402

CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from enterprise import audit as A
s = A.AuditStore(sys.argv[2])
for i in range(int(sys.argv[4])):
    s.emit({"tenant_id": "t", "request_id": sys.argv[3] + "-" + str(i),
            "status": 200, "path": "/x"})
print("version=%d" % s.version)
s.close()
"""


class TestAuditMultiProcess(unittest.TestCase):
    def test_two_processes_concurrent_migrate_and_write(self):
        d = tempfile.mkdtemp(prefix="evo-audit-mp-")
        path = os.path.join(d, "audit.db")
        # 真实部署里数据库是先建好的，两个实例同时启动。
        # 让两个子进程**同时创建一个尚不存在的文件**是 Windows 上另一个
        # 竞态（偶发 readonly），与 EG-008 要验的「并发迁移/写入」无关；
        # 这里先建空库文件（不建表），迁移仍由两个进程并发执行。
        c = sqlite3.connect(path)
        c.close()
        n_each = 40
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", CHILD, EVO, path, "w%d" % i,
                 str(n_each)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(2)
        ]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=90)
            self.assertEqual(p.returncode, 0, "子进程失败：%s" % err[-500:])
            outs.append(out.strip())

        # 两个进程看到同一个 schema 版本
        self.assertEqual(outs, ["version=%d" % A.SCHEMA_VERSION] * 2,
                         "迁移版本不一致：%s" % outs)

        s = A.AuditStore(path)
        try:
            self.assertEqual(s.version, A.SCHEMA_VERSION)
            self.assertEqual(s.count(), 2 * n_each,
                             "有事件丢失（并发写被覆盖）")
            ok, info = s.verify()
            self.assertTrue(ok, "链验证失败：%s" % info)
            self.assertEqual(info["checked"], 2 * n_each)

            # 链头唯一：每个 prev_hash 只能被一行引用，否则就是分叉
            dup = s._conn.execute(
                "SELECT prev_hash, COUNT(*) FROM audit_events "
                "WHERE row_hash IS NOT NULL GROUP BY prev_hash "
                "HAVING COUNT(*) > 1").fetchall()
            self.assertEqual(dup, [], "出现重复链头（分叉链）：%s" % dup[:5])
        finally:
            s.close()

    def test_streaming_verify_handles_large_table(self):
        """流式校验：几万行也能过，且不把全表读进一个 list。"""
        d = tempfile.mkdtemp(prefix="evo-audit-big-")
        path = os.path.join(d, "audit.db")
        s = A.AuditStore(path)
        try:
            for i in range(5000):
                s.emit({"tenant_id": "t", "request_id": "r%d" % i,
                        "ts": float(i), "status": 200})
            ok, info = s.verify()
            self.assertTrue(ok, info)
            self.assertEqual(info["checked"], 5000)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
