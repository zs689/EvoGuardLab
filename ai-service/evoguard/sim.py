# -*- coding: utf-8 -*-
"""模拟业务环境：订单结算 / 知识检索 / 任务规划。

三条铁律：
  1. 所有工具都在内存里跑，不连任何真实系统（规格要求：仅隔离模拟环境）
  2. 每个任务的真值都能**独立算出**（不依赖工具返回），否则没法判静默篡改
  3. 工具输出结构固定，故障注入才能"合法但错"
"""
import copy
import json
import time

# ---------------- 模拟数据 ----------------
ORDERS = {
    "A1001": {"order_id": "A1001", "customer": "客户甲", "status": "待结算",
              "items": [{"sku": "P-1", "name": "打印纸", "price": 25.0, "qty": 8},
                        {"sku": "P-2", "name": "墨盒", "price": 180.0, "qty": 2}],
              "discount_policy": {"threshold": 500.0, "rate": 0.1}},
    "A1002": {"order_id": "A1002", "customer": "客户乙", "status": "待结算",
              "items": [{"sku": "P-3", "name": "显示器", "price": 899.0, "qty": 1}],
              "discount_policy": {"threshold": 1000.0, "rate": 0.05}},
    "A1003": {"order_id": "A1003", "customer": "客户丙", "status": "待结算",
              "items": [{"sku": "P-4", "name": "键盘", "price": 320.0, "qty": 3},
                        {"sku": "P-5", "name": "鼠标", "price": 99.0, "qty": 3}],
              "discount_policy": {"threshold": 1000.0, "rate": 0.08}},
}

KB = {
    "KB-01": {"doc_id": "KB-01", "title": "退货政策", "version": 2,
              "text": "自签收之日起 7 个工作日内可无理由退货，商品需保持完好。"},
    "KB-02": {"doc_id": "KB-02", "title": "退款时效", "version": 2,
              "text": "退货审核通过后 3 个工作日内原路退回，遇节假日顺延。"},
    "KB-03": {"doc_id": "KB-03", "title": "发票规则", "version": 2,
              "text": "电子发票在订单完成后 5 个工作日内开具，不支持修改抬头。"},
}

MIN_DOC_VERSION = 2      # 业务配置：低于此版本的证据不得用于作答

PLAN_LIBRARY = {
    "weekly_report": ["收集本周销售数据", "核对异常订单", "生成周报草稿", "提交审核"],
    "onboarding": ["创建账号", "分配设备", "安排导师", "设置首周目标"],
    "release": ["冻结代码", "跑回归测试", "生成变更说明", "灰度发布"],
}

# 检索题：query -> 期望答案里必须出现的关键片段（真值，工具不知道）
KB_QUESTIONS = {
    "退货要几天内": {"query": "退货时限", "must": "7 个工作日", "doc": "KB-01"},
    "退款多久到账": {"query": "退款时效", "must": "3 个工作日", "doc": "KB-02"},
    "发票什么时候开": {"query": "发票开具", "must": "5 个工作日", "doc": "KB-03"},
}

TASKS = {
    "order": [{"task_id": "A1001", "goal": "结算订单 A1001"},
              {"task_id": "A1002", "goal": "结算订单 A1002"},
              {"task_id": "A1003", "goal": "结算订单 A1003"}],
    "kb": [{"task_id": "退货要几天内", "goal": "退货要几天内"},
           {"task_id": "退款多久到账", "goal": "退款多久到账"},
           {"task_id": "发票什么时候开", "goal": "发票什么时候开"}],
    "planner": [{"task_id": "weekly_report", "goal": "制定周报流程"},
                {"task_id": "onboarding", "goal": "制定新人入职流程"},
                {"task_id": "release", "goal": "制定发布流程"}],
}

AGENT_TYPES = ["order", "kb", "planner"]

FAULT_TYPES = [
    "tool_timeout", "tool_exception", "invalid_json", "missing_field",
    "silent_corruption", "retrieval_failure", "context_loss", "process_interruption",
    # 以下两类是「规则表里没有对应策略」的故障 —— 进化的价值只能在这类故障上体现
    "stale_data", "incorrect_planning",
]

FAULT_DESC = {
    "tool_timeout": "工具超时",
    "tool_exception": "工具执行异常",
    "invalid_json": "返回结构错误",
    "missing_field": "关键字段缺失",
    "silent_corruption": "静默数据篡改（结构合法、值错误）",
    "retrieval_failure": "检索失败（取错对象）",
    "context_loss": "上下文关键信息被裁剪",
    "process_interruption": "执行中断",
    "stale_data": "数据陈旧（返回旧版本，结构合法）",
    "incorrect_planning": "计划顺序错误（步骤齐全但次序颠倒）",
}

# 不是所有故障对每个 Agent 都成立。
# silent_corruption 打在 KB/规划 Agent 上时，篡改的是「文档内容」——
# 单条轨迹里没有任何交叉证据可以核它，属于**已知不可检测边界**（见 README 已知限制）。
# 与其假装能测出来，不如把它排除在统计之外，并如实写在报告里。
CASE_FAULTS = {
    # silent_corruption 只对订单成立（KB/规划没有可交叉验算的数值）
    # stale_data 只对 KB 成立；incorrect_planning 只对规划成立
    "order": [f for f in FAULT_TYPES if f not in ("stale_data", "incorrect_planning")],
    "kb": [f for f in FAULT_TYPES if f not in ("silent_corruption", "incorrect_planning")],
    "planner": [f for f in FAULT_TYPES if f not in ("silent_corruption", "stale_data")],
}


# ---------------- 真值计算（不经过工具） ----------------
def expected_total(order_id):
    """直接从原始数据算，不信任 pricing 工具 —— 这是判定静默篡改的唯一依据。"""
    o = ORDERS[order_id]
    subtotal = sum(i["price"] * i["qty"] for i in o["items"])
    pol = o["discount_policy"]
    total = subtotal * (1 - pol["rate"]) if subtotal >= pol["threshold"] else subtotal
    return {"subtotal": round(subtotal, 2), "total": round(total, 2)}


def expected_answer(agent_type, task_id):
    if agent_type == "order":
        return expected_total(task_id)
    if agent_type == "kb":
        q = KB_QUESTIONS[task_id]
        return {"must": q["must"], "doc": q["doc"]}
    if agent_type == "planner":
        return {"steps": PLAN_LIBRARY[task_id]}
    raise ValueError(agent_type)


# ---------------- 工具实现 ----------------
def _tool_get_order(args):
    oid = args.get("order_id")
    if oid not in ORDERS:
        raise KeyError("订单不存在：%s" % oid)
    o = copy.deepcopy(ORDERS[oid])
    return {"order_id": o["order_id"], "items": o["items"],
            "discount_policy": o["discount_policy"], "status": o["status"]}


def _tool_calc_total(args):
    """按**调用方传进来的明细**算账。

    这一点很关键：如果这里偷偷去查原始订单表，上游明细被篡改就传不到结果，
    静默篡改类故障就永远注不进去。真实系统里计价服务收的就是上游传的明细。
    """
    items = args.get("items") or []
    policy = args.get("discount_policy") or {}
    if not items or not isinstance(policy, dict) or "rate" not in policy:
        raise ValueError("缺少明细或折扣策略")
    subtotal = sum(float(i["price"]) * int(i["qty"]) for i in items)
    total = subtotal * (1 - float(policy["rate"])) if subtotal >= float(policy["threshold"]) else subtotal
    return {"subtotal": round(subtotal, 2), "total": round(total, 2)}


def _tool_settle(args):
    """结算 = 写操作。恢复期重放它必须过审批，否则就是重复扣款。"""
    return {"order_id": args.get("order_id"), "settled": True,
            "total": args.get("total")}


def _tool_kb_search(args):
    q = args.get("query", "")
    hits = []
    for d in KB.values():
        if q and (q[:2] in d["text"] or q[:2] in d["title"]):
            hits.append(dict(d))
    if not hits:
        hits = [dict(KB["KB-01"])]
    return {"docs": hits[:2]}


def _tool_make_plan(args):
    goal = args.get("goal_key", "")
    if goal in PLAN_LIBRARY:
        return {"goal_key": goal, "steps": list(PLAN_LIBRARY[goal])}
    return {"goal_key": goal, "steps": ["收集信息", "确认目标", "执行", "复盘"]}


TOOLS = {
    "order.get_order": {"fn": _tool_get_order, "args": ["order_id"],
                        "out_required": ["order_id", "items", "discount_policy"]},
    "pricing.calc_total": {"fn": _tool_calc_total,
                           "args": ["items", "discount_policy"],
                           "out_required": ["total"]},
    "order.settle": {"fn": _tool_settle, "args": ["order_id", "total"],
                     "out_required": ["settled"], "side_effect": True},
    "kb.search": {"fn": _tool_kb_search, "args": ["query"],
                  "out_required": ["docs"]},
    "task.make_plan": {"fn": _tool_make_plan, "args": ["goal_key"],
                       "out_required": ["steps"]},
}


def auto_target(agent_type, fault_type):
    """故障自动落到哪一步。人工指定 fault_target 时以人指定的为准。

    为什么要有这张表：不同故障得打在不同的位置才有意义。
    比如 silent_corruption 必须打在计价步骤上，打在上游明细上会变成
    「链路内部自洽、外部无法发现」——那是另一个更难的课题，不适合当首版的靶子。
    """
    if fault_type == "context_loss":
        return 3                      # context 构建步
    if fault_type == "silent_corruption":
        return 4 if agent_type == "order" else 2
    return 2                          # 第一个工具调用步


def validate_args(tool, args):
    spec = TOOLS.get(tool)
    if not spec:
        return "未知工具：%s" % tool
    for a in spec["args"]:
        if a not in args:
            return "缺少参数 %s" % a
    return None


def call_tool(tool, args):
    """真实执行。抛异常就是工具异常，由 runtime 捕获并记步骤。"""
    return TOOLS[tool]["fn"](args)


def check_output(tool, output):
    """结构校验：返回 None 表示合规，否则返回问题描述。

    空容器也算不合规 —— "字段在但是空的"和"字段没了"对下游是同一个后果，
    而且真实系统里这个区别最容易被忽略。
    """
    if not isinstance(output, dict):
        return "输出不是对象"
    for f in TOOLS[tool]["out_required"]:
        if f not in output:
            return "缺少字段 %s" % f
        v = output[f]
        if isinstance(v, (list, dict, str)) and len(v) == 0:
            return "字段 %s 为空" % f
    return None


# ---------------- 故障变形 ----------------
def corrupt(fault_type, tool, output, args=None):
    """把合法输出变成「结构合法但有问题」的输出。只在被测工具边界发生。"""
    args = args or {}
    out = copy.deepcopy(output)
    if fault_type == "invalid_json":
        return '{"docs": [{"doc_id": "KB-01", '  # 故意坏掉的 JSON 文本
    if fault_type == "missing_field":
        if tool == "pricing.calc_total" and "total" in out:
            del out["total"]
        elif tool == "order.get_order" and "items" in out:
            del out["items"]
        elif tool == "kb.search":
            out["docs"] = []
        elif tool == "task.make_plan" and "steps" in out:
            del out["steps"]
        return out
    if fault_type == "stale_data":
        # 返回同一篇文档的旧版本：结构完全合法，只有数字过期
        if tool == "kb.search":
            for d in out.get("docs") or []:
                d["text"] = d.get("text", "").replace("7 个工作日", "15 个工作日") \
                                            .replace("3 个工作日", "10 个工作日") \
                                            .replace("5 个工作日", "20 个工作日")
        return out
    if fault_type == "incorrect_planning":
        # 步骤齐全但顺序颠倒 —— schema 完全合法，只能靠业务预期发现
        if tool == "task.make_plan" and out.get("steps"):
            out["steps"] = list(reversed(out["steps"]))
        return out
    if fault_type == "stale_data":
        # 返回同一篇文档的旧版本：结构完全合法，只有数字过期
        if tool == "kb.search":
            for d in out.get("docs") or []:
                d["version"] = 1          # 关键：版本号可观测，否则陈旧数据无法被诊断
                d["text"] = (d.get("text", "").replace("7 个工作日", "15 个工作日")
                             .replace("3 个工作日", "10 个工作日")
                             .replace("5 个工作日", "20 个工作日"))
        return out
    if fault_type == "incorrect_planning":
        # 步骤齐全但顺序颠倒 —— schema 完全合法，只能靠业务预期发现
        if tool == "task.make_plan" and out.get("steps"):
            out["steps"] = list(reversed(out["steps"]))
        return out
    if fault_type == "silent_corruption":
        # 数值被改，但结构完全合法 —— 只有独立验算才抓得到
        if tool == "pricing.calc_total" and "total" in out:
            out["total"] = round(out["total"] * 1.15 + 12.0, 2)
        elif tool == "order.get_order" and out.get("items"):
            out["items"][0]["price"] = round(out["items"][0]["price"] * 2, 2)
        return out
    if fault_type == "retrieval_failure":
        # 「取错对象」：请求的是 A，返回的是 B。
        # 结构完全合法，所以只能靠「入参和出参对不上」来发现 —— 三类 Agent 都适用。
        # 注意必须排除本来就正确的那一条，否则故障等于没注入（这条踩过坑）。
        if tool == "kb.search":
            got = {d.get("doc_id") for d in (out.get("docs") or [])}
            others = [d for k, d in KB.items() if d["doc_id"] not in got]
            if others:
                out["docs"] = [dict(others[0])]
        elif tool == "order.get_order":
            oid = out.get("order_id")
            other = next((k for k in ORDERS if k != oid), None)
            if other:
                out["order_id"] = other
                out["items"] = copy.deepcopy(ORDERS[other]["items"])
                out["discount_policy"] = copy.deepcopy(ORDERS[other]["discount_policy"])
        elif tool == "task.make_plan":
            key = out.get("goal_key")
            other = next((k for k in PLAN_LIBRARY if k != key), None)
            if other:
                out["goal_key"] = other
                out["steps"] = list(PLAN_LIBRARY[other])
        return out
    return out


def kb_answer_extract(docs):
    """从检索结果里抽答案（模拟 Agent 的"读文档"动作）。"""
    if not docs:
        return ""
    return " ".join(d.get("text", "") for d in docs)
