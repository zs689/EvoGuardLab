# -*- coding: utf-8 -*-
"""EG-027：语义判定缓存要有界、有 TTL、不长期复用临时失败。"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import semantic_guard as S                                  # noqa: E402


class Guard(S.SemanticGuard):
    """不连真模型：注入可控的 _call 结果序列。"""

    def __init__(self, *a, **kw):
        self.scripted = kw.pop("scripted", None)
        self.calls_made = []
        super(Guard, self).__init__(*a, **kw)

    def _call(self, mode, text):
        self.calls_made.append((mode, text))
        if self.scripted is not None:
            item = self.scripted.pop(0) if self.scripted else {"is_attack": True}
            if isinstance(item, Exception):
                raise item
            return item
        return {"is_attack": True, "reason": "ok"}


class TestSemanticCache(unittest.TestCase):
    def test_default_key_unchanged_for_reproducibility(self):
        # 改缓存策略不许弄脏已有的离线复现证据
        self.assertEqual(
            S.canonical("tool_output", "hello"),
            "bc1dd6942ec049257d76452ae6edf9156f11ecc377b733db1c584dceeea9ddee")

    def test_scope_isolates_cache_keys(self):
        a = S.canonical("tool_output", "hello", "tenant-a")
        b = S.canonical("tool_output", "hello", "tenant-b")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, S.canonical("tool_output", "hello"))

    def test_cache_is_bounded_lru(self):
        now = [1000.0]
        g = Guard(max_entries=3, ttl_s=999, clock=lambda: now[0])
        for i in range(10):
            g.judge("tool_output", "text-%d" % i)
        self.assertLessEqual(len(g.cache), 3)
        self.assertEqual(g.calls, 10)

    def test_ttl_expiry_forces_recheck(self):
        now = [1000.0]
        g = Guard(ttl_s=10, clock=lambda: now[0])
        r1 = g.judge("tool_output", "same")
        self.assertFalse(r1["cached"])
        r2 = g.judge("tool_output", "same")
        self.assertTrue(r2["cached"], "TTL 内应命中缓存")
        now[0] = 1020.0
        r3 = g.judge("tool_output", "same")
        self.assertFalse(r3["cached"], "TTL 过期必须重新判定")
        self.assertEqual(g.expired, 1)
        self.assertEqual(len(g.calls_made), 2)

    def test_temporary_failure_is_not_cached(self):
        """模型短暂不可用时不能把 unavailable 永久缓存，恢复后要真的重试。"""
        g = Guard(scripted=[OSError("down"),
                            {"is_attack": True, "reason": "recovered"}])
        r1 = g.judge("tool_output", "x")
        self.assertEqual(r1["status"], "unavailable")
        self.assertEqual(len(g.cache), 0)
        r2 = g.judge("tool_output", "x")
        self.assertEqual(r2["status"], "observed")
        self.assertFalse(r2["cached"])
        self.assertEqual(g.not_cached_failures, 1)

    def test_historical_unavailable_rows_are_dropped_on_load(self):
        d = tempfile.mkdtemp(prefix="evo-cache-")
        p = os.path.join(d, "cache.jsonl")
        good = {"key": "k1", "result": {"status": "observed",
                                        "is_attack": True, "ms": 1.0}}
        bad = {"key": "k2", "result": {"status": "unavailable",
                                       "is_attack": None, "ms": 1.0}}
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
        g = Guard(cache_path=p, scripted=[])
        self.assertIn("k1", g.cache)
        self.assertNotIn("k2", g.cache)

    def test_scope_prevents_cross_tenant_reuse(self):
        g = Guard()
        a = g.judge("tool_output", "same", scope="tenant-a")
        b = g.judge("tool_output", "same", scope="tenant-b")
        self.assertFalse(a["cached"])
        self.assertFalse(b["cached"], "不同作用域不能复用同一条判定")
        self.assertEqual(len(g.calls_made), 2)


if __name__ == "__main__":
    unittest.main()
