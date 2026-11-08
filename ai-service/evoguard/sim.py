# -*- coding: utf-8 -*-
"""模拟业务环境：订单结算 / 知识检索 / 任务规划。

三条铁律：
  1. 所有工具都在内存里跑，不连任何真实系统（规格要求：仅隔离模拟环境）
  2. 每个任务的真值都能**独立算出**（不依赖工具返回），否则没法判静默篡改
  3. 工具输出结构固定，故障注入才能"合法但错"
"""
import copy
import json
import re
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
    # 以下来自 TRAIL / MAST 分类法（见 docs/taxonomy.md），单 Agent 可复现的那部分
    "format_violation",      # TRAIL: Output Generation / Instruction Non-compliance
    "wrong_tool",            # TRAIL: Decision Making / Tool Selection Error
    "tool_hallucination",    # TRAIL: Hallucination / Tool-related
    "resource_abuse",        # TRAIL: Context Management / Resource Abuse
    "premature_termination", # MAST: Task Verification / Premature Termination
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
    "format_violation": "输出格式违规（要求 JSON，返回散文）",
    "wrong_tool": "工具选错（返回了另一个工具的结构）",
    "tool_hallucination": "工具输出幻觉（答案里的数字不存在于任何工具返回）",
    "resource_abuse": "资源滥用（同一工具同参数被重复调用）",
    "premature_termination": "过早终止（没拿到结果就宣布完成）",
}

# 不是所有故障对每个 Agent 都成立。
# silent_corruption 打在 KB/规划 Agent 上时，篡改的是「文档内容」——
# 单条轨迹里没有任何交叉证据可以核它，属于**已知不可检测边界**（见 README 已知限制）。
# 与其假装能测出来，不如把它排除在统计之外，并如实写在报告里。
CASE_FAULTS = {
    # silent_corruption 只对订单成立（KB/规划没有可交叉验算的数值）
    # stale_data 只对 KB 成立；incorrect_planning 只对规划成立
    # resource_abuse 是「成本型」故障：任务会成功，只是白花钱，不能用成功率衡量，
    # 所以不进成功率矩阵，单独用 steps/token 指标测（见 tests/test_taxonomy.py）。
    "order": [f for f in FAULT_TYPES
              if f not in ("stale_data", "incorrect_planning", "resource_abuse")],
    "kb": [f for f in FAULT_TYPES
           if f not in ("silent_corruption", "incorrect_planning", "wrong_tool", "resource_abuse")],
    "planner": [f for f in FAULT_TYPES
                if f not in ("silent_corruption", "stale_data", "wrong_tool", "resource_abuse")],
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
    if agent_type == "calc":
        return expected_hard_total(task_id)
    if agent_type == "xref":
        return HARD_KB_ANSWER[task_id]
    raise ValueError(agent_type)


# ---------------- 工具实现 ----------------
def _tool_get_quote_order(args):
    """难度组（calc）专用的取单工具。

    为什么单独开一个而不是复用 order.get_order：
    那个工具的契约（out_required）要求 discount_policy，而难度组用的是
    rebate + tier 两段规则。塞进去会让每一次调用都被判成「缺字段」，
    等于每跑一次就自带一个假故障 —— 实验数据立刻失真。
    """
    oid = args.get("order_id")
    if oid not in HARD_ORDERS:
        raise KeyError("订单不存在：%s" % oid)
    o = copy.deepcopy(HARD_ORDERS[oid])
    return {"order_id": o["order_id"], "items": o["items"],
            "rebate": o["rebate"], "tier": o["tier"], "status": "待结算"}


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


# 各 Agent 的答案步逻辑序号（1 plan / 2 tool / 3 context / 4 tool / 5 settle / 6 plan / 7 answer）
ANSWER_STEP = {"order": 7, "kb": 5, "planner": 5}


def auto_target(agent_type, fault_type):
    """故障自动落到哪一步。人工指定 fault_target 时以人指定的为准。

    为什么要有这张表：不同故障得打在不同的位置才有意义。
    比如 silent_corruption 必须打在计价步骤上，打在上游明细上会变成
    「链路内部自洽、外部无法发现」——那是另一个更难的课题，不适合当首版的靶子。
    """
    if agent_type in ("calc", "xref"):
        return hard_target(agent_type, fault_type)
    if fault_type == "context_loss":
        return 3                      # context 构建步
    if fault_type == "silent_corruption":
        return 4 if agent_type == "order" else 2
    if fault_type in ("format_violation", "tool_hallucination", "premature_termination"):
        # 这三类是「答案那一层」的故障，必须打到最后一步，打在第 2 步等于没注入
        return ANSWER_STEP.get(agent_type, 5)
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
        elif tool == "pricing.quote" and "tier_rate" in out:
            # 少了会员折扣率 → 模型只能按原价算，答案是错的但结构合法
            del out["tier_rate"]
        elif tool == "kb.search_all":
            out["docs"] = []
        elif tool == "kb.search":
            out["docs"] = []
        elif tool == "task.make_plan" and "steps" in out:
            del out["steps"]
        return out
    if fault_type == "stale_data":
        # 返回同一篇文档的旧版本：结构合法，只有版本号和数字过期。
        # 用「所有数字 +3」做通用变形 —— 写死短语替换会漏掉新加的文档，
        # 而且链式 replace 会把 7→15 的 15 再被 5→20 命中，改成 120。
        if tool == "kb.search":
            def bump(m):
                return str(int(m.group(0)) + 3)
            for d in out.get("docs") or []:
                d["version"] = 1
                d["text"] = re.sub(r"\d+", bump, d.get("text", ""))
        elif tool == "kb.search_all":
            # 只给最低版本的那份：模型不做版本比较就会照着旧条款回答
            docs = out.get("docs") or []
            if docs:
                out["docs"] = [dict(min(docs, key=lambda d: d["version"]))]
        return out
    if fault_type == "wrong_tool":
        # 选错了工具：返回的是 kb.search 的结构。字段名全对、JSON 合法，
        # 但和当前工具的契约不匹配 —— 只有做「工具契约校验」才看得出来。
        if tool == "order.get_order":
            return {"docs": [dict(KB["KB-01"])]}
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
        elif tool == "pricing.quote" and "tier_rate" in out:
            # 会员折扣率被悄悄改小：小计是真的，只有这一项错。
            # 这是最隐蔽的形态 —— 模型按它算，算术全对，结果全错。
            out["tier_rate"] = round(max(0.1, float(out["tier_rate"]) * 0.7), 2)
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
        elif tool == "kb.search_all":
            # 取错主题：文档结构完全合法，只有主题对不上
            topic = out.get("topic")
            other = next((k for k in HARD_KB if k != topic), None)
            if other:
                out["topic"] = other
                out["docs"] = [dict(d) for d in HARD_KB[other]]
        return out
    return out


def kb_answer_extract(docs):
    """从检索结果里抽答案（模拟 Agent 的"读文档"动作）。"""
    if not docs:
        return ""
    return " ".join(d.get("text", "") for d in docs)


# ==================== 数据集扩充（目标 >=180 个可复现实例） ====================
# 为什么要扩：48 个样本上的「提升 X 个百分点」在统计上毫无意义。
# 扩充方式保持「真值可独立计算」，不引入任何人工标注。

_EXTRA_ORDERS = [
    ([("P-7", 66.0, 4), ("P-8", 210.0, 1)], {"threshold": 400.0, "rate": 0.06}),
    ([("P-9", 15.0, 20)], {"threshold": 300.0, "rate": 0.03}),
    ([("P-10", 1280.0, 2)], {"threshold": 2000.0, "rate": 0.12}),
    ([("P-11", 88.0, 6), ("P-12", 9.5, 10)], {"threshold": 500.0, "rate": 0.07}),
    ([("P-13", 520.0, 3)], {"threshold": 1500.0, "rate": 0.09}),
]
for _i, (_items, _pol) in enumerate(_EXTRA_ORDERS, start=4):
    _oid = "A100%d" % _i
    ORDERS[_oid] = {"order_id": _oid, "customer": "客户%d" % _i, "status": "待结算",
                    "items": [{"sku": s_, "name": "商品" + s_, "price": p_, "qty": q_}
                              for s_, p_, q_ in _items],
                    "discount_policy": _pol}
    TASKS["order"].append({"task_id": _oid, "goal": "结算订单 %s" % _oid})

_EXTRA_KB = [
    ("KB-04", "保修政策", "整机保修 12 个月，主要部件保修 24 个月。", "保修多久", "12 个月"),
    ("KB-05", "包邮规则", "单笔订单满 99 元包邮，偏远地区加收 8 元。", "包邮门槛", "99 元"),
    ("KB-06", "专票申请", "默认开具电子普通发票，专票需在 3 个工作日内申请。", "专票怎么开", "3 个工作日"),
    ("KB-07", "换货条件", "收到货 15 天内可换货，需保留原包装。", "换货期限", "15 天"),
    ("KB-08", "会员权益", "银卡会员每月 2 张免运费券，金卡每月 5 张。", "免运费券几张", "2 张"),
]
for _doc, _title, _text, _q, _must in _EXTRA_KB:
    KB[_doc] = {"doc_id": _doc, "title": _title, "text": _text, "version": 2}
    KB_QUESTIONS[_q] = {"query": _q, "must": _must, "doc": _doc}
    TASKS["kb"].append({"task_id": _q, "goal": _q})

_EXTRA_PLANS = [
    ("refund", ["核对订单", "确认退货原因", "生成退货单", "财务退款"]),
    ("procurement", ["汇总需求", "三家比价", "提交审批", "下单验收"]),
    ("incident", ["确认故障范围", "止血", "定位根因", "复盘归档"]),
    ("audit", ["准备资料", "抽样检查", "出具报告", "整改跟踪"]),
    ("training", ["确定课表", "邀请讲师", "组织考核", "归档成绩"]),
]
for _key, _steps in _EXTRA_PLANS:
    PLAN_LIBRARY[_key] = _steps
    TASKS["planner"].append({"task_id": _key, "goal": "制定%s流程" % _key})

# dev / test 划分：按任务切，**同一个任务模板绝不跨集合**，避免样本泄漏
DEV_TASKS_PER_AGENT = 2


def dev_tasks(agent_type):
    return TASKS[agent_type][:DEV_TASKS_PER_AGENT]


def test_tasks(agent_type):
    return TASKS[agent_type][DEV_TASKS_PER_AGENT:]


def split_of(agent_type, task_id):
    for t in dev_tasks(agent_type):
        if t["task_id"] == task_id:
            return "dev"
    return "test"


# ==================== 复合故障：策略链的真实靶场 ====================
# 为什么要单独一组：单故障用例里「诊断一次 -> 一个策略」就够了，
# 策略链和单策略在数字上没有区别，组合就成了装饰。
# 复合故障把两个故障放在**不同的逻辑步**：第一次只暴露第一个坏点，
# 处理完重放，第二个坏点才出现 —— 只有多轮策略链能逐个拔掉。
# 每条都标注「单策略理论上限」，评测时用来验证增量是不是真的来自组合。
# 选材原则（踩过坑）：两个故障都必须是**持久**故障，且各自需要不同的修复策略。
# 用过 tool_timeout 做第一个故障，结果恢复轮 attempt>=2 时它自动消失，
# 单策略 cross_verify 就把剩下的篡改也修了 —— 组合策略毫无增量，实验是假的。
COMPOSITES = [
    # (agent, 故障A@步, 故障B@步, 说明)
    ("order", {"type": "missing_field", "target": 2},
     {"type": "silent_corruption", "target": 4},
     "字段缺失(需 fallback_tool) + 金额篡改(需 cross_verify)"),
    ("order", {"type": "wrong_tool", "target": 2},
     {"type": "format_violation", "target": 7},
     "工具契约错配(需 fallback_tool) + 答案格式违规(需 reformat)"),
    ("kb", {"type": "retrieval_failure", "target": 2},
     {"type": "tool_hallucination", "target": 5},
     "取错文档 + 答案数字幻觉：两个坏点分处工具层与答案层"),
    ("kb", {"type": "stale_data", "target": 2},
     {"type": "context_loss", "target": 3},
     "陈旧证据 + 上下文裁剪"),
    ("planner", {"type": "incorrect_planning", "target": 2},
     {"type": "premature_termination", "target": 5},
     "计划顺序颠倒 + 过早收尾"),
]


def composite_cases(split="test"):
    """构造复合故障用例。split 只按任务切，和单故障数据集用同一套划分。"""
    pool = {"dev": dev_tasks, "test": test_tasks}[split]
    out = []
    for agent, fa, fb, desc in COMPOSITES:
        for t in pool(agent):
            out.append({
                "agent_type": agent,
                "task_id": t["task_id"],
                "fault_type": "%s+%s" % (fa["type"], fb["type"]),
                "fault_chain": [dict(fa), dict(fb)],
                "desc": desc,
                "seed": 7,
            })
    return out


def dataset_stats():
    n = 0
    for at in AGENT_TYPES:
        n += len(TASKS[at]) * len(CASE_FAULTS[at])
    # 每个 Agent 的真实用例数：前端直接展示，不许用「总数/3」估算糊弄
    per_agent = {}
    for at in AGENT_TYPES:
        per_agent[at] = {
            "tasks": len(TASKS[at]),
            "faults": len(CASE_FAULTS[at]),
            "dev_cases": len(dev_tasks(at)) * len(CASE_FAULTS[at]),
            "test_cases": len(test_tasks(at)) * len(CASE_FAULTS[at]),
        }
    return {"tasks": {at: len(TASKS[at]) for at in AGENT_TYPES},
            "cases": n,
            "dev_cases": sum(v["dev_cases"] for v in per_agent.values()),
            "test_cases": sum(v["test_cases"] for v in per_agent.values()),
            "per_agent": per_agent,
            "composite_patterns": len(COMPOSITES),
            "composite_test_cases": len(composite_cases("test")),
            "hard": hard_stats()}


# ==================== 难度升级：多步计算 / 跨文档冲突 ====================
# 为什么必须单独一组，而不是把现有 204 例改难：
#   1. 改了就没法和历史数字比，前面几轮的结论全部作废；
#   2. 现有任务是**抽取式**的 —— 工具已经把答案算好，模型只要搬运，
#      所以 936 次调用 0 次答错。这测的是恢复机制，不是模型。
# 这里把工具从「给答案」改成「给原料」，模型必须自己算/自己比，才有答错的空间。
#
# 两类任务：
#   calc —— 多步计算：两段计价规则有**先后顺序**（先满减、后会员折扣），
#           工具只返回小计与规则，**不返回总额**。
#   xref —— 跨文档冲突：同一主题有多份不同版本的文档，必须按「版本高者优先」
#           挑出生效条款，只读一份就会答成别的数字。

TIER_RATE = {"none": 1.0, "silver": 0.95, "gold": 0.9}

HARD_ORDERS = {
    "H3001": {"order_id": "H3001", "tier": "silver",
              "items": [{"sku": "H-1", "name": "服务器", "price": 1580.0, "qty": 2},
                        {"sku": "H-2", "name": "交换机", "price": 860.0, "qty": 1}],
              "rebate": {"threshold": 1000.0, "amount": 100.0}},
    "H3002": {"order_id": "H3002", "tier": "gold",
              "items": [{"sku": "H-3", "name": "工控机", "price": 2400.0, "qty": 1},
                        {"sku": "H-4", "name": "线材", "price": 45.5, "qty": 10}],
              "rebate": {"threshold": 2000.0, "amount": 200.0}},
    "H3003": {"order_id": "H3003", "tier": "none",
              "items": [{"sku": "H-5", "name": "显示器", "price": 620.0, "qty": 1},
                        {"sku": "H-6", "name": "支架", "price": 130.0, "qty": 1}],
              "rebate": {"threshold": 1000.0, "amount": 100.0}},
}

HARD_KB = {
    "退货期限": [
        {"doc_id": "KB-21", "title": "退货政策", "version": 2,
         "text": "自签收之日起 7 个工作日内可无理由退货。"},
        {"doc_id": "KB-22", "title": "退货政策（修订）", "version": 4,
         "text": "自签收之日起 10 个工作日内可无理由退货，需保留吊牌。"},
        {"doc_id": "KB-23", "title": "退货政策（旧版）", "version": 1,
         "text": "自签收之日起 15 个工作日内可无理由退货。"},
    ],
    "保修期限": [
        {"doc_id": "KB-24", "title": "保修政策", "version": 3,
         "text": "整机保修 12 个月。"},
        {"doc_id": "KB-25", "title": "保修政策（修订）", "version": 5,
         "text": "整机保修 24 个月，电池保修 12 个月。"},
        {"doc_id": "KB-26", "title": "保修政策（旧版）", "version": 1,
         "text": "整机保修 6 个月。"},
    ],
    "发票时限": [
        {"doc_id": "KB-27", "title": "发票规则", "version": 2,
         "text": "电子发票在订单完成后 5 个工作日内开具。"},
        {"doc_id": "KB-28", "title": "发票规则（修订）", "version": 6,
         "text": "电子发票在订单完成后 1 个工作日内开具，专票需 3 个工作日。"},
        {"doc_id": "KB-29", "title": "发票规则（旧版）", "version": 1,
         "text": "电子发票在订单完成后 10 个工作日内开具。"},
    ],
}

# 真值：只看 version 最高的那份（工具不知道这条规则，模型必须自己比）
HARD_KB_ANSWER = {}
for _topic, _docs in HARD_KB.items():
    _top = max(_docs, key=lambda d: d["version"])
    _m = re.search(r"(\d+)\s*个?(?:工作日|天|个月)", _top["text"])
    HARD_KB_ANSWER[_topic] = {"must": _m.group(0) if _m else "", "doc": _top["doc_id"]}

HARD_AGENTS = ["calc", "xref"]


def hard_tasks(agent_type):
    if agent_type == "calc":
        return [{"task_id": k, "goal": "按规则算出订单 %s 的应付总额" % k}
                for k in HARD_ORDERS]
    return [{"task_id": k, "goal": "按生效版本回答：%s" % k} for k in HARD_KB]


def expected_hard_total(order_id):
    """独立算：先满减、再会员折扣。顺序不能反 —— 反了结果就不一样。"""
    o = HARD_ORDERS[order_id]
    subtotal = sum(i["price"] * i["qty"] for i in o["items"])
    after = subtotal - o["rebate"]["amount"] if subtotal >= o["rebate"]["threshold"] else subtotal
    total = after * TIER_RATE[o["tier"]]
    return {"subtotal": round(subtotal, 2),
            "after_rebate": round(after, 2),
            "total": round(total, 2)}


def _tool_quote(args):
    """只给原料：小计 + 规则，**不给总额**。

    这是难度的来源：模型必须自己按「先满减后折扣」的顺序算。
    以前的 pricing.calc_total 直接把 total 算好，模型抄一下就对 ——
    那样测出来的 100% 只能说明它会搬运，不能说明它会计。
    """
    items = args.get("items") or []
    rebate = args.get("rebate") or {}
    tier = args.get("tier") or "none"
    if not items or not isinstance(rebate, dict):
        raise ValueError("缺少明细或满减规则")
    subtotal = sum(float(i["price"]) * int(i["qty"]) for i in items)
    return {"subtotal": round(subtotal, 2),
            "rebate": {"threshold": float(rebate.get("threshold", 0)),
                       "amount": float(rebate.get("amount", 0))},
            "tier": tier,
            "tier_rate": TIER_RATE.get(tier, 1.0),
            "rule_order": "先满减，再乘会员折扣"}


def _tool_quote_calc(args):
    """旧接口，保留给 calc agent 显式要「只算小计」的场景。"""
    items = args.get("items") or []
    return {"subtotal": round(sum(float(i["price"]) * int(i["qty"]) for i in items), 2)}


def _tool_apply_rules(args):
    """按**调用方给出的顺序**套用规则。

    这是难度的落点：算术不让模型做（真实 Agent 就该调计算器），
    但「先满减、还是先打折」必须由模型决定 —— 顺序不同结果不同，
    而且满减门槛是按**当前金额**判断的，先打折可能导致门槛不达标。

    两种规则：
      rebate   —— 满 threshold 减 amount（按当前金额判断，不达标就不减）
      discount —— 当前金额 × 折扣率
    """
    subtotal = float(args.get("subtotal"))
    rebate = args.get("rebate") or {}
    rate = args.get("tier_rate")
    order = args.get("order") or []
    if not order:
        raise ValueError("缺少规则执行顺序 order")
    cur = subtotal
    applied = []
    for name in order:
        if name == "rebate":
            th = float(rebate.get("threshold", 0))
            am = float(rebate.get("amount", 0))
            if cur >= th:
                cur -= am
                applied.append("rebate")
        elif name == "discount":
            if rate is None:
                raise ValueError("缺少折扣率")
            cur *= float(rate)
            applied.append("discount")
        else:
            raise ValueError("未知规则：%s" % name)
    return {"total": round(cur, 2), "applied": applied, "order": list(order)}


def _tool_kb_search_all(args):
    """按主题返回**全部**历史版本，让模型自己比 version。

    不能在这里挑最高版本 —— 那就是把答案算好了。
    """
    topic = args.get("topic", "")
    docs = HARD_KB.get(topic)
    if not docs:
        raise KeyError("没有该主题的文档：%s" % topic)
    return {"topic": topic, "docs": [dict(d) for d in docs]}


def _tool_tier_lookup(args):
    return {"customer_id": args.get("customer_id"),
            "tier": HARD_ORDERS.get(args.get("order_id"), {}).get("tier", "none")}


TOOLS.update({
    "order.get_quote_order": {"fn": _tool_get_quote_order, "args": ["order_id"],
                              "out_required": ["order_id", "items", "rebate", "tier"]},
    "pricing.quote": {"fn": _tool_quote, "args": ["items", "rebate", "tier"],
                      "out_required": ["subtotal", "tier_rate"]},
    "calc.apply_rules": {"fn": _tool_apply_rules,
                         "args": ["subtotal", "rebate", "tier_rate", "order"],
                         "out_required": ["total"]},
    "pricing.subtotal": {"fn": _tool_quote_calc, "args": ["items"],
                         "out_required": ["subtotal"]},
    "kb.search_all": {"fn": _tool_kb_search_all, "args": ["topic"],
                      "out_required": ["docs"]},
    "crm.tier": {"fn": _tool_tier_lookup, "args": ["order_id", "customer_id"],
                 "out_required": ["tier"]},
})

ANSWER_STEP.update({"calc": 7, "xref": 5})


def hard_cases():
    """hard suite 只用于评测，不进训练集（训练集保持 dev 划分不变）。

    每个任务额外带一条 `fault_type="none"` 的对照 —— 那是**纯能力基线**：
    没有任何故障时模型能不能算对。这个数字最重要：如果基线就不是 100%，
    说明题目确实难住了模型，而不是恢复机制出了问题。

    为什么这组**不测 format_violation**：
    该故障的语义是「内容本来是对的，只是输出成了散文」，注入方式是拿 fallback
    答案包一层散文。而这一组模型的职责是"自己作决策"，agent 手里没有正确内容，
    fallback 只能是空结构 → reformat 恢复出来仍是空 → 必然失败。
    测一个语义不成立的故障，只会得到一个看着像机制缺陷的假结论。
    答案层改用 premature_termination（没拿到结果就宣布完成），语义成立。
    """
    faults = {
        "calc": ["tool_timeout", "tool_exception", "missing_field",
                 "silent_corruption", "premature_termination"],
        "xref": ["tool_timeout", "retrieval_failure", "stale_data",
                 "missing_field", "premature_termination"],
    }
    out = []
    for agent, fts in faults.items():
        for t in hard_tasks(agent):
            out.append({"agent_type": agent, "task_id": t["task_id"],
                        "fault_type": "none", "seed": 7})
            for ft in fts:
                out.append({"agent_type": agent, "task_id": t["task_id"],
                            "fault_type": ft, "seed": 7})
    return out


def hard_target(agent_type, fault_type):
    """hard 任务的故障落点：calc 要在计价步打，xref 要在检索步打。"""
    if fault_type == "silent_corruption":
        return 4                      # pricing.quote 那一步
    if fault_type in ("format_violation", "premature_termination"):
        return ANSWER_STEP[agent_type]
    if fault_type == "context_loss":
        return 3
    return 2


def hard_stats():
    c = hard_cases()
    return {"cases": len(c),
            "per_agent": {a: sum(1 for x in c if x["agent_type"] == a) for a in HARD_AGENTS}}
