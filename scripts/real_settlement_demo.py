# -*- coding: utf-8 -*-
"""会话 5/6 演示：把 5 个「必须测试的情形」跑成一份可核对的报告。

输出默认进 experiments/real_settlement_v1/，不覆盖任何冻结证据。
每个情形用独立临时库，证据逐条落进 demo.json，报告里的每个数字都能回指。
"""
import argparse
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import approval        # noqa: E402
import core            # noqa: E402
import real_tasks as RT  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "experiments", "real_settlement_v1")
ARGS = {"order_id": "A1001", "total": 504.0}
ORDERS = {"A1001": {"order_id": "A1001",
                    "items": [{"price": 180.0, "qty": 3},
                              {"price": 60.0, "qty": 1}]}}


def _store(d):
    return core.Store(os.path.join(d, "demo.db"))


def _pending(gate, run_id="run_1", **kw):
    try:
        gate.require(run_id, "order.settle", ARGS, **kw)
    except approval.ApprovalRequired as e:
        return e
    raise AssertionError("应当暂停等待审批")


def case_first_write(d):
    store = _store(d)
    try:
        gate = approval.ApprovalGate(store)
        ledger = approval.SettlementLedger(store)
        cp = store.add_checkpoint("run_1", 2, {"cache": {"step": 2}})
        e = _pending(gate, first_write=True)
        req = gate.get(e.request_id)
        return {
            "pass": ledger.count("A1001") == 0 and req["status"] == "pending"
                    and req["checkpoint_id"] == cp,
            "evidence": {"request_id": e.request_id, "checkpoint_id": cp,
                         "status": req["status"], "ledger_rows": ledger.count("A1001"),
                         "tool_executed": False},
            "note": "没有审批 → 写操作暂停，零落库副作用，状态可定位到 request + 检查点",
        }
    finally:
        store.close()


def case_args_changed(d):
    store = _store(d)
    try:
        gate = approval.ApprovalGate(store)
        e = _pending(gate)
        gate.approve(e.request_id, "zhangshuo")
        try:
            gate.consume(e.request_id, run_id="run_1", tool="order.settle",
                         args={"order_id": "A1001", "total": 505.0})
            denied = None
        except approval.ApprovalDenied as ex:
            denied = ex.reason
        req = gate.get(e.request_id)
        return {
            "pass": denied is not None and "参数摘要" in denied
                    and req["status"] == "approved" and req["consumed_at"] is None,
            "evidence": {"deny_reason": denied, "original_digest": req["args_digest"],
                         "changed_digest": approval.digest(
                             {"order_id": "A1001", "total": 505.0}),
                         "request_status": req["status"], "consumed_at": None},
            "note": "参数变化 → 摘要不匹配，原审批不能复用，且未被偷偷消费",
        }
    finally:
        store.close()


def case_response_lost(d):
    store = _store(d)
    try:
        gate = approval.ApprovalGate(store)
        ledger = approval.SettlementLedger(store)
        with RT.LocalOrderService(ORDERS) as service:
            r1 = RT.run_settlement(store=store, gate=gate, ledger=ledger,
                                   service=service, run_id="run_lost",
                                   order_id="A1001")
            gate.approve(r1["approval"]["request_id"])
            try:
                RT.run_settlement(store=store, gate=gate, ledger=ledger,
                                  service=service, run_id="run_lost",
                                  order_id="A1001", lose_response=True)
                lost = None
            except RT.ResponseLost as ex:
                lost = ex.result
            key = lost["idempotency_key"]
            created, row = ledger.settle(
                key, run_id="run_lost", order_id="A1001", total=540.0,
                rule_version="rules-v1")
            n = ledger.count("A1001")
            return {
                "pass": lost is not None and lost["settled"] and not created and n == 1,
                "evidence": {
                    "http_source": r1["http_source"],
                    "rules_path": r1["rules_path"],
                    "rule_version": r1["rule_version"],
                    "compute": r1["compute"],
                    "idempotency_key": key,
                    "committed_before_loss": lost["settled"],
                    "retry_created_new_row": created,
                    "ledger_rows_for_order": n,
                    "ledger_row": {k: row[k] for k in
                                   ("idempotency_key", "order_id", "total",
                                    "rule_version")},
                },
                "note": "提交成功但响应丢失 → 同一幂等键重试，仍只有一笔结算",
            }
    finally:
        store.close()


def case_reject_and_expire(d):
    store = _store(d)
    try:
        gate = approval.ApprovalGate(store)
        e = _pending(gate)
        gate.reject(e.request_id, "人工拒绝：金额需要复核")
        rejected = gate.get(e.request_id)

        clock = {"t": 1000.0}
        gate2 = approval.ApprovalGate(store, ttl=10, clock=lambda: clock["t"])
        e2 = _pending(gate2, run_id="run_exp")
        gate2.approve(e2.request_id)
        clock["t"] = 1020.0
        try:
            gate2.consume(e2.request_id, run_id="run_exp", tool="order.settle",
                          args=ARGS)
            expired_reason = None
        except approval.ApprovalDenied as ex:
            expired_reason = ex.reason
        expired = gate2.get(e2.request_id)
        return {
            "pass": rejected["status"] == "rejected" and expired["status"] == "expired"
                    and expired_reason is not None,
            "evidence": {
                "rejected_status": rejected["status"],
                "rejected_reason": rejected["reason"],
                "expired_status": expired["status"],
                "expired_reason": expired_reason,
                "tool_executed": False,
            },
            "note": "拒绝与过期都留下原因，任务不会自动继续，也没有写操作被执行",
        }
    finally:
        store.close()


def case_restart(d):
    path = os.path.join(d, "restart.db")
    store = core.Store(path)
    gate = approval.ApprovalGate(store)
    store.add_checkpoint("run_restart", 4, {"cache": {"step": 4}})
    e = _pending(gate, run_id="run_restart")
    gate.approve(e.request_id, "zhangshuo")
    store.close()                         # 模拟进程退出

    store2 = core.Store(path)
    try:
        gate2 = approval.ApprovalGate(store2)
        ledger2 = approval.SettlementLedger(store2)
        state = gate2.resume("run_restart")
        dec = gate2.consume(e.request_id, run_id="run_restart",
                            tool="order.settle", args=ARGS)
        created, _ = ledger2.settle(dec["idempotency_key"], run_id="run_restart",
                                     order_id="A1001", total=504.0,
                                     rule_version="rules-v1")
        created2, _ = ledger2.settle(dec["idempotency_key"], run_id="run_restart",
                                      order_id="A1001", total=504.0,
                                      rule_version="rules-v1")
        n = ledger2.count("A1001")
        return {
            "pass": state["checkpoint"] is not None and len(state["approved"]) == 1
                    and created and not created2 and n == 1,
            "evidence": {
                "checkpoint_state": state["checkpoint"]["state"],
                "approved_count": len(state["approved"]),
                "resume_note": state["note"],
                "first_settle_created": created,
                "second_settle_created": created2,
                "ledger_rows_for_order": n,
            },
            "note": "关掉 Store 再重开，检查点与批准状态都在，且不重复写入",
        }
    finally:
        store2.close()


CASES = [
    ("首次写入没有审批", case_first_write),
    ("审批前后参数发生变化", case_args_changed),
    ("提交成功但响应丢失", case_response_lost),
    ("拒绝与过期", case_reject_and_expire),
    ("暂停后进程重启", case_restart),
]


def _stable(obj):
    """把运行时地址（随机 id、临时端口）脱敏，让产物可逐字节重跑。

    为什么必须脱敏：本仓库的主张是「同配置重跑结果一致」。审批 id 是 uuid、
    回环端口是内核随机分配，只要它们进报告，两次运行的产物就不可能一样，
    而它们又不承载任何结论 —— 保留形状（`appr_<masked>`）比保留值有用。
    """
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stable(v) for v in obj]
    if isinstance(obj, str):
        s = re.sub(r"\b(appr|ckpt)_[0-9a-f]{6,}", r"\1_<masked>", obj)
        return re.sub(r"127\.0\.0\.1:\d{2,5}", "127.0.0.1:<port>", s)
    return obj


def _markdown(data):
    L = ["# 审批状态机与真实副作用闭环：5 个验收情形", "",
         "> 由 `scripts/real_settlement_demo.py` 生成；每个情形用独立临时库。",
         "> 通过 %d / %d" % (sum(1 for r in data["cases"] if r["pass"]),
                            len(data["cases"])), "",
         "> 审批 id 与回环端口是运行时地址，已脱敏为 `appr_<masked>` / "
         "`127.0.0.1:<port>`，目的是让本文件可逐字节重跑；脱敏不影响任何结论。",
         "", "## 结果", "",
         "| 情形 | 结果 | 说明 |", "|---|---|---|"]
    for r in data["cases"]:
        L.append("| %s | %s | %s |" % (r["name"], "通过" if r["pass"] else "未通过",
                                       r["note"]))
    L += ["", "## 逐情形证据", ""]
    for r in data["cases"]:
        L += ["### %s" % r["name"], "",
              "```json", json.dumps(r["evidence"], ensure_ascii=False, indent=2),
              "```", ""]
    L += ["## 边界（不粉饰）", "",
          "- 订单来自 127.0.0.1 回环测试服务，规则文件在项目沙箱内，"
          "结算只写专用 SQLite 表；不碰外部账号、真实支付、任意 shell 执行。",
          "- `sim` 保留作快速回归，本演示是另一条真实副作用链路。",
          "- 默认 `runtime.Ctx` 不挂 `approval_gate`，历史实验行为与冻结数字不变；"
          "真实任务显式挂上后才启用可恢复审批。",
          "- 这不是生产级审批工作流：没有身份认证、多租户、审计访问与限流。", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="审批与真实副作用闭环演示")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    args = ap.parse_args(argv)
    results = []
    with tempfile.TemporaryDirectory(prefix="settlement_") as tmp:
        for i, (name, fn) in enumerate(CASES, 1):
            d = os.path.join(tmp, "case_%d" % i)
            os.makedirs(d)
            r = fn(d)
            r["name"] = name
            results.append(r)
    data = _stable({"version": 1, "cases": results,
                    "passed": sum(1 for r in results if r["pass"]),
                    "total": len(results)})
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "demo.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    for r in results:
        print("  %-24s %s" % (r["name"], "通过" if r["pass"] else "未通过"))
    if data["passed"] != data["total"]:
        raise SystemExit("有情形未通过，见报告")
    return data


if __name__ == "__main__":
    main()
