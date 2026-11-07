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
        if s["kind"] in ("tool", "replay") and s["name"] == "order.get_order" and isinstance(s["output"], dict):
            items = s["output"].get("items")
            policy = s["output"].get("discount_policy")
        if s["kind"] in ("tool", "replay") and s["name"] == "pricing.calc_total" and isinstance(s["output"], dict):
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


# 通用流程阶段词典：**不依赖任何任务的真值**。
# 只表达「任何业务流程都该先启动、后收尾」这条常识，
# 不出现 PLAN_LIBRARY 里的具体步骤，也不读 expected_answer。
PHASE_OPEN = ("收集", "确认", "核对", "汇总", "准备", "冻结", "确定", "抽样", "创建", "设置")
PHASE_CLOSE = ("提交", "发布", "归档", "复盘", "退款", "验收", "跟踪", "审核", "考核", "审批")


def _inv_plan_order(steps, ctx):
    """流程首尾阶段不得颠倒。

    为什么这条能治 incorrect_planning 又不算作弊：
      - 故障注入把步骤整体 reverse()，首尾必然互换；
      - 正常流程不会「以收尾动作开头、以启动动作结尾」；
      - 规则只看动作词的通用语义，完全不看这题的标准步骤是什么。
    局限（如实记录）：纯执行型流程（如 onboarding 四步都是执行动作）
    首尾没有阶段信号，这条规则检测不到 —— 属于已知盲区，不假装全覆盖。
    """
    for s in steps:
        if s["kind"] == "tool" and s["name"] == "task.make_plan" and isinstance(s["output"], dict):
            plan = [str(x) for x in (s["output"].get("steps") or [])]
            if len(plan) < 2:
                continue
            first, last = plan[0], plan[-1]
            starts_closed = any(k in first for k in PHASE_CLOSE)
            ends_open = any(k in last for k in PHASE_OPEN)
            if starts_closed and ends_open:
                return Violation(
                    "INV-PLAN-ORDER", "流程首尾阶段颠倒（收尾在前、启动在后）",
                    s["seq"], {"first": first, "last": last},
                    "先启动后收尾", {"phase_open": PHASE_OPEN, "phase_close": PHASE_CLOSE})
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
    {"id": "INV-PLAN-ORDER", "desc": "流程首尾阶段不得颠倒",
     "applies": ["planner"], "check": _inv_plan_order},
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
