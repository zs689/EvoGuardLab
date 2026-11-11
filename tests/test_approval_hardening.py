# -*- coding: utf-8 -*-
"""评审 R04/R05 守门测试：审批必须绑定版本，消费必须是原子的。

R04：审批表里存了 candidate_version 和 idempotency_key，但 require/consume
根本不比对。用 v1 批的票能消费 v2 的调用 —— 旧批准悄悄覆盖新的执行上下文。
R05：require 先读 approved，再在另一段连接里 `UPDATE ... WHERE id=id`，
两个线程都能读到 approved，于是都返回 consumed。
"""
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import approval  # noqa: E402
import core      # noqa: E402

ARGS = {"order_id": "A1001", "total": 504.0}


def _gate():
    d = tempfile.mkdtemp(prefix="appr_hard_")
    store = core.Store(os.path.join(d, "t.db"))
    return store, approval.ApprovalGate(store)


def _pending(gate, **kw):
    try:
        gate.require("run_1", "order.settle", ARGS, **kw)
    except approval.ApprovalRequired as e:
        return e.request_id
    raise AssertionError("没有产生待批票")


class TestVersionBinding(unittest.TestCase):
    def test_approved_for_v1_cannot_consume_v2(self):
        store, gate = _gate()
        rid = _pending(gate, candidate_version="v1", idempotency_key="key-v1")
        gate.approve(rid)
        with self.assertRaises(approval.ApprovalDenied) as cm:
            gate.require("run_1", "order.settle", ARGS,
                         candidate_version="v2", idempotency_key="key-v2")
        self.assertIn("版本", str(cm.exception.reason))
        # 旧票必须还在待消费状态，不能被这次失败顺手消费掉
        self.assertEqual(gate.get(rid)["status"], approval.APPROVED)
        store.close()

    def test_same_version_consumes_once(self):
        store, gate = _gate()
        rid = _pending(gate, candidate_version="v1", idempotency_key="key-v1")
        gate.approve(rid)
        got = gate.require("run_1", "order.settle", ARGS,
                           candidate_version="v1", idempotency_key="key-v1")
        self.assertEqual(got["status"], approval.CONSUMED)
        self.assertEqual(got["candidate_version"], "v1")
        # 同一张票不能再消费第二次。失败方向必须安全：要么拒绝，要么重新走审批，
        # 绝不能悄悄复用 —— 这里断言的是「没有第二次 consumed，且旧票仍是 consumed」。
        try:
            again = gate.require("run_1", "order.settle", ARGS,
                                 candidate_version="v1", idempotency_key="key-v1")
            self.fail("同一张票被消费了两次：%r" % (again,))
        except (approval.ApprovalDenied, approval.ApprovalRequired):
            pass
        self.assertEqual(gate.get(rid)["status"], approval.CONSUMED)
        store.close()

    def test_idempotency_key_change_is_rejected(self):
        store, gate = _gate()
        rid = _pending(gate, candidate_version="v1", idempotency_key="key-v1")
        gate.approve(rid)
        with self.assertRaises(approval.ApprovalDenied) as cm:
            gate.consume(rid, run_id="run_1", tool="order.settle", args=ARGS,
                         candidate_version="v1", idempotency_key="key-v2")
        self.assertIn("幂等键", str(cm.exception.reason))
        store.close()

    def test_schema_version_is_bound_too(self):
        store, gate = _gate()
        rid = _pending(gate, candidate_version="v1",
                       tool_schema_version="schema-v1")
        gate.approve(rid)
        with self.assertRaises(approval.ApprovalDenied):
            gate.consume(rid, run_id="run_1", tool="order.settle", args=ARGS,
                         candidate_version="v1",
                         tool_schema_version="schema-v2")
        store.close()

    def test_missing_stored_version_is_not_a_wildcard(self):
        """审批里是 NULL、调用方给了 v2 —— 缺值不能当通配符。"""
        store, gate = _gate()
        rid = _pending(gate)                    # 没带任何版本
        gate.approve(rid)
        with self.assertRaises(approval.ApprovalDenied):
            gate.require("run_1", "order.settle", ARGS, candidate_version="v2")
        store.close()

    def test_version_change_does_not_reuse_stale_pending_ticket(self):
        store, gate = _gate()
        old = _pending(gate, candidate_version="v1")
        new = _pending(gate, candidate_version="v2")
        self.assertNotEqual(old, new, "版本变了还复用旧待批票")
        self.assertEqual(gate.get(old)["status"], approval.EXPIRED)
        store.close()


class TestAtomicConsume(unittest.TestCase):
    def test_two_concurrent_consumers_only_one_wins(self):
        store, gate = _gate()
        rid = _pending(gate)
        gate.approve(rid)

        barrier = threading.Barrier(2)
        original = gate.get

        def synced_get(request_id):
            # 把竞争窗口撑开：两个线程都先看到 approved，再各自尝试消费
            r = original(request_id)
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            return r

        results = []

        def consume(_):
            try:
                return gate.consume(rid, run_id="run_1", tool="order.settle",
                                    args=ARGS)["status"]
            except approval.ApprovalDenied as e:
                return "denied:" + e.reason
            except Exception as e:                # noqa: BLE001
                return "error:" + type(e).__name__

        gate.get = synced_get
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(consume, range(2)))
        finally:
            gate.get = original
        ok = [r for r in results if r == approval.CONSUMED]
        self.assertEqual(len(ok), 1,
                         "两个消费者都成功了：%r（一次性消费失效）" % (results,))
        store.close()

    def test_require_race_also_only_one_wins(self):
        store, gate = _gate()
        rid = _pending(gate)
        gate.approve(rid)

        def consume(_):
            try:
                return gate.require("run_1", "order.settle", ARGS)["status"]
            except approval.ApprovalDenied as e:
                return "denied"
            except approval.ApprovalRequired:
                return "pending"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, range(2)))
        self.assertIn(approval.CONSUMED, results)
        self.assertEqual(results.count(approval.CONSUMED), 1, results)
        store.close()

    def test_approve_is_conditional(self):
        """并发批准/拒绝不能互相覆盖：只有一条迁移能成功。"""
        store, gate = _gate()
        rid = _pending(gate)
        gate.approve(rid)
        with self.assertRaises(approval.ApprovalDenied):
            gate.approve(rid)          # 已经不是 pending
        store.close()

    def test_reject_after_consume_is_refused(self):
        store, gate = _gate()
        rid = _pending(gate)
        gate.approve(rid)
        gate.consume(rid, run_id="run_1", tool="order.settle", args=ARGS)
        with self.assertRaises(approval.ApprovalDenied):
            gate.reject(rid)
        store.close()


if __name__ == "__main__":
    unittest.main()
