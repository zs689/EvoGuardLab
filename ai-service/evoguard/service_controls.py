# -*- coding: utf-8 -*-
"""服务层工程基线：鉴权 / 限流 / kill switch / 结构化审计日志 / 配置校验。

纯标准库实现，零运行时依赖（与 pyproject.toml 的 ``dependencies = []`` 一致）。
本模块刻意不 import app.py，也不 import evoguard 下其它模块，可独立单元测试。

外部依据（控制项映射，来源已核实，仅作工程基线参考，不代表合规认证）：
  * OWASP AI Agent Security Cheat Sheet
    https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html
    —— tool allowlist、approval 原子消费、fail-closed、rate limits、audit logs。
       本模块对应其中的 rate limits（check_rate）、audit logs（log）、
       fail-closed（validate_config 供启动期校验）。
  * OWASP Top 10 for Agentic Applications 2026
    https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-2026/
    —— ASI02（最小权限 / 限流）、ASI03（身份 / 鉴权）、
       ASI08（级联失败的限流与熔断）、ASI10（stop switch 与活动日志）。
       本模块对应 ASI02/ASI08 的限流、ASI03 的 Bearer 鉴权、
       ASI10 的 kill_switch 与结构化活动日志。

设计红线（务必保持）：
  * 默认不设置 ``AGENTLAB_API_TOKEN`` 时鉴权关闭 —— 这是为了向后兼容既有
    冒烟测试与「本地直接 python app.py 打开」的用法；但配置里必须显式给出
    warning，绝不静默，且文档里要写清风险。
  * 任何 token / 请求正文都绝不写进日志；log() 只挑白名单字段重建记录。
  * 数值型环境变量解析失败不在构造函数里吞掉，由 validate_config() 报出，
    供 main() 启动时 fail-closed。
  * 鉴权比较用 hmac.compare_digest（恒定时间），绝不用 ``==``。
"""
from __future__ import annotations

import collections
import datetime
import hmac
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass

DEFAULT_RATE_LIMIT = 60
DEFAULT_RATE_WINDOW = 60.0
# 需要被 stop switch 拦下的「写」方法；GET/HEAD 始终可读。
WRITE_METHODS = frozenset(("POST", "PUT", "DELETE", "PATCH"))
# kill switch 视为开启的字符串（大小写不敏感）。
TRUE_VALUES = frozenset(("1", "true", "on", "yes"))

# 本文件位于 <repo>/ai-service/evoguard/service_controls.py，向上三级到仓库根。
_DEFAULT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class ServiceConfig:
    """所有配置集中在这里。``describe()`` 负责脱敏输出，绝不外泄 api_token。"""

    auth_enabled: bool = False
    api_token: str | None = None          # 仅内存持有；不进 describe / 不进日志
    warning: str = ""
    rate_limit: int = DEFAULT_RATE_LIMIT         # 生效值（解析失败时取默认）
    rate_window: float = DEFAULT_RATE_WINDOW     # 生效值（秒）
    rate_limit_raw: str = str(DEFAULT_RATE_LIMIT)
    rate_window_raw: str = str(DEFAULT_RATE_WINDOW)
    kill_switch: bool = False
    kill_switch_raw: str = ""
    thresholds_path: str = ""


def _as_positive_int(raw, default):
    try:
        v = int(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _as_positive_float(raw, default):
    try:
        v = float(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _is_truthy(raw):
    return str(raw).strip().lower() in TRUE_VALUES


class ServiceControls:
    """可独立实例化的服务控制集合。"""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self._lock = threading.Lock()
        # actor -> deque[单调递增时间戳]；滑动窗口 + 定期清理，避免无限增长。
        self._hits = collections.defaultdict(collections.deque)

    @classmethod
    def from_env(cls, environ=None):
        """从环境变量构造配置。

        未设（或空）``AGENTLAB_API_TOKEN`` => 鉴权关闭，并在 warning 里写明风险。
        ``environ`` 可注入以便测试；默认取 ``os.environ``。
        """
        if environ is None:
            environ = os.environ
        token = environ.get("AGENTLAB_API_TOKEN")
        auth_enabled = bool(token and str(token).strip())
        warning = ""
        if not auth_enabled:
            warning = ("AGENTLAB_API_TOKEN 未设置：鉴权已关闭，所有接口开放访问。"
                       "仅可用于本机开发/演示；生产环境必须设置该变量并置于 TLS 之后。")

        rate_limit_raw = environ.get("AGENTLAB_RATE_LIMIT", str(DEFAULT_RATE_LIMIT))
        rate_window_raw = environ.get("AGENTLAB_RATE_WINDOW", str(DEFAULT_RATE_WINDOW))
        kill_switch_raw = environ.get("AGENTLAB_KILL_SWITCH", "")
        root = environ.get("AGENTLAB_ROOT", _DEFAULT_ROOT)
        thresholds_path = environ.get(
            "AGENTLAB_THRESHOLDS",
            os.path.join(root, "config", "thresholds_v1.json"))

        cfg = ServiceConfig(
            auth_enabled=auth_enabled,
            api_token=str(token) if auth_enabled else None,
            warning=warning,
            rate_limit=_as_positive_int(rate_limit_raw, DEFAULT_RATE_LIMIT),
            rate_window=_as_positive_float(rate_window_raw, DEFAULT_RATE_WINDOW),
            rate_limit_raw=str(rate_limit_raw),
            rate_window_raw=str(rate_window_raw),
            kill_switch=_is_truthy(kill_switch_raw),
            kill_switch_raw=str(kill_switch_raw),
            thresholds_path=thresholds_path,
        )
        return cls(cfg)

    # ---------------- 鉴权（OWASP ASI03 / Cheat Sheet） ----------------
    def authorize(self, headers):
        """校验 ``Authorization: Bearer <token>``。

        ``headers`` 只需支持 ``.get(name)``（dict 或 http.client.HTTPMessage 均可）。
        返回 ``(ok, reason, actor)``；``X-Actor`` 仅作审计主体，缺省 anonymous。
        """
        actor = "anonymous"
        auth_header = ""
        if headers:
            actor = headers.get("X-Actor") or "anonymous"
            auth_header = headers.get("Authorization") or ""
        if not isinstance(actor, str):
            actor = "anonymous"
        actor = actor.strip() or "anonymous"

        if not self.config.auth_enabled:
            return True, "auth_disabled", actor

        if not isinstance(auth_header, str):
            auth_header = ""
        parts = auth_header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False, "missing_or_malformed_token", actor
        presented = parts[1].strip()
        expected = self.config.api_token or ""
        # 恒定时间比较，避免时序侧信道；统一转 bytes 以兼容非 ASCII token。
        try:
            matched = hmac.compare_digest(presented.encode("utf-8"),
                                          expected.encode("utf-8"))
        except (TypeError, ValueError):
            matched = False
        if not matched:
            return False, "invalid_token", actor
        return True, "ok", actor

    # ---------------- 限流（OWASP ASI02 / ASI08 / Cheat Sheet） ----------------
    def check_rate(self, actor, now=None):
        """滑动窗口限流，``threading.Lock`` 保证线程安全。

        返回 ``(ok, retry_after)``。``now`` 可注入以便确定性测试；生产调用只用
        默认的 ``time.monotonic()``。每次调用先清理所有过期时间戳与空桶，
        避免字典随不同 actor 无限增长。
        """
        if now is None:
            now = time.monotonic()
        window = self.config.rate_window
        limit = self.config.rate_limit
        cutoff = now - window
        with self._lock:
            for key in list(self._hits.keys()):
                dq = self._hits[key]
                while dq and dq[0] <= cutoff:
                    dq.popleft()
                if not dq:
                    del self._hits[key]
            dq = self._hits[actor]
            if len(dq) >= limit:
                retry_after = max(0.0, dq[0] + window - now)
                return False, retry_after
            dq.append(now)
            return True, 0.0

    # ---------------- stop switch（OWASP ASI10） ----------------
    def kill_switch(self, method=None):
        """kill switch 是否生效。

        不给 method 时返回开关是否打开；给 method 时只挡写操作，GET 仍可读。
        """
        if not self.config.kill_switch:
            return False
        if method is None:
            return True
        return str(method).upper() in WRITE_METHODS

    # ---------------- 结构化审计日志（OWASP ASI10 / Cheat Sheet） ----------------
    def new_request_id(self):
        return uuid.uuid4().hex

    def log(self, event):
        """输出单行 JSON 到 stderr。

        只按白名单重建记录 —— 即便调用方误把 token / body 塞进 event，
        也不会写进日志。``sort_keys=True`` 保证字段顺序确定性。
        """
        if not isinstance(event, dict):
            event = {}
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "request_id": event.get("request_id", ""),
            "method": event.get("method", ""),
            "path": event.get("path", ""),
            "status": event.get("status", 0),
            "actor": event.get("actor", "anonymous"),
            "decision": event.get("decision", ""),
        }
        sys.stderr.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

    # ---------------- 配置校验（fail-closed 的启动期依据） ----------------
    def validate_config(self):
        """返回错误字符串列表；空列表表示配置合法。

        刻意不在构造函数/解析里偷偷吞掉坏值：数值型环境变量保留原始串，
        在这里重新解析并报错；同时校验阈值文件存在且为合法 JSON。
        """
        errors = []
        for name, raw, caster in (
            ("AGENTLAB_RATE_LIMIT", self.config.rate_limit_raw, int),
            ("AGENTLAB_RATE_WINDOW", self.config.rate_window_raw, float),
        ):
            try:
                v = caster(raw)
                if v <= 0:
                    raise ValueError("must be positive")
            except (TypeError, ValueError):
                errors.append("%s 不是合法的正数：%r" % (name, raw))
        path = self.config.thresholds_path
        if not os.path.isfile(path):
            errors.append("阈值文件不存在：%s" % path)
        else:
            try:
                with open(path, encoding="utf-8") as f:
                    json.load(f)
            except (OSError, ValueError) as e:
                errors.append("阈值文件不是合法 JSON：%s（%s）" % (path, e))
        return errors

    # ---------------- 脱敏配置摘要 ----------------
    def describe(self):
        """返回脱敏后的配置摘要：token 只显示是否已设置，绝不显示值。"""
        return {
            "auth_enabled": self.config.auth_enabled,
            "api_token_set": bool(self.config.api_token),
            "warning": self.config.warning,
            "rate_limit": self.config.rate_limit,
            "rate_window": self.config.rate_window,
            "kill_switch": self.config.kill_switch,
            "thresholds_path": self.config.thresholds_path,
        }
