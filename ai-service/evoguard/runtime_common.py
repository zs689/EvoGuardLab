# -*- coding: utf-8 -*-
"""ARC-DEEP-01-T2：runtime / 诊断模块共用的常量、异常与小工具。

同一对象共享是拆分的前提：`except ToolError` 只有在两边拿到**同一个类**
时才成立，各自定义一份会让异常静默漏接。
"""

import os

MAX_STEPS = 20
MAX_RETRY = 2
MAX_CHAIN = 3              # 一条策略链最多几步，防止无限恢复
TOKEN_BUDGET = 8000
WRITE_TOOLS = {"order.settle"}
AUTO_APPROVE = os.environ.get("AGENTLAB_AUTO_APPROVE", "1") == "1"
SIDE_EFFECT_COUNT = {"n": 0}

# 瞬时故障：重试一次就过去了。持久故障：同样的调用再发一次，结果还是错的。
# 这个区分是整个 A/B/C 对照能不能成立的关键 ——
# 如果所有故障都能靠重试解决，那"诊断"就没有存在价值，实验也就白做了。
TRANSIENT_FAULTS = {"tool_timeout", "tool_exception", "process_interruption"}

# 仅离线评测使用：强制某个候选技能参与恢复（晋升前单独测它）
FORCED_SKILL = None

# 仅离线评测使用：按模式指定技能库。
# 为什么需要它：C 和 D 的差别本来只在「验证严不严」，但如果两边共用同一个库，
# 这个变量根本没被激活 —— 库是严格验证出来的，C 用不用严格验证都一样。
# 要测出治理价值，C 必须用**自己**那套放宽流程训出来的库（里面全是没验证过的经验）。
# 生产运行时不设置它，行为与以前完全一致（就用调用方传进来的 store）。
STORE_BY_MODE = {}

# 策略真实语义：哪些策略只是「重放」，哪些会**修复输出**。
# 这一层是关键 —— 如果所有策略都只靠 suppress 把故障点绕过去，
# 那"策略"就只是个名字，组合起来也不会有任何增量。
# replan 故意不在里面：计划顺序的正确性由「流程不变量」诊断，
# 修复靠抑制故障点后重放（工具返回自然就是干净的），
# 而不是拿一张动词词典去猜标准答案 —— 那等于把真值塞进恢复逻辑，是自欺。
REPAIR_STRATEGIES = {"cross_verify", "fallback_tool", "reformat"}

# 每个修复式策略真正能兜住的「诊断根因」。
# 只有根因落在覆盖范围内，才让故障照常注入、由策略修好；
# 否则沿用旧的抑制重放，保证单策略链的基线下限不被打破。
# 注意判定依据是**诊断结果**（只看轨迹），不是故障真值 —— 没有评测泄漏。
REPAIR_COVERAGE = {
    "cross_verify": {"silent_corruption"},
    "fallback_tool": {"missing_field", "invalid_json", "wrong_tool",
                      "retrieval_failure", "stale_data", "incorrect_planning"},
    "reformat": {"format_violation"},
}


def _has_module(name):
    """检查器是否真的可导入。不能只看配置里写没写名字 —— 那只是字符串。"""
    try:
        __import__(name)
        return True
    except Exception:                     # noqa: BLE001
        return False


def _fault_specs(run_exp):
    """把故障描述统一成 [{type,target}, ...]。

    exp 里可能是：
      - fault_type / fault_target：单故障（旧版全部用例走这条，保持兼容）
      - fault_chain：[{"type","target"}, ...]：复合故障，多个故障落在不同逻辑步
    """
    chain = run_exp.get("fault_chain")
    if chain:
        return [{"type": f["type"], "target": int(f.get("target") or 0)} for f in chain]
    ft = run_exp.get("fault_type")
    if ft and ft != "none":
        return [{"type": ft, "target": int(run_exp.get("fault_target") or 0)}]
    return []


def _last_attempt(steps):
    """只取「最后一次执行尝试」的轨迹做诊断。

    多轮恢复时，steps 里会累积前几轮的重放轨迹；若拿全量去诊断，
    重复的 tool 步会被规则 0b 误判成 resource_abuse，诊断就脏了。
    每次 Ctx 的 logical 都从 1 重新计数，所以最后一个 logical==1 就是本轮起点。
    """
    start = 0
    for i, s in enumerate(steps):
        if (s.get("logical") or 0) == 1:
            start = i
    return steps[start:] if start else steps

SYSTEM_PROMPT = ("你是业务 Agent。只看用户给出的上下文回答，不要使用上下文以外的知识。"
                 "只输出一个 JSON 对象，不要输出解释、不要复述要求、不要输出思考过程。")


class ToolError(Exception):
    def __init__(self, error_type, message):
        self.error_type = error_type
        self.message = message
        super().__init__("%s: %s" % (error_type, message))


class Interrupted(Exception):
    pass
