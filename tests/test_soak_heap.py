# -*- coding: utf-8 -*-
"""EG-010：长时 soak 的堆指标不能被观测器自己污染。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import store_remaining_checks as R                          # noqa: E402


class TestSoakHeapObserver(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_sampling_does_not_accumulate_in_heap(self):
        root = tempfile.mkdtemp(prefix="evo-soakheap-")
        r = R.check_soak(root, seconds=10, workers=2)
        ev = r["evidence"]
        self.assertTrue(r["pass"], r["detail"])
        self.assertTrue(ev["observer_excluded"])
        self.assertTrue(ev["heap_stable"], "采样点驻留内存会让堆假涨")
        self.assertIsNotNone(ev["heap_growth_mb"])
        self.assertLess(ev["heap_growth_mb"], 0.15)
        self.assertEqual(len(ev["samples"]), 2)
        # 临时 JSONL 必须清掉，不留在工作目录
        self.assertFalse(os.path.isfile(
            os.path.join(root, "soak", "samples.jsonl")))
        # 产物格式向后兼容：每个采样点仍带 heap_mb / pool
        for s in ev["samples"]:
            self.assertIn("heap_mb", s)
            self.assertIn("pool", s)


if __name__ == "__main__":
    unittest.main()
