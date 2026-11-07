# -*- coding: utf-8 -*-
"""三个 Target Agent。

它们故意写得「老实」：不自己做交叉验算、不做防御性检查。
理由：真实 Agent 就是这样写的。如果 Agent 自己什么都能查出来，
诊断模块就没有存在意义了 —— 这是本平台的评测对象。

每个 agent 返回 dict：
    {"success": bool, "answer": ..., "expect": ..., "reason": "..."}
其中 success 只反映「拿到的数据是否够用」，**不反映答案是否正确**。
答案对不对由 evaluate 模块用真值判定 —— 这是防泄漏的关键分工。
"""
import sim


def order_agent(ctx):
    """订单结算：取订单 -> 建上下文 -> 计价 -> 结算（写） -> 报数。

    注意它把**上下文里的明细**传给计价工具，而不是重新查订单。
    这是真实写法，也是静默篡改能传播下去的原因。
    """
    oid = ctx.task["task_id"]

    ctx.model("规划：先取订单明细，再调用计价工具，最后结算", tokens=48)

    order = ctx.call("order.get_order", {"order_id": oid})
    if not isinstance(order, dict):
        raise ctx.ToolError("invalid_output", "订单返回不是对象")

    ctxo = ctx.context({"order_id": order.get("order_id"),
                        "items": order.get("items"),
                        "discount_policy": order.get("discount_policy")})

    items = ctxo.get("items")
    policy = ctxo.get("discount_policy")
    if not items or not isinstance(policy, dict):
        return {"success": False, "answer": None,
                "expect": sim.expected_total(oid),
                "reason": "上下文缺少明细或折扣策略"}

    total_out = ctx.call("pricing.calc_total", {"items": items,
                                                "discount_policy": policy})
    total = total_out.get("total") if isinstance(total_out, dict) else None
    if not isinstance(total, (int, float)):
        return {"success": False, "answer": None,
                "expect": sim.expected_total(oid), "reason": "没有拿到可用的总额"}

    ctx.call("order.settle", {"order_id": oid, "total": total})
    ctx.model("汇总：报告订单总额")
    ans = ctx.answer(
        "上下文：订单 %s，计价工具返回总额 %s。\n"
        "请输出 JSON：{\"order_id\": \"订单号\", \"total\": 数字}" % (oid, total),
        {"order_id": oid, "total": total})
    return {"success": True, "answer": ans,
            "expect": sim.expected_total(oid), "reason": ""}


def kb_agent(ctx):
    """知识检索：检索 -> 建上下文 -> 抽答案 -> 报告。"""
    q = ctx.task["task_id"]
    qinfo = sim.KB_QUESTIONS[q]

    ctx.model("规划：检索制度文档", tokens=44)

    res = ctx.call("kb.search", {"query": qinfo["query"]})
    docs = res.get("docs") if isinstance(res, dict) else None
    if not docs:
        return {"success": False, "answer": None,
                "expect": sim.expected_answer("kb", q), "reason": "检索没有返回证据"}

    ctxo = ctx.context({"query": qinfo["query"], "docs": docs})

    # 关键是读**上下文输出**，不是局部变量 docs。
    # 上下文管理器裁掉什么，模型就真的看不到什么 —— 否则 context_loss 故障是假的。
    visible = ctxo.get("docs") or []
    if not visible:
        return {"success": False, "answer": None,
                "expect": sim.expected_answer("kb", q), "reason": "上下文里没有证据"}

    text = sim.kb_answer_extract(visible)
    ctx.model("抽取：从证据里读出结论")
    ans = ctx.answer(
        "上下文证据：%s\n问题：%s\n"
        "请输出 JSON：{\"text\": 依据证据写出的结论原文, \"cited\": [文档号列表]}"
        % (text, q),
        {"text": text, "cited": [d.get("doc_id") for d in visible]})
    return {"success": bool(ans.get("text")), "answer": ans,
            "expect": sim.expected_answer("kb", q),
            "reason": "" if ans.get("text") else "证据里读不出结论"}


def planner_agent(ctx):
    """任务规划：调工具出计划 -> 建上下文 -> 报告步骤。"""
    key = ctx.task["task_id"]

    ctx.model("规划：为流程目标生成步骤", tokens=58)

    res = ctx.call("task.make_plan", {"goal_key": key})
    steps = res.get("steps") if isinstance(res, dict) else None
    if not steps:
        return {"success": False, "answer": None,
                "expect": sim.expected_answer("planner", key), "reason": "没有拿到计划步骤"}

    ctxo = ctx.context({"goal_key": key, "steps": steps})
    visible = ctxo.get("steps") or []
    if not visible:
        return {"success": False, "answer": None,
                "expect": sim.expected_answer("planner", key),
                "reason": "上下文里没有计划步骤"}

    ctx.model("整理：输出最终步骤清单")
    ans = ctx.answer(
        "上下文步骤：%s\n目标：%s\n"
        "请输出 JSON：{\"steps\": [\"步骤1\", \"步骤2\", ...]}"
        % (" / ".join(visible), key),
        {"steps": list(visible)})
    return {"success": True, "answer": ans,
            "expect": sim.expected_answer("planner", key), "reason": ""}


AGENTS = {
    "order": order_agent,
    "kb": kb_agent,
    "planner": planner_agent,
}


def _num(v):
    """模型经常把数字写成字符串（"1058.4"），判定时要能吃下这种差异。"""
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def _contains_bounded(haystack, needle):
    """带数字边界的包含判断。

    为什么需要：原来用朴素子串匹配，"2 张"会被"12 张"命中 ——
    于是把 2 改成 5、5 改成 12 之后，判定居然还是"答对了"。
    判分器自己漏判，比故障没注入更危险：它会让所有指标虚高。
    """
    if not haystack or not needle:
        return False
    i = 0
    while True:
        i = haystack.find(needle, i)
        if i < 0:
            return False
        before = haystack[i - 1] if i > 0 else ""
        after = haystack[i + len(needle)] if i + len(needle) < len(haystack) else ""
        if not before.isdigit() and not after.isdigit():
            return True
        i += 1


def is_correct(agent_type, answer, expect):
    """用真值判对错 —— 只有 evaluate 会调用它。"""
    if not isinstance(answer, dict) or expect is None:
        return False
    if agent_type == "order":
        t = _num(answer.get("total"))
        return t is not None and abs(t - expect["total"]) < 0.02
    if agent_type == "kb":
        return _contains_bounded(answer.get("text") or "", expect["must"])
    if agent_type == "planner":
        return list(answer.get("steps") or []) == expect["steps"]
    return False
