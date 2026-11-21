# -*- coding: utf-8 -*-
"""锚点防伪诚实性（修前必红反例）。

外部审查发现 external_anchor.py:120 无密钥时无脑 return True，
注释却承诺"存在性/一致性核对"。这里把承诺变成真行为。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import external_anchor as EA                                # noqa: E402


class TestAnchorNoKey(unittest.TestCase):
    def _no_key(self):
        old1 = os.environ.pop(EA.KEY_ENV, None)
        old2 = os.environ.pop("EVOGUARD_AUDIT_HMAC_KEY", None)
        def restore():
            for k, v in ((EA.KEY_ENV, old1),
                         ("EVOGUARD_AUDIT_HMAC_KEY", old2)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)

    def test_garbage_record_rejected_without_key(self):
        """无密钥也必须拒绝空壳记录 —— 不能无脑 return True。"""
        self._no_key()
        self.assertFalse(EA.verify_anchor({}))
        self.assertFalse(EA.verify_anchor({"seq": 1}))
        self.assertFalse(EA.verify_anchor({"seq": 1, "tail_hash": ""}))

    def test_well_formed_record_passes_without_key(self):
        self._no_key()
        rec = {"seq": 3, "tail_hash": "abc123", "ts": 1700000000.0,
               "hmac": ""}
        self.assertTrue(EA.verify_anchor(rec), "结构完整的记录应通过一致性核对")

    def test_event_anchor_verifiable(self):
        """append_event 用 4 段签名，verify 必须能验事件行。"""
        self._no_key()
        d = tempfile.mkdtemp(prefix="anchor-")
        p = os.path.join(d, "a.jsonl")
        sink = EA.ExternalAnchorSink(p)          # 无密钥
        ev = sink.append_event("auto_rollback", {"from": 2, "to": 1})
        self.assertTrue(EA.verify_anchor(ev),
                        "自己写出的事件行必须能通过校验")

    def test_tampered_record_rejected_with_key(self):
        d = tempfile.mkdtemp(prefix="anchor-")
        p = os.path.join(d, "a.jsonl")
        sink = EA.ExternalAnchorSink(p, key="k")
        rec = sink.append(seq=1, tail_hash="abc")
        bad = dict(rec, tail_hash="evil")
        self.assertFalse(EA.verify_anchor(bad, key="k"))


if __name__ == "__main__":
    unittest.main()
