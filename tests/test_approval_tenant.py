# -*- coding: utf-8 -*-
"""EG-026：审批绑定租户、消费后要有效果回执、崩溃后可对账。"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import approval  # noqa: E402
import core      # noqa: E402

ARGS = {"order_id": "A1001", "total": 504.0}


class TestApprovalTenantAndReconcile(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.store = core.Store(self.path)
        self.addCleanup(self.store.close)
        self.gate = approval.ApprovalGate(self.store)

    def pending(self, tenant="acme", run_id="run_t"):
        try:
            self.gate.require(run_id, "order.settle", ARGS, tenant_id=tenant)
        except approval.ApprovalRequired as e:
            return e.request_id
        self.fail("应当暂停等待审批")

    def test_approval_is_bound_to_tenant(self):
        rid = self.pending("acme")
        self.assertEqual(self.gate.get(rid)["tenant_id"], "acme")
        with self.assertRaises(approval.ApprovalDenied) as cm:
            self.gate.approve(rid, approved_by="bob", tenant_id="beta")
        self.assertIn("租户", str(cm.exception))
        self.assertEqual(self.gate.get(rid)["status"], approval.PENDING)
        self.gate.approve(rid, approved_by="alice", tenant_id="acme")
        self.assertEqual(self.gate.get(rid)["status"], approval.APPROVED)

    def test_cross_tenant_reuse_is_denied(self):
        rid = self.pending("acme")
        self.gate.approve(rid, tenant_id="acme")
        with self.assertRaises(approval.ApprovalDenied):
            self.gate.require("run_t", "order.settle", ARGS, tenant_id="beta")
        # 同租户可以正常消费
        self.gate.require("run_t", "order.settle", ARGS, tenant_id="acme")
        self.assertEqual(self.gate.get(rid)["status"], approval.CONSUMED)

    def test_consumed_without_receipt_shows_up_in_reconcile(self):
        rid = self.pending("acme")
        self.gate.approve(rid, tenant_id="acme")
        self.gate.require("run_t", "order.settle", ARGS, tenant_id="acme")
        rows = self.gate.reconcile(tenant_id="acme")
        self.assertEqual([r["id"] for r in rows], [rid])
        self.assertIsNone(rows[0]["effect_status"])
        self.assertEqual(self.gate.reconcile(tenant_id="beta"), [],
                         "别的租户不该看到这条")

    def test_mark_effect_clears_reconcile(self):
        rid = self.pending("acme")
        self.gate.approve(rid, tenant_id="acme")
        self.gate.require("run_t", "order.settle", ARGS, tenant_id="acme")
        self.assertTrue(self.gate.mark_effect(
            rid, ok=True, receipt="ext-settle-2026-11-13-001"))
        self.assertEqual(self.gate.reconcile(tenant_id="acme"), [])
        row = self.gate.get(rid)
        self.assertEqual(row["effect_status"], "succeeded")
        self.assertIn("ext-settle", row["effect_receipt"])

    def test_failed_effect_is_recorded_not_hidden(self):
        rid = self.pending("acme")
        self.gate.approve(rid, tenant_id="acme")
        self.gate.require("run_t", "order.settle", ARGS, tenant_id="acme")
        self.assertTrue(self.gate.mark_effect(rid, ok=False,
                                              receipt="timeout: upstream"))
        self.assertEqual(self.gate.get(rid)["effect_status"], "failed")
        self.assertEqual(self.gate.reconcile(tenant_id="acme"), [],
                         "已回执（哪怕失败）就不该留在待对账里")

    def test_migration_is_idempotent(self):
        approval.ApprovalGate(self.store)
        approval.ApprovalGate(self.store)
        with self.store.conn() as c:
            cols = {r[1] for r in
                    c.execute("PRAGMA table_info(approval_requests)").fetchall()}
        for c in ("tenant_id", "effect_status", "effect_receipt"):
            self.assertIn(c, cols)

    def test_legacy_request_without_tenant_still_works(self):
        """老库里的审批没有 tenant_id，不能因为加了绑定就全部拒绝。"""
        rid = self.pending(None)
        self.assertIsNone(self.gate.get(rid)["tenant_id"])
        self.gate.approve(rid, approved_by="human")
        self.gate.require("run_t", "order.settle", ARGS, tenant_id="acme")
        self.assertEqual(self.gate.get(rid)["status"], approval.CONSUMED)


if __name__ == "__main__":
    unittest.main()
