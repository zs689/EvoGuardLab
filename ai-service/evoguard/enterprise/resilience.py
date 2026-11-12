# -*- coding: utf-8 -*-
"""韧性：熔断、超时预算、SLO 计算、故障演练。

真实现：三态熔断器（closed/open/half_open）、单请求超时预算、
SLO 达标与错误预算消耗、可复现的故障演练脚本。
预留接口：`AlertSink` —— 熔断打开时企业可推 PagerDuty / Webhook，实现一个方法即可。

SLO 是**计算**，不是承诺：它把「总请求 / 成功请求」换算成 SLI 与错误预算，
不产生任何对外 SLA 文本。真实 SLA 需要合同与运维流程，代码给不了。
"""
import threading
import time
from dataclasses import dataclass

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    """失败达到阈值就打开；冷却后放一个探测请求，成功则闭合。线程安全。"""

    def __init__(self, failure_threshold=5, cooldown_s=30.0, clock=None):
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_s = float(cooldown_s)
        self._clock = clock or time.monotonic
        self._state = CLOSED
        self._failures = 0
        self._opened_at = None
        self._lock = threading.Lock()

    @property
    def state(self):
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self):
        if self._state == OPEN and self._opened_at is not None \
                and self._clock() - self._opened_at >= self.cooldown_s:
            self._state = HALF_OPEN

    def allow(self):
        with self._lock:
            self._maybe_half_open()
            return self._state in (CLOSED, HALF_OPEN)

    def record(self, ok):
        with self._lock:
            if ok:
                if self._state == HALF_OPEN:
                    self._state = CLOSED
                self._failures = 0
                self._opened_at = None
                return self._state
            self._failures += 1
            if self._state == HALF_OPEN:
                self._state = OPEN
                self._opened_at = self._clock()
            elif self._failures >= self.failure_threshold:
                self._state = OPEN
                self._opened_at = self._clock()
            return self._state

    def snapshot(self):
        with self._lock:
            self._maybe_half_open()
            return {"state": self._state, "failures": self._failures,
                    "opened_at": self._opened_at,
                    "failure_threshold": self.failure_threshold,
                    "cooldown_s": self.cooldown_s}


class TimeoutBudget:
    """单请求总预算；跨步骤扣减，超了就不要开始下一步。"""

    def __init__(self, total_s, clock=None):
        self.total_s = float(total_s)
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + self.total_s

    def remaining(self):
        return max(0.0, self._deadline - self._clock())

    def spend(self, seconds):
        if self.remaining() <= 0:
            return False
        return self.remaining() >= float(seconds)

    @property
    def expired(self):
        return self.remaining() <= 0


@dataclass
class SLO:
    name: str
    target: float                     # 例如 0.999
    window_s: float = 3600.0

    def evaluate(self, total, good):
        """返回 SLI、是否达标、错误预算剩余。分母为 0 记 None（不写 0）。"""
        if total <= 0:
            return {"name": self.name, "window_s": self.window_s,
                    "target": self.target, "total": total, "good": good,
                    "sli": None, "met": None, "budget_remaining": None,
                    "exhausted": None}
        bad = max(0, total - good)
        allowed_bad = (1.0 - self.target) * total
        sli = good / float(total)
        return {"name": self.name, "window_s": self.window_s,
                "target": self.target, "total": total, "good": good,
                "sli": round(sli, 6), "met": sli >= self.target,
                "allowed_bad": round(allowed_bad, 3),
                "bad": bad,
                "budget_remaining": round(allowed_bad - bad, 3),
                "exhausted": bad > allowed_bad}


class AlertSink:
    def alert(self, event):                # pragma: no cover - 接口
        raise NotImplementedError


class DrillRunner:
    """故障演练：把一串成功/失败结果喂给熔断器，记录状态轨迹。

    `advance(seconds)` 用于推进虚拟时钟，验证「冷却后能恢复」而不是靠 sleep。
    """

    def __init__(self, breaker, alert=None):
        self.breaker = breaker
        self.alert = alert

    def run(self, outcomes, advance=None, cooldown_s=0.0):
        trace, opened = [], False
        for ok in outcomes:
            if advance is not None and cooldown_s:
                advance(cooldown_s)
            allowed = self.breaker.allow()
            if allowed:
                self.breaker.record(ok)
            state = self.breaker.state
            trace.append({"allowed": allowed, "ok": bool(ok), "state": state})
            if state == OPEN:
                opened = True
        summary = {"steps": len(trace), "opened": opened,
                   "final_state": self.breaker.state,
                   "failed_steps": sum(1 for t in trace if not t["ok"]),
                   "trace": trace}
        if opened and self.alert is not None:
            self.alert.alert({"type": "circuit_open", "summary": summary})
        return summary

    def run_with_recovery(self, failing_steps, recover_steps=1,
                          advance=None, cooldown_s=0.0):
        """先制造失败让熔断打开，再冷却 + 成功探测，验证能回到 closed。"""
        outcomes = [False] * failing_steps + [True] * recover_steps
        return self.run(outcomes, advance=advance, cooldown_s=cooldown_s)
