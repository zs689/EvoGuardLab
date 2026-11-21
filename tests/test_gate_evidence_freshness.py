# -*- coding: utf-8 -*-
"""gate 证据新鲜度（修前必红反例）。

29/29 gate 里有 8 项只读 docs/review-remediation/probes/*.json 里
passed:true 就放行。源码改了、证据没重跑，gate 依然绿——注水点。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import review_checks as RC                                  # noqa: E402


class TestEvidenceFreshness(unittest.TestCase):
    def test_stale_evidence_is_rejected(self):
        """证据比它验证的源码旧 -> 必须判红。"""
        d = tempfile.mkdtemp(prefix="ev-fresh-")
        ev = os.path.join(d, "probe.json")
        src = os.path.join(d, "source.py")
        with open(ev, "w", encoding="utf-8") as f:
            f.write('{"passed": true}')
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        os.utime(ev, (1, 1))            # 证据 1970 年，源码现在
        r = RC.evidence_freshness([(ev, [src])])
        self.assertFalse(r["fresh"], r)
        self.assertTrue(any("过期" in x or "stale" in x.lower()
                            for x in r["stale"]), r)

    def test_fresh_evidence_passes(self):
        d = tempfile.mkdtemp(prefix="ev-fresh-")
        ev = os.path.join(d, "probe.json")
        src = os.path.join(d, "source.py")
        with open(ev, "w", encoding="utf-8") as f:
            f.write('{"passed": true}')
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        os.utime(src, (1, 1))            # 源码旧，证据新
        r = RC.evidence_freshness([(ev, [src])])
        self.assertTrue(r["fresh"], r)
        self.assertEqual(r["stale"], [])

    def test_missing_evidence_is_stale(self):
        d = tempfile.mkdtemp(prefix="ev-fresh-")
        src = os.path.join(d, "source.py")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        r = RC.evidence_freshness([(os.path.join(d, "gone.json"), [src])])
        self.assertFalse(r["fresh"], r)

    def test_real_gate_discloses_frozen_evidence(self):
        """可重跑的证据必须新于源码；一次性探针必须显式冻结并给理由。"""
        r = RC.c_evidence_freshness()
        self.assertEqual(r["status"], "pass", r["detail"])
        pairs = [(os.path.join(RC.ROOT, ev), srcs)
                 for ev, srcs in RC.EVIDENCE_SOURCES.items()]
        v = RC.evidence_freshness(pairs)
        self.assertFalse(v["stale"], v["stale"])
        self.assertEqual(v["checked"], 8)
        # 冻结不是免死金牌：必须带理由，否则等于静默放行
        for item in v["frozen"]:
            self.assertIn("冻结：", item)
        self.assertTrue(v["frozen"], "应如实列出无法自动重跑的证据")

    def test_frozen_without_reason_is_not_honored(self):
        """标了 frozen 但没理由 —— 不能蒙混过关。"""
        d = tempfile.mkdtemp(prefix="ev-frozen-")
        ev = os.path.join(d, "p.json")
        src = os.path.join(d, "s.py")
        with open(ev, "w", encoding="utf-8") as f:
            f.write('{"passed":true,"frozen":true}')
        with open(src, "w", encoding="utf-8") as f:
            f.write("x=1\n")
        os.utime(ev, (1, 1))
        v = RC.evidence_freshness([(ev, [src])])
        self.assertFalse(v["fresh"], "冻结必须带 frozen_reason")


if __name__ == "__main__":
    unittest.main()
