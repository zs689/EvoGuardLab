# -*- coding: utf-8 -*-
"""DEEP-05：深度/语义检测器（从 security.py 机械拆出）。"""

# DEEP-05 通用兜底 imports（机械拆分后原模块级依赖统一补齐）
import hashlib
import json
import os
import re
import time
import traceback
import uuid
import concurrent.futures as _cf

import os
import re
import json

from security_rules import DETECTORS

def deep_enabled():
    """第二级（学出来的模型）是否开启：**默认关**。

    来源：外部语料上正则对自然语言注入召回 0，训练了字符 n-gram 模型补齐；
    但门禁复核显示它在没见过的业务域上误报 33%，所以只作为显式开关，
    不默认打开（`AGENTLAB_SECURITY_DEEP=1` + `AGENTLAB_DEEP_MODEL=<path>`）。
    真实收益与代价见 docs/detector-evolution.md。
    """
    return (os.environ.get("AGENTLAB_SECURITY_DEEP") or "").strip().lower() in ("1", "true", "on")


_DEEP_CACHE = {"path": None, "model": None}


def deep_model():
    """按路径懒加载模型；加载失败返回 None（不因为模型缺失就中断扫描）。"""
    path = (os.environ.get("AGENTLAB_DEEP_MODEL") or "").strip()
    if not path:
        return None
    if _DEEP_CACHE["path"] == path:
        return _DEEP_CACHE["model"]
    try:
        import json as _json
        with open(path, encoding="utf-8") as f:
            model = _json.load(f)
        _DEEP_CACHE.update({"path": path, "model": model})
        return model
    except Exception:                     # noqa: BLE001
        _DEEP_CACHE.update({"path": path, "model": None})
        return None


def _rails_result_findings(tool, text):
    """按工具声明的返回侧护栏检查（`tool_rails`）。

    与 `_scope_findings` 的区别：那条是写死的规则，这条是**配置驱动**的 ——
    换工具就换检查（例如 `http.send` 的返回不需要职责判断，`kb.search` 需要），
    改配置不用改代码。检查器自己抛错时给 `rails_checker_error`，
    绝不折算成「没命中」。
    """
    if (os.environ.get("AGENTLAB_SECURITY_RAILS") or "1").strip().lower() in ("0", "false", "off"):
        return []
    try:
        import tool_rails
        rules = tool_rails.load_default()
        v = tool_rails.check_result(rules, tool, text)
    except Exception as e:                    # noqa: BLE001
        return [{"code": "rails_checker_error", "severity": "warn",
                 "where": "tool_output", "match": type(e).__name__,
                 "detail": "工具护栏执行失败：%s: %s" % (type(e).__name__, e),
                 "false_positive": ""}]
    if v["status"] == tool_rails.SAFE:
        return []
    if v["status"] in (tool_rails.ERROR, tool_rails.NOT_DECLARED):
        return [{"code": "rails_" + v["status"], "severity": "warn",
                 "where": "tool_output", "match": tool,
                 "detail": v["reason"], "false_positive": ""}]
    out = []
    for f in v["findings"]:
        # action_scope 已经有专门的码，别重复报
        if f["code"] == "action_on_read_only_tool":
            continue
        out.append({"code": "rails_" + f["code"],
                    "severity": f["severity"], "where": "tool_output",
                    "match": "%s/%s" % (f["scanner"], f["code"]),
                    "detail": "工具 %s 的返回命中了声明式护栏规则" % tool,
                    "false_positive": "配置里的检查是通用规则，"
                                      "正常文档也可能命中，需人工看一眼"})
    return out


def _scope_findings(tool, text):
    """「动作-职责不符」：只读工具的返回里要求助手执行动作。

    为什么单独一层：InjecAgent base 变体（「请把我的门锁权限给 Amy」）没有任何
    攻击关键词，一级正则召回 0.000、三级语义 0.050。但它违反了一个确定的事实
    —— 只读工具不可能合法地要求调用方去授权/转账/删除。这不是猜意图，是拿
    工具职责当基线做对比。实测测试集召回 0.806、误报 0.2%，纯正则、零模型调用。
    默认**开**：它只产出 info/warn，不会误阻断（见 action_scope 里的边界说明）。
    关掉：`AGENTLAB_SECURITY_SCOPE=0`。
    """
    if (os.environ.get("AGENTLAB_SECURITY_SCOPE") or "1").strip().lower() in ("0", "false", "off"):
        return []
    try:
        import action_scope
    except Exception:                     # noqa: BLE001
        return []
    f = action_scope.check_output_scope(tool, text)
    return [f] if f else []


def _deep_findings(text, where):
    """跑第二级；只回答「这段文本像不像指令」，落点由调用方限定。"""
    if not deep_enabled():
        return []
    try:
        import deep_scan
    except Exception:                     # noqa: BLE001
        return []
    model = deep_model()
    if model is None or not deep_scan.applicable(where):
        return []
    r = deep_scan.classify(model, text)
    if not r["is_injection"]:
        return []
    top = "、".join(x["ngram"] for x in r["evidence"][:3]) or "—"
    return [{"code": "injection_learned", "severity": "warn",
             "where": where, "match": top, "score": r["score"],
             "false_positive": "模型学的是「像指令」；正常业务里的祈使句也会命中，"
                               "所以只在 tool_output / peer_message 落点使用"}]


_SEMANTIC = None


def semantic_enabled():
    """第三级（本地语义判定）是否开启：**默认关**。

    它比前两级慢约 3 个数量级（实测 p50 ~1s），只适合离线复核或低流量入口；
    显式设 `AGENTLAB_SECURITY_SEMANTIC=1` 才启用。
    """
    return (os.environ.get("AGENTLAB_SECURITY_SEMANTIC") or "").strip() in (
        "1", "true", "on")


def _semantic_findings(text, where):
    if not semantic_enabled():
        return []
    global _SEMANTIC
    try:
        import semantic_guard
        if _SEMANTIC is None:
            _SEMANTIC = semantic_guard.SemanticGuard()
        if not semantic_guard.applicable(where):
            return []
        r = _SEMANTIC.judge(where, text)
        if r["status"] != "observed" or not r["is_attack"]:
            return []
        return [{"code": "injection_semantic", "severity": "warn",
                 "where": where, "match": r["reason"][:120],
                 "false_positive": "语义判定由本地小模型给出，需人工复核；"
                                   "不自动改正文、不停止调用"}]
    except Exception:                     # noqa: BLE001 - 模型不可用不能拖垮扫描
        return []


