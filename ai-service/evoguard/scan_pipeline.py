# -*- coding: utf-8 -*-
"""按角色配置的扫描器流水线 + 轨迹回放（借鉴 LlamaFirewall 的编排方法）。

来源：meta-llama/PurpleLlama 的 LlamaFirewall（MIT，本地只读研读）。它的三件事值得学：
  1. `Configuration = Mapping[Role, Sequence[ScannerType]]` —— 不同落点挂不同扫描器，
     而不是所有文本走同一套规则；
  2. `register_scanner` / `create_scanner` —— 扫描器可插拔、按名字懒加载；
  3. `scan_replay` —— 逐条消息带「此前轨迹」扫描，BLOCK / 人工复核立即停止。

**刻意没照抄的地方**（见 docs/llamafirewall-merge.md）：
  - 上游 RegexScanner 命中邮箱/电话也直接 BLOCK；这里按 severity 分级，
    info 级（例如公开邮箱）不拦，error 级才 BLOCK，warn 级转人工复核。
  - 上游 `ScanResult` 只有 decision/reason/score/status；这里保留 findings 明细，
    与本项目六字段执行契约和 CI 的 pass/review/block 对齐。
  - 不引入 torch / transformers / Semgrep / 外部 API，纯标准库 + 现有 security 层。

默认配置是「角色 → 扫描器名」的字典，可通过 `ScanPipeline(configuration=...)` 覆盖。
"""
import json
import os
import sys
from dataclasses import dataclass, field
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import security as S  # noqa: E402


class Role(Enum):
    USER = "user"
    TOOL = "tool"
    ASSISTANT = "assistant"
    MEMORY = "memory"
    SYSTEM = "system"


class ScanDecision(Enum):
    ALLOW = "allow"
    HUMAN_IN_THE_LOOP_REQUIRED = "human_in_the_loop_required"
    BLOCK = "block"


class ScanStatus(Enum):
    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class Message:
    role: Role
    content: str
    tool_calls: list = field(default_factory=list)

    def __str__(self):
        return "%s: %s" % (self.role.value, self.content)


@dataclass
class ScanResult:
    decision: ScanDecision
    reason: str
    score: float = 0.0
    status: ScanStatus = ScanStatus.SUCCESS
    findings: list = field(default_factory=list)


# 严重程度 → 决策。这是与上游最大的分歧，写在这里便于一眼审。
_SEVERITY_DECISION = {
    "error": ScanDecision.BLOCK,
    "warn": ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED,
    "info": ScanDecision.ALLOW,
}
_SEVERITY_SCORE = {"error": 1.0, "warn": 0.5, "info": 0.1}

# 这些是「内容特征不是攻击」的配置类信号，不参与决策聚合，只留在 findings 里。
_NON_ATTACK_CODES = {"tool_policy_missing", "peer_not_allowed"}


class Scanner:
    """扫描器基类。子类只需实现 scan(message, past_trace)。"""

    name = "base"
    require_full_trace = False

    def scan(self, message, past_trace=None):
        raise NotImplementedError


class RegexScanner(Scanner):
    """复用 security.scan 的 24 条特征表（含不可见字符归一化）。"""

    name = "regex"

    def scan(self, message, past_trace=None):
        findings = S.scan(message.content, where=message.role.value)
        active = [f for f in findings if f["code"] not in _NON_ATTACK_CODES]
        decision, score = ScanDecision.ALLOW, 0.0
        for f in active:
            d = _SEVERITY_DECISION.get(f["severity"], ScanDecision.ALLOW)
            sc = _SEVERITY_SCORE.get(f["severity"], 0.1)
            if _rank(d) > _rank(decision):
                decision = d
            score = max(score, sc)
        reason = "; ".join("%s(%s)" % (f["code"], f["severity"])
                           for f in active) or "default"
        return ScanResult(decision=decision, reason=reason,
                          score=score, findings=findings)


class HiddenASCIIScanner(Scanner):
    """不可见字符专用扫描器：只要出现 tag chars / 零宽 / bidi 就留证。

    与 RegexScanner 会重复报一次，但这一层可以在配置里单独挂到
    TOOL / MEMORY 这类「本不该有富文本控制符」的落点。
    """

    name = "hidden_ascii"

    def scan(self, message, past_trace=None):
        # 走完整 scan() 再筛 hidden_*：复用归一化与统计，不碰 security 的私有函数
        hits = [f for f in S.scan(message.content, where=message.role.value)
                if f["code"].startswith("hidden_")]
        if not hits:
            return ScanResult(ScanDecision.ALLOW, "default", 0.0)
        decision = (ScanDecision.BLOCK
                    if any(f["severity"] == "error" for f in hits)
                    else ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED)
        return ScanResult(decision, "; ".join(f["code"] for f in hits),
                          1.0 if decision == ScanDecision.BLOCK else 0.5,
                          findings=hits)


class AlignmentScanner(Scanner):
    """轨迹级目标对齐审查（借鉴 LlamaFirewall AlignmentCheck）。

    默认**不挂**在任何角色上：它需要本地模型、约 1 秒/次。
    想用就显式配置，例如
    `ScanPipeline({Role.ASSISTANT: ["alignment"]})`。
    """

    name = "alignment"
    require_full_trace = True

    def scan(self, message, past_trace=None):
        import semantic_guard as SG       # 懒加载，默认路径不碰模型
        goal = None
        for m in (past_trace or []):
            if m.role == Role.USER:
                goal = m.content
                break
        if not goal:
            return ScanResult(ScanDecision.ALLOW, "轨迹里没有用户目标，跳过",
                              0.0, ScanStatus.SKIPPED)
        r = SG.judge_alignment(goal, message.content,
                               [str(m) for m in (past_trace or [])])
        decision = (ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED
                    if r["decision"] == "human_in_the_loop_required"
                    else ScanDecision.ALLOW)
        finding = {"code": "goal_misalignment", "severity": "warn",
                   "where": message.role.value,
                   "match": r["observation"][:120],
                   "detail": r["thought"][:200], "false_positive":
                   "小模型可能把合理的中间步骤误判为偏离，命中只转人工复核"}
        return ScanResult(decision, r["reason"],
                          0.5 if decision != ScanDecision.ALLOW else 0.0,
                          (ScanStatus.ERROR if r["status"] != "observed"
                           else ScanStatus.SUCCESS),
                          [finding] if decision != ScanDecision.ALLOW else [])


def _rank(decision):
    return {ScanDecision.ALLOW: 0,
            ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED: 1,
            ScanDecision.BLOCK: 2}.get(decision, 0)


_BUILTIN = {"regex": RegexScanner, "hidden_ascii": HiddenASCIIScanner,
            "alignment": AlignmentScanner}
_REGISTRY = {}


def register_scanner(name):
    """注册自定义扫描器（借鉴上游 register_llamafirewall_scanner 装饰器）。"""
    def deco(cls):
        if not issubclass(cls, Scanner):
            raise ValueError("注册的扫描器必须继承 Scanner")
        _REGISTRY[name] = cls
        return cls
    return deco


def create_scanner(name):
    if name in _REGISTRY:
        return _REGISTRY[name]()
    if name in _BUILTIN:
        return _BUILTIN[name]()
    raise ValueError("未知扫描器：%r" % name)


DEFAULT_CONFIGURATION = {
    Role.USER: ["regex"],
    Role.TOOL: ["regex", "hidden_ascii"],
    Role.ASSISTANT: ["regex"],
    Role.MEMORY: ["hidden_ascii"],
    Role.SYSTEM: [],
}


class ScanPipeline:
    """按角色编排扫描器。默认不改变 security.py 的任何既有入口。"""

    def __init__(self, configuration=None):
        self.configuration = dict(configuration or DEFAULT_CONFIGURATION)

    def scan(self, message, past_trace=None):
        names = self.configuration.get(message.role, [])
        results = []
        for name in names:
            scanner = create_scanner(name)
            if scanner.require_full_trace and past_trace is None:
                results.append(ScanResult(ScanDecision.ALLOW,
                                          "%s: 需要历史轨迹，跳过" % name,
                                          0.0, ScanStatus.SKIPPED))
                continue
            try:
                results.append(scanner.scan(message, past_trace))
            except Exception as e:                # noqa: BLE001
                results.append(ScanResult(
                    ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED,
                    "%s 扫描失败：%s: %s" % (name, type(e).__name__, e),
                    0.5, ScanStatus.ERROR))
        if not results:
            return ScanResult(ScanDecision.ALLOW, "default", 0.0)
        # BLOCK 优先，其次人工复核；扫描器自身出错也要人工看，不静默放行
        decision = max((r.decision for r in results), key=_rank)
        reasons = [r.reason for r in results if r.reason != "default"]
        # 同一条内容可能同时挂在 regex / hidden_ascii 上，明细去重免得报告虚胖
        findings, seen = [], set()
        for r in results:
            for f in r.findings:
                key = (f.get("code"), f.get("where"), f.get("match"))
                if key not in seen:
                    seen.add(key)
                    findings.append(f)
        status = (ScanStatus.ERROR
                  if any(r.status == ScanStatus.ERROR for r in results)
                  else ScanStatus.SUCCESS)
        return ScanResult(decision, "; ".join(dict.fromkeys(reasons)) or "default",
                          max((r.score for r in results), default=0.0),
                          status, findings)

    def scan_replay(self, trace):
        """逐条带历史扫描；BLOCK / 人工复核立即停止（借上游 scan_replay）。"""
        result = ScanResult(ScanDecision.ALLOW, "default", 0.0)
        for ix, message in enumerate(trace):
            result = self.scan(message, trace[:ix] if ix else None)
            if result.decision != ScanDecision.ALLOW:
                break
        return result

    def scan_replay_build_trace(self, message, stored_trace=None):
        """只把 ALLOW 的消息加进轨迹；被拦/待复核的不污染后续上下文。"""
        stored = list(stored_trace or [])
        result = self.scan(message, stored if stored else None)
        if result.decision == ScanDecision.ALLOW:
            return result, stored + [message]
        return result, stored


def to_ci_action(result):
    """把三档决策映射到本项目 CI 的 pass / review / block。"""
    return {ScanDecision.ALLOW: "pass",
            ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED: "review",
            ScanDecision.BLOCK: "block"}[result.decision]


def from_guard_result(guard_result, role=Role.TOOL):
    """把现有六字段 guard 结果转成流水线结果，便于渐进接入。"""
    text = guard_result.get("text") or ""
    msg = Message(role=role, content=text)
    r = RegexScanner().scan(msg)
    r.findings = list(guard_result.get("findings") or r.findings)
    return r
