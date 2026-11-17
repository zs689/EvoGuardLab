# -*- coding: utf-8 -*-
"""WS-03-T2：多特征融合 + 轻量模型路由的语义扫描器。

注册到 `scan_pipeline` 的名字：`semantic_route`。

分层：
1. L1（正则 / 不可见字符）与 action_scope 命中 → 直接 BLOCK（确定性特征）；
2. 灰区（无确定特征，但含动作请求、长文本、URL/编码片段）→ 路由语义判官；
3. 判官不可用 / 超时 → HUMAN_IN_THE_LOOP_REQUIRED（**fail-closed review**），
   绝不因为「没判出来」就 ALLOW —— 这对应 v3 第 7 条线 silent_pass_max=0。

判官接口：任何带 `judge(text) -> {manipulative, confidence, error}` 的对象
（`agent_watch.OnnxInjectionJudge` 或 `SemanticJudge` 都符合），通过构造注入，
核心库零第三方依赖。
"""
import re

try:
    from . import action_scope as _as
    from . import scan_pipeline as _sp
    from . import security as _sec
except ImportError:
    import action_scope as _as                              # type: ignore
    import scan_pipeline as _sp                             # type: ignore
    import security as _sec                                 # type: ignore

_GREY_URL = re.compile(r"https?://|data:|javascript:|&\w+=|%[0-9A-Fa-f]{2}")
_GREY_ACTION = re.compile(
    r"(忽略|系统提示|新指令|管理员|授权|转账|发到|上传|执行|权限)", re.I)


def _grey(text):
    """哪些文本需要过判官：短、干净、无动作词的不路由（省延迟）。"""
    reasons = []
    if len(text) > 400:
        reasons.append("long_text")
    if _GREY_URL.search(text):
        reasons.append("url_or_encoded")
    if _GREY_ACTION.search(text):
        reasons.append("actionish")
    if _as.detect_action_requests(text):
        reasons.append("action_request")
    return reasons


@_sp.register_scanner("semantic_route")
class SemanticRouteScanner(_sp.Scanner):
    name = "semantic_route"
    require_full_trace = False

    def __init__(self, judge=None, threshold=0.5, on_unavailable="review",
                 enable_grey=True):
        self.judge = judge
        self.threshold = float(threshold)
        self.on_unavailable = on_unavailable
        self.enable_grey = bool(enable_grey)

    def _unavailable(self, reason):
        dec = (_sp.ScanDecision.BLOCK if self.on_unavailable == "block"
               else _sp.ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED)
        return _sp.ScanResult(decision=dec, reason="semantic_unavailable:" + reason,
                              score=1.0, findings=[{
                                  "code": "semantic_unavailable",
                                  "severity": "error", "detail": reason}])

    def scan(self, message, past_trace=None):
        text = message.content or ""
        # 1) L1 确定性特征
        findings = _sec.scan(text, where=message.role.value)
        hard = [f for f in findings if f["severity"] == "error"]
        if hard:
            return _sp.ScanResult(
                decision=_sp.ScanDecision.BLOCK,
                reason="L1:" + ",".join(f["code"] for f in hard[:4]),
                score=1.0, findings=findings)
        # 2) 灰区判定
        reasons = _grey(text) if self.enable_grey else ["always"]
        if not reasons:
            return _sp.ScanResult(decision=_sp.ScanDecision.ALLOW,
                                  reason="clean", score=0.0,
                                  findings=findings)
        # 3) 没有判官就转人工，不静默放行（v3 第 7 条线）
        if self.judge is None or not getattr(self.judge, "available", lambda: True)():
            return self._unavailable("judge_unavailable")
        try:
            r = self.judge.judge(text)
        except Exception as exc:                            # noqa: BLE001
            return self._unavailable("judge_error:" + repr(exc)[:80])
        if r.get("error"):
            return self._unavailable(str(r["error"])[:80])
        manip = bool(r.get("manipulative"))
        conf = float(r.get("confidence") or 0.0)
        if manip and conf >= self.threshold:
            return _sp.ScanResult(
                decision=_sp.ScanDecision.BLOCK,
                reason="semantic:%.3f (%s)" % (conf, ",".join(reasons[:3])),
                score=conf,
                findings=findings + [{"code": "semantic_injection",
                                      "severity": "error",
                                      "reason": r.get("reason", "")}])
        if manip or conf >= self.threshold * 0.6:
            return _sp.ScanResult(
                decision=_sp.ScanDecision.HUMAN_IN_THE_LOOP_REQUIRED,
                reason="semantic_grey:%.3f" % conf, score=conf,
                findings=findings + [{"code": "semantic_review",
                                      "severity": "warn"}])
        return _sp.ScanResult(decision=_sp.ScanDecision.ALLOW,
                              reason="semantic_clean:%.3f" % conf,
                              score=conf, findings=findings)
