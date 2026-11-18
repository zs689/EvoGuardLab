# -*- coding: utf-8 -*-
"""EG-014：熔断与并发上限真的接在实际调用路径上。"""
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway                                             # noqa: E402
from enterprise import resilience as R                     # noqa: E402


class FakeProvider(gateway.Provider):
    def __init__(self, fail_times=0, delay=0.0):
        self.fail_times = fail_times
        self.delay = delay
        self.calls = 0
        self.name = "fake"
        self._lock = threading.Lock()

    def complete(self, messages, temperature=0.0, max_tokens=256):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_times > 0:
            with self._lock:
                self.fail_times -= 1
            raise RuntimeError("downstream boom")
        return "ok"


class TestResilientProvider(unittest.TestCase):
    def test_breaker_opens_and_stops_calling_downstream(self):
        now = [100.0]
        b = R.CircuitBreaker(failure_threshold=2, cooldown_s=10,
                             clock=lambda: now[0])
        inner = FakeProvider(fail_times=99)
        p = gateway.ResilientProvider(inner, breaker=b)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                p.complete([])
        self.assertEqual(inner.calls, 2)
        with self.assertRaises(gateway.LLMError) as ctx:
            p.complete([])
        self.assertEqual(ctx.exception.code, "E_CIRCUIT_OPEN")
        self.assertEqual(inner.calls, 2, "熔断打开后不该再打下游")
        self.assertEqual(p.stats["rejected_open"], 1)

    def test_half_open_then_closed_after_success(self):
        now = [100.0]
        b = R.CircuitBreaker(failure_threshold=1, cooldown_s=5,
                             clock=lambda: now[0])
        inner = FakeProvider(fail_times=1)
        p = gateway.ResilientProvider(inner, breaker=b)
        with self.assertRaises(RuntimeError):
            p.complete([])
        self.assertEqual(b.state, "open")
        now[0] = 106.0
        self.assertEqual(b.state, "half_open")
        self.assertEqual(p.complete([]), "ok")
        self.assertEqual(b.state, "closed")

    def test_half_open_allows_only_one_probe_under_concurrency(self):
        """FIX-ENT-T2：HALF_OPEN 只放行 1 个探测请求，其余惊群快速拒绝。"""
        b = R.CircuitBreaker(failure_threshold=1, cooldown_s=5,
                             clock=lambda: 100.0)
        b.record(False)             # 打开熔断
        self.assertEqual(b.state, "open")
        b._opened_at = 90.0         # 冷却已过 -> 下一次 allow 进入 HALF_OPEN

        gate = threading.Barrier(12)
        allowed = []
        lock = threading.Lock()

        def worker():
            gate.wait()
            if b.allow():
                with lock:
                    allowed.append(1)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(2.0)
        self.assertEqual(len(allowed), 1,
                         "HALF_OPEN 并发放行了 %d 个探测（应为 1）"
                         % len(allowed))
        self.assertTrue(b.snapshot()["probe_in_flight"])

    def test_half_open_probe_is_counted_separately(self):
        """EG-014：半开放行的探测单独计数，惊群被拒不计入。"""
        now = [100.0]
        b = R.CircuitBreaker(failure_threshold=1, cooldown_s=5,
                             clock=lambda: now[0])
        inner = FakeProvider(fail_times=1)
        p = gateway.ResilientProvider(inner, breaker=b)
        with self.assertRaises(RuntimeError):
            p.complete([])                     # 失败 -> 打开
        now[0] = 106.0                         # 冷却 -> 半开
        self.assertEqual(p.complete([]), "ok") # 探测成功 -> 闭合
        self.assertEqual(p.stats["probe_admitted"], 1)
        self.assertEqual(p.snapshot()["probe_admitted"], 1)

    def test_bounded_concurrency_rejects_instead_of_queueing_forever(self):
        inner = FakeProvider(delay=0.5)
        p = gateway.ResilientProvider(inner, max_concurrency=1,
                                      queue_timeout=0.1)
        started = threading.Event()

        def hold():
            started.set()
            return p.complete([])

        t = threading.Thread(target=hold)
        t.start()
        started.wait(1.0)
        time.sleep(0.05)          # 确保第一个已经拿到信号量
        with self.assertRaises(gateway.LLMError) as ctx:
            p.complete([])
        self.assertEqual(ctx.exception.code, "E_OVERLOADED")
        self.assertEqual(p.stats["rejected_busy"], 1)
        t.join(2.0)

    def test_success_path_counts(self):
        inner = FakeProvider()
        p = gateway.ResilientProvider(inner)
        self.assertEqual(p.complete([]), "ok")
        self.assertEqual(p.snapshot()["ok"], 1)
        self.assertEqual(p.snapshot()["breaker"]["state"], "closed")

    def test_gateway_hook_is_opt_in(self):
        old = os.environ.pop("AGENTLAB_LLM_RESILIENCE", None)
        try:
            gateway.reset_cache()
            bare = gateway.get_provider("mock")
            self.assertIsInstance(bare, gateway.MockProvider,
                                  "评测路径默认不套熔断")
            os.environ["AGENTLAB_LLM_RESILIENCE"] = "1"
            gateway.reset_cache()
            wrapped = gateway.get_provider("mock")
            self.assertIsInstance(wrapped, gateway.ResilientProvider)
            self.assertIsInstance(wrapped.inner, gateway.MockProvider)
            out = wrapped.complete([{"role": "user", "content": "hi"}])
            self.assertIsNone(out[0], "mock 不产生内容，只回 tokens 与模型名")
            self.assertEqual(out[2], "mock")
        finally:
            os.environ.pop("AGENTLAB_LLM_RESILIENCE", None)
            gateway.reset_cache()
            if old is not None:
                os.environ["AGENTLAB_LLM_RESILIENCE"] = old

    def test_server_entry_enables_resilience(self):
        """服务入口必须默认开启；评测脚本不设该变量。"""
        p = os.path.join(ROOT, "ai-service", "api", "app.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('setdefault("AGENTLAB_LLM_RESILIENCE", "1")', src)


if __name__ == "__main__":
    unittest.main()
