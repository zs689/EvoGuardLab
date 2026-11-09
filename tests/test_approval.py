# -*- coding: utf-8 -*-
"""会话 5 / 6 验收：可恢复审批状态机 + 本地真实副作用幂等闭环。

计划 §6 的「必须测试的情形」逐条落成测试：
  1. 首次写入没有审批        → 没有落库副作用，状态可定位
  2. 审批前后参数发生变化    → 摘要不匹配，原审批不能复用
  3. 提交成功但响应丢失      → 相同幂等键重试后仍只有一笔结算
  4. 拒绝与过期              → 任务不会自动继续，事件有原因
  5. 暂停后进程重启          → 恢复检查点与批准状态，不重复写入
另外守住：权限变更、重复消费、首次写入与重放写入走同一个门禁。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import approval  # noqa: E402
import core      # noqa: E402
import runtime   # noqa: E402
import security  # noqa: E402
import real_tasks as RT  # noqa: E402

ARGS = {"order_id": "A1001", "total": 504.0}
ORDERS = {"A1001": {"order_id": "A1001",
                    "items": [{"price": 180.0, "qty": 3},
                              {"price": 60.0, "qty": 1}]}}


class _Base(unittest.TestCase):
    def make_store(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        store = core.Store(path)          # 非 fast：每次独立连接，可模拟进程重启
        self.addCleanup(store.close)
        return store, path

    def pending_request(self, gate, run_id="run_1", args=None, **kw):
        try:
            gate.require(run_id, "order.settle", args or ARGS, **kw)
        except approval.ApprovalRequired as e:
            return e.request_id
        self.fail("应当暂停等待审批")


class TestApprovalGate(_Base):
    def test_first_write_without_approval_has_no_side_effect(self):
        """情形 1：没审批就不落库，且状态可定位到 request_id + 检查点。"""
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        ledger = approval.SettlementLedger(store)
        cp = store.add_checkpoint("run_1", 2, {"cache": {"k": "v"}})
        with self.assertRaises(approval.ApprovalRequired) as cm:
            gate.require("run_1", "order.settle", ARGS, first_write=True)
        e = cm.exception
        self.assertEqual(e.code, "E_APPROVAL_REQUIRED")
        self.assertEqual(e.tool, "order.settle")
        req = gate.get(e.request_id)
        self.assertEqual(req["status"], approval.PENDING)
        self.assertEqual(req["checkpoint_id"], cp)
        self.assertEqual(req["first_write"], 1)
        self.assertEqual(ledger.count("A1001"), 0, "绝不能产生落库副作用")

    def test_args_changed_invalidates_approval(self):
        """情形 2：参数变了，原审批不能复用；旧审批仍是 approved 未被消费。"""
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        rid = self.pending_request(gate)
        gate.approve(rid, "zhangshuo")
        with self.assertRaises(approval.ApprovalDenied) as cm:
            gate.consume(rid, run_id="run_1", tool="order.settle",
                         args={"order_id": "A1001", "total": 505.0})
        self.assertIn("参数摘要", cm.exception.reason)
        self.assertEqual(gate.get(rid)["status"], approval.APPROVED)
        self.assertIsNone(gate.get(rid)["consumed_at"])

    def test_permission_change_invalidates_approval(self):
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        rid = self.pending_request(gate, allowed_tools=["order.settle"])
        gate.approve(rid)
        with self.assertRaises(approval.ApprovalDenied) as cm:
            gate.consume(rid, run_id="run_1", tool="order.settle", args=ARGS,
                         allowed_tools=["order.settle", "http.send"])
        self.assertIn("权限", cm.exception.reason)

    def test_duplicate_consumption_rejected(self):
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        rid = self.pending_request(gate)
        gate.approve(rid)
        self.assertEqual(gate.consume(rid, run_id="run_1", tool="order.settle",
                                      args=ARGS)["status"], approval.CONSUMED)
        with self.assertRaises(approval.ApprovalDenied) as cm:
            gate.consume(rid, run_id="run_1", tool="order.settle", args=ARGS)
        self.assertIn("消费", cm.exception.reason)

    def test_reject_does_not_auto_continue(self):
        """情形 4 前半：拒绝后任务不自动继续，且留有原因。"""
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        rid = self.pending_request(gate)
        gate.reject(rid, "人工拒绝：金额需要复核")
        req = gate.get(rid)
        self.assertEqual(req["status"], approval.REJECTED)
        self.assertIn("人工拒绝", req["reason"])
        with self.assertRaises(approval.ApprovalDenied):
            gate.consume(rid, run_id="run_1", tool="order.settle", args=ARGS)
        # 再次 require 只会产生新的 pending，不会自动执行
        new_rid = self.pending_request(gate)
        self.assertNotEqual(new_rid, rid)
        self.assertEqual(gate.get(new_rid)["status"], approval.PENDING)

    def test_expiry_does_not_auto_continue(self):
        """情形 4 后半：过期后状态 expired，任务不自动继续。"""
        store, _ = self.make_store()
        clock = {"t": 1000.0}
        gate = approval.ApprovalGate(store, ttl=10, clock=lambda: clock["t"])
        rid = self.pending_request(gate)
        gate.approve(rid)
        clock["t"] = 1020.0
        with self.assertRaises(approval.ApprovalDenied) as cm:
            gate.consume(rid, run_id="run_1", tool="order.settle", args=ARGS)
        self.assertIn("过期", cm.exception.reason)
        self.assertEqual(gate.get(rid)["status"], approval.EXPIRED)

    def test_canonical_args_normalizes_float_and_whitespace(self):
        a = approval.digest({"total": 504, "order_id": " A1001 "})
        b = approval.digest({"order_id": "A1001", "total": 504.0})
        self.assertEqual(a, b)

    def test_first_and_replay_use_the_same_gate(self):
        """首次写入与重放写入都必须过 require()，且一次性消费不能自动复用。"""
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        ledger = approval.SettlementLedger(store)
        # 首次写入：暂停
        rid1 = self.pending_request(gate, first_write=True)
        gate.approve(rid1)
        d1 = gate.consume(rid1, run_id="run_1", tool="order.settle", args=ARGS)
        created, _ = ledger.settle(d1["idempotency_key"], run_id="run_1",
                                   order_id="A1001", total=504.0,
                                   rule_version="rules-v1")
        self.assertTrue(created)
        # 重放写入：审批已消费，仍必须重新走门禁，不能自动落库
        rid2 = self.pending_request(gate, first_write=False)
        self.assertEqual(gate.get(rid2)["first_write"], 0)
        self.assertEqual(ledger.count("A1001"), 1, "重放不能重复写入")


class TestRealSettlement(_Base):
    def test_http_rules_and_ledger_end_to_end(self):
        """本地 HTTP 读订单 → 沙箱规则 → 算总额 → 审批 → 幂等落库。"""
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        ledger = approval.SettlementLedger(store)
        with RT.LocalOrderService(ORDERS) as service:
            r = RT.run_settlement(store=store, gate=gate, ledger=ledger,
                                  service=service, run_id="run_e2e",
                                  order_id="A1001")
            self.assertFalse(r["settled"])
            self.assertEqual(r["approval"]["status"], "pending")
            self.assertEqual(ledger.count("A1001"), 0)
            self.assertIn("127.0.0.1", r["http_source"])
            self.assertEqual(r["rule_version"], "rules-v1")
            self.assertEqual(r["compute"]["subtotal"], 600.0)
            self.assertEqual(r["compute"]["total"], 540.0)   # 600 * 0.9
            rid = r["approval"]["request_id"]
            gate.approve(rid)
            r2 = RT.run_settlement(store=store, gate=gate, ledger=ledger,
                                   service=service, run_id="run_e2e",
                                   order_id="A1001")
            self.assertTrue(r2["settled"])
            self.assertTrue(r2["created_this_call"])
            self.assertEqual(r2["ledger_rows_for_order"], 1)

    def test_commit_success_response_lost_still_one_row(self):
        """情形 3：提交成功但响应丢失，相同幂等键重试后仍只有一笔。"""
        store, _ = self.make_store()
        gate = approval.ApprovalGate(store)
        ledger = approval.SettlementLedger(store)
        with RT.LocalOrderService(ORDERS) as service:
            r = RT.run_settlement(store=store, gate=gate, ledger=ledger,
                                  service=service, run_id="run_lost",
                                  order_id="A1001")
            gate.approve(r["approval"]["request_id"])
            with self.assertRaises(RT.ResponseLost) as cm:
                RT.run_settlement(store=store, gate=gate, ledger=ledger,
                                  service=service, run_id="run_lost",
                                  order_id="A1001", lose_response=True)
            lost = cm.exception.result
            self.assertTrue(lost["settled"])
            key = lost["idempotency_key"]
            self.assertEqual(ledger.count("A1001"), 1)
            # 相同幂等键「查询或重试」：不新增记录
            created, row = ledger.settle(
                key, run_id="run_lost", order_id="A1001", total=540.0,
                rule_version="rules-v1")
            self.assertFalse(created)
            self.assertEqual(row["idempotency_key"], key)
            self.assertEqual(ledger.count("A1001"), 1)

    def test_process_restart_restores_checkpoint_and_approval(self):
        """情形 5：关掉 Store 再重开，检查点与批准状态都在，且不重复写入。"""
        store, path = self.make_store()
        gate = approval.ApprovalGate(store)
        store.add_checkpoint("run_restart", 4, {"cache": {"step": 4}})
        rid = self.pending_request(gate, run_id="run_restart")
        gate.approve(rid, "zhangshuo")
        store.close()                       # 模拟进程退出

        store2 = core.Store(path)
        self.addCleanup(store2.close)
        gate2 = approval.ApprovalGate(store2)
        ledger2 = approval.SettlementLedger(store2)
        state = gate2.resume("run_restart")
        self.assertIsNotNone(state["checkpoint"])
        self.assertEqual(state["checkpoint"]["state"]["cache"]["step"], 4)
        self.assertEqual(len(state["approved"]), 1)
        self.assertIn("不自动继续", state["note"])
        d = gate2.consume(rid, run_id="run_restart", tool="order.settle",
                          args=ARGS)
        created, _ = ledger2.settle(d["idempotency_key"], run_id="run_restart",
                                    order_id="A1001", total=504.0,
                                    rule_version="rules-v1")
        self.assertTrue(created)
        created2, _ = ledger2.settle(d["idempotency_key"], run_id="run_restart",
                                     order_id="A1001", total=504.0,
                                     rule_version="rules-v1")
        self.assertFalse(created2)
        self.assertEqual(ledger2.count("A1001"), 1)

    def test_rules_path_outside_sandbox_is_rejected(self):
        outside = os.path.join(tempfile.gettempdir(), "evil_rules.json")
        with self.assertRaises(ValueError):
            RT.load_rules(outside)


class TestRuntimeGateIntegration(_Base):
    """统一门禁真的接进了 runtime：首次写入与重放写入都走同一条路。"""

    def tearDown(self):
        os.environ.pop("AGENTLAB_SECURITY_MODE", None)
        os.environ.pop("AGENTLAB_SECURITY_WRAP", None)
        security._POLICY_EXTRA.clear()

    def make_ctx(self, gate=None, recovering=False):
        store, _ = self.make_store()
        exp = store.create_experiment(
            name="gate", agent_type="order", task_id="A1001",
            fault_type="none", fault_target=0, seed=7, recovery_mode="D")
        run = store.create_run(exp["id"], "D", 7)
        ctx = runtime.Ctx(store, run["id"], exp, approval_gate=gate)
        ctx.recovering = recovering
        return store, ctx

    def test_first_write_pauses_without_approval(self):
        """首次写入（非恢复期）也必须审批；不落库、状态可定位。"""
        store, ctx = self.make_ctx()
        gate = approval.ApprovalGate(store)
        ctx.approval_gate = gate
        with self.assertRaises(core.ApiError) as cm:
            ctx.call("order.settle", ARGS)
        self.assertEqual(cm.exception.code, "E_APPROVAL_REQUIRED")
        steps = store.list_steps(ctx.run_id)
        self.assertTrue(any(s["error_type"] == "approval_required" for s in steps))
        effects = [e for e in security.list_effects(store, ctx.run_id)
                   if e["where"] == "approval_gate"]
        self.assertTrue(effects)
        self.assertEqual(effects[0]["effect"], "call_stopped")
        reqs = gate.list(ctx.run_id, approval.PENDING)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["first_write"], 1)

    def test_replay_write_uses_the_same_gate(self):
        store, ctx = self.make_ctx(recovering=True)
        gate = approval.ApprovalGate(store)
        ctx.approval_gate = gate
        with self.assertRaises(core.ApiError):
            ctx.call("order.settle", ARGS)
        reqs = gate.list(ctx.run_id, approval.PENDING)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["first_write"], 0)
        # 批准后同一个门禁放行，工具真的执行
        gate.approve(reqs[0]["id"])
        out = ctx.call("order.settle", ARGS)
        self.assertTrue(out.get("settled"))
        self.assertEqual(gate.get(reqs[0]["id"])["status"], approval.CONSUMED)

    def test_default_runtime_behavior_unchanged(self):
        """不挂 gate 时历史行为不变，294 项回归与冻结数字可比。"""
        store, ctx = self.make_ctx(recovering=False)
        out = ctx.call("order.settle", ARGS)      # 首次写入照常执行
        self.assertTrue(out.get("settled"))
        store2, ctx2 = self.make_ctx(recovering=True)
        os.environ["AGENTLAB_SECURITY_MODE"] = "enforce"
        with self.assertRaises(core.ApiError) as cm:
            ctx2.call("order.settle", ARGS)       # 旧逻辑：恢复期要审批
        self.assertEqual(cm.exception.code, "E_APPROVAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()
