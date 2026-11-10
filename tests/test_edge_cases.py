# -*- coding: utf-8 -*-
"""边界与异常：正常路径之外，这些分支最容易藏 bug。

覆盖本轮新增模块的异常输入、非法状态迁移、空/缺字段、路径越界、
幂等键冲突、off/audit/enforce 三档差异。发现 bug 就修实现，不改测试放水。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard_inspect"))

import approval          # noqa: E402
import ci_policy         # noqa: E402
import conditional_skill as CS  # noqa: E402
import core              # noqa: E402
import experience_scope as ES   # noqa: E402
import real_tasks as RT  # noqa: E402
import tool_contract as TC      # noqa: E402

POLICY = {"allowed_tools": ["kb.search", "order.get_order", "order.settle"],
          "write_tools": ["order.settle"]}
# 条件技能的 allowed_tools 必须 ⊆ 冻结策略，否则先卡在校验上，测不到执行分支。
POLICY_CS = {"allowed_tools": POLICY["allowed_tools"]
             + ["kb.search_all", "kb.tier_lookup"],
             "write_tools": ["order.settle"]}
ARGS = {"order_id": "A1001", "total": 504.0}


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    s = core.Store(path)
    return s, path


class TestToolContractEdge(unittest.TestCase):
    def test_off_mode_does_not_deny(self):
        """off 用于对照，结构化失败也只能 allow，不能 deny。"""
        r = TC.authorize("nobody", "http.send", {"url": "http://x"},
                         mode="off", policy=POLICY)
        self.assertNotEqual(r["decision"], "deny")
        self.assertEqual(r["decision"], "allow")

    def test_non_dict_args_does_not_crash(self):
        r = TC.authorize("support-agent", "kb.search", "not-a-dict",
                         mode="enforce", policy=POLICY)
        self.assertIn(r["decision"], ("allow", "deny", "report"))

    def test_empty_args_on_read_tool(self):
        r = TC.authorize("support-agent", "kb.search", {}, mode="enforce",
                         policy=POLICY)
        self.assertEqual(r["decision"], "allow")

    def test_unknown_mode_falls_back_to_report(self):
        r = TC.authorize("nobody", "http.send", {}, mode="weird",
                         policy=POLICY)
        self.assertIn(r["decision"], ("allow", "report"))


class TestApprovalEdge(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _store()
        self.addCleanup(self.store.close)

    def test_resume_without_checkpoint(self):
        g = approval.ApprovalGate(self.store)
        st = g.resume("run_none")
        self.assertIsNone(st["checkpoint"])
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["approved"], [])

    def test_consume_rejects_wrong_run_id(self):
        g = approval.ApprovalGate(self.store)
        try:
            g.require("run_a", "order.settle", ARGS)
        except approval.ApprovalRequired as e:
            rid = e.request_id
        g.approve(rid)
        with self.assertRaises(approval.ApprovalDenied) as cm:
            g.consume(rid, run_id="run_b", tool="order.settle", args=ARGS)
        self.assertIn("run_id", cm.exception.reason)

    def test_consume_rejects_wrong_tool(self):
        g = approval.ApprovalGate(self.store)
        try:
            g.require("run_a", "order.settle", ARGS)
        except approval.ApprovalRequired as e:
            rid = e.request_id
        g.approve(rid)
        with self.assertRaises(approval.ApprovalDenied) as cm:
            g.consume(rid, run_id="run_a", tool="order.get_order", args=ARGS)
        self.assertIn("工具", cm.exception.reason)

    def test_approve_missing_or_non_pending(self):
        g = approval.ApprovalGate(self.store)
        with self.assertRaises(approval.ApprovalDenied):
            g.approve("appr_missing")
        try:
            g.require("run_a", "order.settle", ARGS)
        except approval.ApprovalRequired as e:
            rid = e.request_id
        g.reject(rid, "人工拒绝")
        with self.assertRaises(approval.ApprovalDenied):
            g.approve(rid)

    def test_expire_stale_also_expires_approved(self):
        clock = {"t": 1000.0}
        g = approval.ApprovalGate(self.store, ttl=10, clock=lambda: clock["t"])
        try:
            g.require("run_a", "order.settle", ARGS)
        except approval.ApprovalRequired as e:
            rid = e.request_id
        g.approve(rid)
        clock["t"] = 2000.0
        g.expire_stale()
        self.assertEqual(g.get(rid)["status"], approval.EXPIRED)

    def test_same_idempotency_key_creates_new_pending_after_consume(self):
        g = approval.ApprovalGate(self.store)
        key = "idem_fixed"
        try:
            g.require("run_a", "order.settle", ARGS, idempotency_key=key)
        except approval.ApprovalRequired as e:
            rid1 = e.request_id
        g.approve(rid1)
        d = g.consume(rid1, run_id="run_a", tool="order.settle", args=ARGS)
        self.assertEqual(d["idempotency_key"], key)
        # 同一幂等键再申请：不能复述旧审批，只能新开 pending
        try:
            g.require("run_a", "order.settle", ARGS, idempotency_key=key)
            self.fail("应当重新要求审批")
        except approval.ApprovalRequired as e:
            self.assertNotEqual(e.request_id, rid1)

    def test_list_filters(self):
        g = approval.ApprovalGate(self.store)
        try:
            g.require("run_a", "order.settle", ARGS)
        except approval.ApprovalRequired as e:
            rid = e.request_id
        self.assertEqual(len(g.list("run_a", approval.PENDING)), 1)
        self.assertEqual(len(g.list("run_b")), 0)
        g.reject(rid)
        self.assertEqual(len(g.list("run_a", approval.PENDING)), 0)
        self.assertEqual(len(g.list("run_a", approval.REJECTED)), 1)

    def test_pending_is_reused_instead_of_piling_up(self):
        """同一逻辑操作重试，不应每次多开一张待批票。"""
        g = approval.ApprovalGate(self.store)
        ids = []
        for _ in range(3):
            try:
                g.require("run_a", "order.settle", ARGS, idempotency_key="idem_1")
            except approval.ApprovalRequired as e:
                ids.append(e.request_id)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(g.list("run_a", approval.PENDING)), 1)

    def test_pending_not_reused_when_policy_changed(self):
        """权限变了，旧待批票作废，必须重新申请。"""
        g = approval.ApprovalGate(self.store)
        try:
            g.require("run_a", "order.settle", ARGS,
                      allowed_tools=["order.settle"], idempotency_key="k")
        except approval.ApprovalRequired as e:
            rid1 = e.request_id
        try:
            g.require("run_a", "order.settle", ARGS,
                      allowed_tools=["order.settle", "kb.search"],
                      idempotency_key="k")
        except approval.ApprovalRequired as e:
            rid2 = e.request_id
        self.assertNotEqual(rid1, rid2)
        self.assertEqual(g.get(rid1)["status"], approval.EXPIRED)
        self.assertEqual(g.get(rid2)["status"], approval.PENDING)


class TestRealTasksEdge(unittest.TestCase):
    def setUp(self):
        self.store, self.path = _store()
        self.addCleanup(self.store.close)
        self.gate = approval.ApprovalGate(self.store)
        self.ledger = approval.SettlementLedger(self.store)

    def test_compute_total_rejects_empty_items(self):
        with self.assertRaises(ValueError):
            RT.compute_total({"order_id": "A1001", "items": []},
                             {"discount_threshold": 500, "discount_rate": 0.1,
                              "max_total": 99999, "version": "v"})

    def test_compute_total_rejects_over_cap(self):
        with self.assertRaises(ValueError):
            RT.compute_total(
                {"order_id": "A1001", "items": [{"price": 999999, "qty": 1}]},
                {"discount_threshold": 500, "discount_rate": 0.1,
                 "max_total": 10000, "version": "v"})

    def test_compute_total_rejects_bad_prefix(self):
        with self.assertRaises(ValueError):
            RT.compute_total(
                {"order_id": "B9999", "items": [{"price": 1, "qty": 1}]},
                {"discount_threshold": 500, "discount_rate": 0.1,
                 "max_total": 10000, "allowed_order_prefix": "A",
                 "version": "v"})

    def test_fetch_unknown_order_raises(self):
        with RT.LocalOrderService({"A1001": {"order_id": "A1001", "items": []}}) as s:
            with self.assertRaises(Exception):
                s.fetch_order("A9999")

    def test_ledger_count_and_rows(self):
        c1, _ = self.ledger.settle("k1", run_id="r", order_id="A1001",
                                   total=1.0, rule_version="v")
        c2, _ = self.ledger.settle("k1", run_id="r", order_id="A1001",
                                   total=1.0, rule_version="v")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(self.ledger.count(), 1)
        self.assertEqual(self.ledger.count("A1001"), 1)
        self.assertEqual(self.ledger.count("A9999"), 0)
        self.assertEqual(len(self.ledger.rows()), 1)

    def test_ledger_same_key_different_amount_is_a_conflict(self):
        """同一幂等键、金额却不同 → 不能静默当「已结过」，必须报冲突。"""
        self.ledger.settle("k2", run_id="r", order_id="A1001",
                           total=504.0, rule_version="v")
        with self.assertRaises(approval.LedgerConflict) as cm:
            self.ledger.settle("k2", run_id="r", order_id="A1001",
                               total=999.0, rule_version="v")
        self.assertIn("金额", cm.exception.reason)
        self.assertEqual(self.ledger.count(), 1)

    def test_ledger_same_key_different_order_is_a_conflict(self):
        self.ledger.settle("k3", run_id="r", order_id="A1001",
                           total=1.0, rule_version="v")
        with self.assertRaises(approval.LedgerConflict):
            self.ledger.settle("k3", run_id="r", order_id="A1002",
                               total=1.0, rule_version="v")

    def test_ledger_float_precision_is_not_a_conflict(self):
        self.ledger.settle("k4", run_id="r", order_id="A1001",
                           total=504.0, rule_version="v")
        created, row = self.ledger.settle("k4", run_id="r2", order_id="A1001",
                                          total=504, rule_version="v")
        self.assertFalse(created)
        self.assertEqual(row["total"], 504.0)

    def test_run_settlement_denied_branch_leaves_no_side_effect(self):
        """审批存在但参数被改过 → 不能落库。"""
        with RT.LocalOrderService({"A1001": {
                "order_id": "A1001",
                "items": [{"price": 180.0, "qty": 3}]}}) as service:
            r = RT.run_settlement(store=self.store, gate=self.gate,
                                  ledger=self.ledger, service=service,
                                  run_id="run_d", order_id="A1001")
            self.assertEqual(r["approval"]["status"], "pending")
            self.gate.approve(r["approval"]["request_id"])
            # 伪造一个参数不同的调用：直接改 args 后走 consume
            with self.assertRaises(approval.ApprovalDenied):
                self.gate.consume(r["approval"]["request_id"],
                                  run_id="run_d", tool="order.settle",
                                  args={"order_id": "A1001", "total": 1.0})
            self.assertEqual(self.ledger.count("A1001"), 0)


class TestExperienceScopeEdge(unittest.TestCase):
    def test_missing_scope_field_is_excluded(self):
        rec = {"id": "x", "text": "退货政策"}
        ok, why = ES.structural_filter(rec, {})
        self.assertFalse(ok)
        self.assertTrue(any("缺适用范围字段" in w for w in why))

    def test_retrieve_top_k_truncates(self):
        recs = [ES.make_record(rid="r%d" % i, text="退货政策 第%d版" % i,
                               source_run="r", tool_contract_version="v1",
                               data_version="d1", permission_scope=["kb.search"],
                               created_at=1000, ttl=10000)
                for i in range(5)]
        out = ES.retrieve(recs, "退货政策", context={}, now=1000, top_k=2)
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(out["eligible"], 5)

    def test_bm25_single_doc(self):
        s = ES.bm25("退货", ["退货政策"])
        self.assertEqual(len(s), 1)
        self.assertGreater(s[0], 0)


class TestCiPolicyEdge(unittest.TestCase):
    def _data(self, verdict, sec_ok=True, missing=None, skipped=None,
              skip_reasons=None):
        return {"verdict": verdict, "dimension_coverage": {
            "security": {"coverage_ok": sec_ok,
                         "missing_config": missing or [],
                         "skipped_rules": skipped or [],
                         "skip_reasons": skip_reasons or []}}}

    def test_allow_limited_downgrades_to_review(self):
        d = ci_policy.evaluate(self._data("LIMITED"), {"allow_limited": True})
        self.assertEqual(d["ci_action"], "review")

    def test_allow_unknown_downgrades_to_review(self):
        d = ci_policy.evaluate(self._data("UNKNOWN"), {"allow_unknown": True})
        self.assertEqual(d["ci_action"], "review")

    def test_allowed_missing_config_downgrades_to_review(self):
        d = ci_policy.evaluate(
            self._data("PASS", sec_ok=False,
                       missing=["config:allowed_tools"],
                       skip_reasons=["配置未声明 allowed_tools"]),
            {"allow_missing_config": ["config:allowed_tools"]})
        self.assertEqual(d["ci_action"], "review")

    def test_missing_config_not_in_allowlist_still_blocks(self):
        d = ci_policy.evaluate(
            self._data("PASS", sec_ok=False,
                       missing=["config:allowed_tools"]),
            {"allow_missing_config": ["config:expect_keys"]})
        self.assertEqual(d["ci_action"], "block")

    def test_pass_rejected_when_not_in_allow_verdicts(self):
        d = ci_policy.evaluate(self._data("PASS"), {"allow_verdicts": ["RISK"]})
        self.assertEqual(d["ci_action"], "block")

    def test_require_all_dimension_coverage(self):
        data = self._data("PASS")
        data["dimension_coverage"]["reliability"] = {"coverage_ok": False}
        d = ci_policy.evaluate(data, {"require_all_dimension_coverage": True})
        self.assertEqual(d["ci_action"], "review")


class TestConditionalSkillEdge(unittest.TestCase):
    def test_fallback_defaults_to_abstain(self):
        s = CS.example_skill()
        s["fallback"] = {}
        r = CS.execute(s, {"evidence": [], "steps": 1}, POLICY_CS)
        self.assertTrue(r["fallback_used"])
        self.assertEqual(r["action"], "abstain")

    def test_stop_when_no_evidence_false_but_empty(self):
        s = CS.example_skill()
        s["stop_when"] = {}          # 未声明 no_evidence
        r = CS.execute(s, {"evidence": [], "steps": 1}, POLICY_CS)
        self.assertTrue(r["fallback_used"])
        self.assertFalse(r["stopped"])

    def test_paired_check_unsafe_flag(self):
        before = [{"key": "a", "success": False}]
        after = [{"key": "a", "success": True, "unsafe": True}]
        r = CS.paired_check(before, after)
        self.assertEqual(r["verdict"], "REJECT")
        self.assertIn("a", r["unauthorized_cases"])


if __name__ == "__main__":
    unittest.main()
