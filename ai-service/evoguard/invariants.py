# -*- coding: utf-8 -*-
"""业务不变量（Invariants）。

为什么单独一层：诊断规则回答"这一步看起来对不对"，不变量回答"这一步在业务上合不合法"。
前者靠 schema 和异常，后者靠业务约束。**静默篡改只能被后者抓住。**

设计借鉴 Microsoft AgentRx（MIT）的 invariants 思路：
把约束写成独立可测的声明式规则，而不是散落在诊断的 if 里。
本文件为自研实现，未复制其代码。

每条不变量：{id, desc, applies(agent_types), check(steps) -> Violation|None}
"""
from collections import Counter


class Violation:
    def __init__(self, rule_id, desc, step_seq, observed, expected, evidence):
        self.rule_id = rule_id
        self.desc = desc
        self.step_seq = step_seq
        self.observed = observed
        self.expected = expected
        self.evidence = evidence

    def as_dict(self):
        return {"rule_id": self.rule_id, "desc": self.desc, "step_seq": self.step_seq,
                "observed": self.observed, "expected": self.expected,
                "evidence": self.evidence}


def _steps_of(steps, kind=None, name=None):
    out = []
    for s in steps:
        if kind and s["kind"] != kind:
            continue
        if name and s["name"] != name:
            continue
        out.append(s)
    return out


# ---------- 规则实现 ----------
def _inv_order_total(steps, ctx):
    """订单总额必须等于「按明细重算」的结果。这是治静默篡改的核心不变量。"""
    items = policy = None
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "order.get_order" and isinstance(s["output"], dict):
            items = s["output"].get("items")
            policy = s["output"].get("discount_policy")
        if s["kind"] == "tool" and s["name"] == "pricing.calc_total" and isinstance(s["output"], dict):
            if not items or not isinstance(policy, dict):
                continue
            subtotal = sum(float(i["price"]) * int(i["qty"]) for i in items)
            expect = subtotal * (1 - float(policy["rate"])) if subtotal >= float(policy["threshold"]) else subtotal
            got = s["output"].get("total")
            if isinstance(got, (int, float)) and abs(got - round(expect, 2)) > 0.02:
                return Violation("INV-ORDER-TOTAL", "总额与明细重算不一致", s["seq"],
                                 got, round(expect, 2), {"items": items, "policy": policy})
    return None


def _inv_settle_matches_total(steps, ctx):
    """结算金额必须与计价结果一致，否则就是按错金额扣款。"""
    total = None
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "pricing.calc_total" and isinstance(s["output"], dict):
            total = s["output"].get("total")
        if s["kind"] == "tool" and s["name"] == "order.settle" and isinstance(s["output"], dict):
            settled = s["output"].get("total")
            if total is not None and settled != total:
                return Violation("INV-SETTLE-MATCH", "结算金额与计价结果不一致", s["seq"],
                                 settled, total, {"priced": total})
    return None


def _inv_no_duplicate_write(steps, ctx):
    """同一次运行里，同一个写操作最多执行一次。防重复扣款。"""
    writes = [s for s in _steps_of(steps, "tool", "order.settle")
              if isinstance(s["output"], dict) and s["output"].get("settled")]
    if len(writes) > 1:
        return Violation("INV-NO-DUP-WRITE", "写操作被执行了多次", writes[1]["seq"],
                         len(writes), 1, {"steps": [w["seq"] for w in writes]})
    return None


def _inv_kb_cited(steps, ctx):
    """答案必须引用检索到的文档；引用了没检索到的文档就是编造。"""
    retrieved = []
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "kb.search" and isinstance(s["output"], dict):
            retrieved = [d.get("doc_id") for d in (s["output"].get("docs") or [])]
    for s in steps:
        if s["kind"] == "model" and s["name"] == "answer" and isinstance(s["output"], dict):
            parsed = s["output"].get("parsed") or {}
            cited = parsed.get("cited") or []
            bad = [c for c in cited if c not in retrieved]
            if bad:
                return Violation("INV-KB-CITED", "引用了未检索到的文档", s["seq"],
                                 bad, retrieved, {})
    return None


def _inv_plan_steps_sane(steps, ctx):
    """计划步骤不得为空、不得重复。"""
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "task.make_plan" and isinstance(s["output"], dict):
            plan = s["output"].get("steps") or []
            if not plan:
                return Violation("INV-PLAN-EMPTY", "计划步骤为空", s["seq"], 0, ">0", {})
            dup = [k for k, v in Counter(plan).items() if v > 1]
            if dup:
                return Violation("INV-PLAN-DUP", "计划步骤有重复项", s["seq"], dup, "无重复", {})
    return None


INVARIANTS = [
    {"id": "INV-ORDER-TOTAL", "desc": "总额必须等于明细重算值",
     "applies": ["order"], "check": _inv_order_total},
    {"id": "INV-SETTLE-MATCH", "desc": "结算金额必须等于计价结果",
     "applies": ["order"], "check": _inv_settle_matches_total},
    {"id": "INV-NO-DUP-WRITE", "desc": "写操作一次运行最多执行一次",
     "applies": ["order", "kb", "planner"], "check": _inv_no_duplicate_write},
    {"id": "INV-KB-CITED", "desc": "只能引用检索到的文档",
     "applies": ["kb"], "check": _inv_kb_cited},
    {"id": "INV-PLAN-SANE", "desc": "计划步骤非空且不重复",
     "applies": ["planner"], "check": _inv_plan_steps_sane},
]


def check_all(steps, agent_type):
    """跑所有适用规则，返回违规列表（空列表 = 全部通过）。"""
    out = []
    for inv in INVARIANTS:
        if agent_type not in inv["applies"]:
            continue
        try:
            v = inv["check"](steps, {"agent_type": agent_type})
        except Exception as e:                     # 规则本身出错不能拖垮诊断
            v = Violation(inv["id"], "规则执行异常：%s" % e, 0, None, None, {})
        if v is not None:
            out.append(v.as_dict())
    return out
