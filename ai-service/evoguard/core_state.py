# -*- coding: utf-8 -*-
"""错误码与运行状态机（纯数据，从 core.py 拆出）。

把"哪些状态是终态、允许哪些转移"放在一个独立、无依赖的模块里：
  - 状态机是业务不变量，改它应该只需要看这一处；
  - core.py 不必为常量表占位；
  - 转移表可以被单独测试（漏一条转移就是一个"状态卡死"的 bug）。
"""
# ---------------- 错误码 ----------------
ERR = {
    "E_BAD_REQUEST": "请求参数不合法",
    "E_NOT_FOUND": "资源不存在",
    "E_INVALID_STATE": "当前状态不允许该操作",
    "E_BUDGET_EXCEEDED": "超出预算（步数/重试/Token）",
    "E_FAULT_INJECT": "故障注入失败",
    "E_APPROVAL_REQUIRED": "该操作需要审批",
    "E_INTERNAL": "内部错误",
}


class ApiError(Exception):
    def __init__(self, code, message="", http=400):
        self.code = code
        self.message = message or ERR.get(code, code)
        self.http = http
        super().__init__("%s: %s" % (code, self.message))


# ---------------- 状态机 ----------------
CREATED, READY, RUNNING = "CREATED", "READY", "RUNNING"
PAUSED, FAILED, RECOVERING = "PAUSED", "FAILED", "RECOVERING"
SUCCEEDED, CANCELLED = "SUCCEEDED", "CANCELLED"

STATES = [CREATED, READY, RUNNING, PAUSED, FAILED, RECOVERING, SUCCEEDED, CANCELLED]

TRANSITIONS = {
    CREATED: {READY, CANCELLED},
    READY: {RUNNING, CANCELLED},
    RUNNING: {SUCCEEDED, FAILED, PAUSED, CANCELLED},
    PAUSED: {RUNNING, CANCELLED},
    RECOVERING: {SUCCEEDED, FAILED, PAUSED, CANCELLED},
    FAILED: {RECOVERING, CANCELLED},
    SUCCEEDED: set(),
    CANCELLED: set(),
}

TERMINAL = {SUCCEEDED, CANCELLED}

