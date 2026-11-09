# -*- coding: utf-8 -*-
"""受控真实任务：本地 HTTP 订单 + 沙箱规则文件 + 专用 SQLite 结算（§6 S05）。

刻意保持在本地回环与项目沙箱内：
  - 订单通过 **本机** 127.0.0.1 的测试 HTTP 服务读取，不碰外部账号或服务；
  - 规则文件只读项目内的 `tests/fixtures/settlement_rules.json`；
  - 结算只写专用 SQLite 表（`settlement_ledger`），不碰真实支付、不开 shell；
  - `sim` 保留作快速回归，本模块是另一条真实副作用链路。

「提交成功但响应丢失」用 `lose_response=True` 模拟：账本已写入后抛
`ResponseLost`，调用方拿**同一个幂等键**重试，`SettlementLedger.settle()` 的
`INSERT OR IGNORE` 保证仍只有一笔。
"""
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approval  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RULES_PATH = os.path.join(ROOT, "tests", "fixtures", "settlement_rules.json")


class ResponseLost(RuntimeError):
    """结算已提交，但响应在返回途中丢失 —— 用来验幂等重试。

    带上已提交的结果，调用方才能拿**同一个幂等键**查询或重试。
    """

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


class _OrderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        prefix = "/order/"
        if not self.path.startswith(prefix):
            self.send_error(404, "not found")
            return
        oid = self.path[len(prefix):]
        order = self.server.orders.get(oid)
        if order is None:
            self.send_error(404, "unknown order")
            return
        body = json.dumps(order, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass                      # 演示不往终端刷访问日志


class LocalOrderService:
    """127.0.0.1 随机端口的测试订单服务，用完必须 stop()。"""

    def __init__(self, orders):
        self.orders = orders
        self.server = None
        self.thread = None

    def start(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OrderHandler)
        self.server.orders = self.orders
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def fetch_order(self, order_id):
        # 显式绕过系统代理，避免本机请求被代理劫持。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.base_url + "/order/" + order_id, timeout=5) as r:
            if r.status != 200:
                raise RuntimeError("订单服务返回 %s" % r.status)
            return json.loads(r.read().decode("utf-8"))

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()


def load_rules(path=RULES_PATH):
    """规则文件必须在项目沙箱内，路径越界直接拒绝。"""
    p = os.path.abspath(path)
    root = os.path.abspath(ROOT)
    if not p.startswith(root + os.sep):
        raise ValueError("规则文件不在项目沙箱内：%s" % p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def compute_total(order, rules):
    """按规则算总额；返回 (total, detail)。规则版本一并留痕。"""
    items = order.get("items") or []
    if not items:
        raise ValueError("订单没有明细，无法计算")
    subtotal = sum(float(i["price"]) * int(i["qty"]) for i in items)
    threshold = float(rules["discount_threshold"])
    rate = float(rules["discount_rate"])
    total = subtotal * (1 - rate) if subtotal >= threshold else subtotal
    total = round(total, 2)
    if total > float(rules["max_total"]):
        raise ValueError("总额 %s 超过规则上限 %s" % (total, rules["max_total"]))
    prefix = rules.get("allowed_order_prefix")
    if prefix and not str(order.get("order_id", "")).startswith(prefix):
        raise ValueError("订单号不在允许范围内")
    return total, {"subtotal": round(subtotal, 2), "threshold": threshold,
                   "rate": rate, "total": total,
                   "rule_version": rules["version"]}


def run_settlement(*, store, gate, ledger, service, run_id, order_id,
                   rules_path=RULES_PATH, allowed_tools=None,
                   lose_response=False, first_write=False,
                   checkpoint_id=None):
    """端到端：HTTP 读订单 → 读规则 → 算总额 → 过审批门禁 → 幂等落库。

    返回 dict，包含每一步的可核对证据；审批未通过时**绝不落库**。
    """
    order = service.fetch_order(order_id)
    rules = load_rules(rules_path)
    total, detail = compute_total(order, rules)
    args = {"order_id": order_id, "total": total}

    result = {
        "run_id": run_id, "order_id": order_id,
        "http_source": service.base_url + "/order/" + order_id,
        "rules_path": os.path.relpath(os.path.abspath(rules_path), ROOT),
        "rule_version": rules["version"], "compute": detail,
        "args": args, "settled": False, "approval": None,
    }

    # 唯一的写操作门禁：首次写入与重放写入都走这里。
    try:
        decision = gate.require(
            run_id, "order.settle", args,
            allowed_tools=allowed_tools or ["order.settle"],
            tool_contract={"order.settle": {"side_effect": True}},
            checkpoint_id=checkpoint_id, first_write=first_write,
            idempotency_key=approval.digest(
                {"run_id": run_id, "order_id": order_id, "total": total}))
    except approval.ApprovalRequired as e:
        result["approval"] = {"status": "pending", "request_id": e.request_id,
                              "checkpoint_id": e.checkpoint_id,
                              "reason": str(e)}
        result["reason"] = "暂停等待审批，未产生任何落库副作用"
        return result
    except approval.ApprovalDenied as e:
        result["approval"] = {"status": "denied", "request_id": e.request_id,
                              "reason": e.reason}
        result["reason"] = "审批不可复用，未产生任何落库副作用"
        return result

    result["approval"] = {"status": decision["status"],
                          "request_id": decision["request_id"]}
    key = decision["idempotency_key"]
    created, row = ledger.settle(
        key, run_id=run_id, order_id=order_id, total=total,
        rule_version=rules["version"],
        payload={"compute": detail, "source": "local_http",
                 "rules": os.path.basename(rules_path)})
    result["settled"] = True
    result["idempotency_key"] = key
    result["created_this_call"] = created
    result["ledger_rows_for_order"] = ledger.count(order_id)
    result["ledger_row"] = row

    if lose_response and created:
        # 账本已落库，但响应丢了。调用方拿同一个 key 重试，仍只有一笔。
        raise ResponseLost("结算 %s 已提交但响应丢失；用同一幂等键重试" % key,
                           result)
    return result
