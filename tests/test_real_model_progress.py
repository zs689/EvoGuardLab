# -*- coding: utf-8 -*-
"""EG-019：评测状态以显式进度为准，不再用 mtime 猜「是不是跑完了」。"""
import io
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import app                                                  # noqa: E402


class TestRealModelProgress(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="evo-rm-")
        self.metrics = os.path.join(self.d, "metrics.json")
        self.ckpt = os.path.join(self.d, "runs.jsonl")
        self.prog = os.path.join(self.d, "progress.json")

    def write(self, path, obj):
        with io.open(path, "w", encoding="utf-8") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f)

    def state(self):
        return app.real_model_state(
            metrics_path=self.metrics, checkpoint_path=self.ckpt,
            progress_path=self.prog,
            report_paths={"report": os.path.join(self.d, "r.md")})

    def test_running_wins_over_newer_metrics(self):
        self.write(self.metrics, {"runs_completed": 480})
        self.write(self.ckpt, "{}\n")
        self.write(self.prog, {"schema": 1, "run_id": "run_a",
                               "status": "running", "completed": 12,
                               "total": 480})
        st = self.state()
        self.assertFalse(st["ready"], "进行中不能被当成已完成")
        self.assertTrue(st["running"])
        self.assertEqual(st["source"], "progress")
        self.assertEqual(st["run_id"], "run_a")
        self.assertIsNone(st["metrics"])

    def test_completed_exposes_artifacts_and_hashes(self):
        self.write(self.metrics, {"runs_completed": 480})
        self.write(self.ckpt, "{}\n")
        self.write(self.prog, {"schema": 1, "run_id": "run_b",
                               "status": "completed", "completed": 480,
                               "total": 480, "protocol": "p1",
                               "config_hash": "c1", "library_hash": "l1"})
        st = self.state()
        self.assertTrue(st["ready"])
        self.assertFalse(st["running"])
        self.assertEqual(st["metrics"]["runs_completed"], 480)
        self.assertEqual(st["config_hash"], "c1")
        self.assertEqual(st["library_hash"], "l1")

    def test_corrupt_progress_is_not_silently_ignored(self):
        self.write(self.metrics, {"runs_completed": 480})
        self.write(self.ckpt, "{}\n")
        self.write(self.prog, "{not json")
        st = self.state()
        self.assertFalse(st["ready"], "损坏的进度不能被当成完成")
        self.assertTrue(st["error"])
        self.assertEqual(st["source"], "progress_corrupt")

    def test_progress_without_status_is_corrupt(self):
        self.write(self.metrics, {"runs_completed": 1})
        self.write(self.prog, {"run_id": "x"})
        st = self.state()
        self.assertFalse(st["ready"])
        self.assertIn("status", st["error"])

    def test_legacy_without_progress_is_labelled(self):
        self.write(self.metrics, {"runs_completed": 480})
        self.write(self.ckpt, "{}\n")
        # metrics 比 checkpoint 新 => 旧逻辑判完成，但必须标注是推断的
        now = time.time()
        os.utime(self.ckpt, (now - 100, now - 100))
        os.utime(self.metrics, (now, now))
        st = self.state()
        self.assertTrue(st["ready"])
        self.assertEqual(st["source"], "mtime_legacy",
                         "旧产物必须标出来源，不能假装是可信进度")

    def test_legacy_detects_newer_checkpoint_as_running(self):
        self.write(self.metrics, {"runs_completed": 10})
        self.write(self.ckpt, "{}\n{}\n{}\n")
        now = time.time()
        os.utime(self.metrics, (now - 100, now - 100))
        os.utime(self.ckpt, (now, now))
        st = self.state()
        self.assertFalse(st["ready"])
        self.assertTrue(st["running"])
        self.assertEqual(st["source"], "mtime_legacy")


if __name__ == "__main__":
    unittest.main()
