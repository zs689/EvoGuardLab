# -*- coding: utf-8 -*-
"""EG-009 持久作业：状态机、幂等、租约恢复、租户隔离。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from enterprise import jobs as J                            # noqa: E402


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="evo-jobs-test-")
        self.now = [1000.0]
        self.path = os.path.join(self.dir, "jobs.db")
        self.s = J.JobStore(self.path, clock=lambda: self.now[0],
                            lease_seconds=10, max_queue=3)

    def tearDown(self):
        self.s.close()

    def test_idempotency_key_returns_original_job(self):
        a, created = self.s.submit("t", {"x": 1}, "key-1")
        b, again = self.s.submit("t", {"x": 999}, "key-1")
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.s.get("t", b["id"])["payload"], {"x": 1})
        self.assertEqual(len(self.s.list("t")), 1)

    def test_idempotency_is_per_tenant(self):
        a, _ = self.s.submit("a", {"x": 1}, "same-key")
        b, created = self.s.submit("b", {"x": 2}, "same-key")
        self.assertTrue(created)
        self.assertNotEqual(a["id"], b["id"])

    def test_claim_is_exclusive(self):
        self.s.submit("t", {"x": 1})
        self.assertIsNotNone(self.s.claim("w1"))
        self.assertIsNone(self.s.claim("w2"), "同一条不能被两个 worker 领取")

    def test_cancel_only_queued(self):
        a, _ = self.s.submit("t", {"x": 1})
        self.assertTrue(self.s.cancel("t", a["id"]))
        self.assertEqual(self.s.get("t", a["id"])["status"], "canceled")
        b, _ = self.s.submit("t", {"x": 2})
        self.s.claim("w1")
        self.assertFalse(self.s.cancel("t", b["id"]),
                         "已在运行的不能假装没跑过")

    def test_succeeded_cannot_be_replayed(self):
        a, _ = self.s.submit("t", {"x": 1})
        self.s.claim("w1")
        self.s.finish(a["id"], "succeeded", result={"ok": True})
        self.assertFalse(self.s.retry("t", a["id"]))

    def test_stale_lease_becomes_unknown_not_requeued(self):
        """崩溃恢复不许自动重放：副作用可能已经发生。"""
        a, _ = self.s.submit("t", {"x": 1})
        self.s.claim("w1")
        self.now[0] = 2000.0
        self.assertEqual(self.s.recover_stale_leases(), 1)
        job = self.s.get("t", a["id"])
        self.assertEqual(job["status"], "unknown")
        self.assertIn("副作用状态未知", job["error"])
        self.assertIsNone(self.s.claim("w2"), "unknown 不会自动回到队列")
        self.assertTrue(self.s.retry("t", a["id"]), "人工对账后可显式重放")

    def test_queue_is_bounded(self):
        for i in range(3):
            self.s.submit("t", {"x": i})
        with self.assertRaises(OverflowError):
            self.s.submit("t", {"x": 99})

    def test_tenant_isolation(self):
        self.s.submit("a", {"x": 1}, "k")
        self.assertEqual(len(self.s.list("a")), 1)
        self.assertEqual(len(self.s.list("b")), 0)
        a_id = self.s.list("a")[0]["id"]
        self.assertIsNone(self.s.get("b", a_id))

    def test_worker_processes_and_stops(self):
        seen = []

        def executor(tenant, payload):
            seen.append((tenant, payload))
            return {"echo": payload["x"]}

        w = J.JobWorker(self.s, executor, poll_interval=0.01).start()
        a, _ = self.s.submit("t", {"x": 7})
        deadline = self.now[0] + 100
        import time
        end = time.time() + 5
        while time.time() < end:
            job = self.s.get("t", a["id"])
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        w.stop()
        self.assertEqual(self.s.get("t", a["id"])["status"], "succeeded",
                         self.s.get("t", a["id"]))
        self.assertEqual(seen, [("t", {"x": 7})])
        self.assertEqual(self.s.get("t", a["id"])["result"], {"echo": 7})

    def test_two_processes_share_one_store(self):
        """两个独立 JobStore 实例指向同一文件：迁移与领取不冲突。"""
        s2 = J.JobStore(self.path, clock=lambda: self.now[0])
        try:
            a, _ = self.s.submit("t", {"x": 1})
            job = s2.claim("w-from-b")
            self.assertIsNotNone(job)
            self.assertEqual(job["id"], a["id"])
            self.assertIsNone(self.s.claim("w-from-a"))
        finally:
            s2.close()


if __name__ == "__main__":
    unittest.main()
