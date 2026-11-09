# -*- coding: utf-8 -*-
"""§5「先改权限结构 再补特征」：结构化工具契约授权。

核心立场：**授权由契约决定，正则只作附加信号，不承担全部授权决策。**
域名、资源 ID、可写路径、最大金额、方法与副作用类型都是结构化字段；
未知身份在 enforce 下一律拒绝；输入文本、模型或经验都不能自行扩大权限。

与 security.py 的关系：
  - security.scan() 的命中结果只写进 `regex_signals`，**不影响 decision**；
  - 真正决定 allow/deny 的是下面这些结构化检查；
  - 这样「改个说法绕过正则」不会自动变成越权，反过来「正则没命中」也不会
    让一个域名/金额/路径越界的调用被放行。

刻意独立成模块，不改 security.guard_tool_args 的默认行为（351 项回归可比）。
"""
import json
import os
import re
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import security  # noqa: E402

CONTRACT_VERSION = "tool-contract-v1"

# 手写冻结契约。字段含义见 docs/tool-contract.md。
DEFAULT_CONTRACTS = {
    "kb.search": {
        "identities": ["support-agent", "kb", "xref"],
        "allowed_domains": [],
        "allowed_resource_pattern": r"^KB-[0-9]{2,4}$",
        "writable_paths": [],
        "max_amount": None,
        "methods": ["read"],
        "side_effect": "none",
        "requires_approval": False,
    },
    "order.get_order": {
        "identities": ["support-agent", "order", "calc"],
        "allowed_domains": [],
        "allowed_resource_pattern": r"^A[0-9]{4}$",
        "writable_paths": [],
        "max_amount": None,
        "methods": ["read"],
        "side_effect": "none",
        "requires_approval": False,
    },
    "order.settle": {
        "identities": ["order"],
        "allowed_domains": [],
        "allowed_resource_pattern": r"^A[0-9]{4}$",
        "writable_paths": ["settlement_ledger"],
        "max_amount": 10000.0,
        "methods": ["write"],
        "side_effect": "write",
        "requires_approval": True,
    },
    "http.send": {
        "identities": [],                  # 默认谁都不许用
        "allowed_domains": [],
        "allowed_resource_pattern": None,
        "writable_paths": [],
        "max_amount": None,
        "methods": [],
        "side_effect": "external",
        "requires_approval": True,
    },
    "shell.exec": {
        "identities": [],
        "allowed_domains": [],
        "allowed_resource_pattern": None,
        "writable_paths": [],
        "max_amount": None,
        "methods": [],
        "side_effect": "arbitrary",
        "requires_approval": True,
    },
}

# 这些参数键按结构化规则核对，不靠正则猜。
URL_KEYS = ("url", "endpoint", "callback")
PATH_KEYS = ("path", "file", "writable_path", "target_path")
AMOUNT_KEYS = ("amount", "total", "max_amount")
RESOURCE_KEYS = ("order_id", "resource_id", "doc_id")


def _find(args, keys):
    for k in keys:
        if k in args and args[k] not in (None, ""):
            return k, args[k]
    return None, None


def _checks(agent, tool, args, contract, policy):
    """逐项结构化检查。任何一项 ok=False 都是授权问题，不是「提示」。"""
    out = []

    def add(field, rule, value, ok, detail=""):
        out.append({"field": field, "rule": rule, "value": str(value),
                    "ok": bool(ok), "detail": detail})

    # 1) 身份：契约声明 + 冻结策略白名单，两者都要过
    identities = set(contract.get("identities") or [])
    in_contract = agent in identities
    in_policy = tool in set(policy.get("allowed_tools") or [])
    add("identity", "known_identity", agent, in_contract,
        "契约登记的身份：%s" % (sorted(identities) or "无"))
    add("tool", "policy_allowlist", tool, in_policy,
        "冻结策略白名单：%s" % sorted(policy.get("allowed_tools") or []))

    # 2) 域名白名单（有 URL 参数才检查）
    key, url = _find(args, URL_KEYS)
    if key:
        host = urlparse(str(url)).hostname or ""
        domains = contract.get("allowed_domains") or []
        add(key, "allowed_domain", host, host in domains,
            "允许域名：%s" % (domains or "无（该工具不许外联）"))

    # 3) 可写路径白名单
    key, path = _find(args, PATH_KEYS)
    if key:
        paths = contract.get("writable_paths") or []
        add(key, "writable_path", path, str(path) in paths,
            "允许可写路径：%s" % (paths or "无"))

    # 4) 最大金额
    key, amount = _find(args, AMOUNT_KEYS)
    if key:
        cap = contract.get("max_amount")
        try:
            ok = cap is not None and float(amount) <= float(cap)
        except (TypeError, ValueError):
            ok = False
        add(key, "max_amount", amount, ok,
            "上限：%s" % ("N/A" if cap is None else cap))

    # 5) 资源 ID 格式
    key, rid = _find(args, RESOURCE_KEYS)
    if key:
        pat = contract.get("allowed_resource_pattern")
        ok = bool(pat and re.match(pat, str(rid)))
        add(key, "resource_id", rid, ok, "要求匹配 %s" % (pat or "无"))

    # 6) 方法
    key, method = _find(args, ("method", "verb"))
    if key:
        methods = contract.get("methods") or []
        add(key, "method", method, str(method).lower() in
            [m.lower() for m in methods], "允许方法：%s" % (methods or "无"))

    # 7) 副作用类型与审批
    side = contract.get("side_effect")
    add("side_effect", "declared", side, side in ("none", "read", "write"),
        "external/arbitrary 一律不允许")
    if contract.get("requires_approval"):
        approved = bool(args.get("approval_request_id"))
        add("approval", "approval_request_id", args.get("approval_request_id"),
            approved, "写操作必须带审批请求 id")
    return out


def authorize(agent, tool, args, *, mode="audit", contracts=None,
              policy=None, policy_digest_value=None):
    """返回结构化授权结论。`regex_signals` 只是附加信号，不参与 decision。"""
    table = contracts or DEFAULT_CONTRACTS
    pol = policy or {"allowed_tools": [], "write_tools": ["order.settle"]}
    contract = table.get(tool)
    result = {
        "version": CONTRACT_VERSION, "agent": agent, "tool": tool,
        "mode": mode, "contract_known": contract is not None,
        "policy_digest": policy_digest_value,
        "checks": [], "regex_signals": [], "decision": "allow", "reasons": [],
    }
    if contract is None:
        result["checks"] = [{"field": "tool", "rule": "known_contract",
                             "value": tool, "ok": False,
                             "detail": "没有登记的工具契约"}]
        result["reasons"].append("未知工具契约：%s" % tool)
        result["decision"] = "deny" if mode == "enforce" else "report"
        return result

    result["checks"] = _checks(agent, tool, args, contract, pol)
    failed = [c for c in result["checks"] if not c["ok"]]
    result["reasons"] = ["%s.%s 不合规（%s）" % (c["field"], c["rule"], c["detail"])
                         for c in failed]

    # 正则只作信号：命中也只记录，不改变结构化授权结论。
    try:
        result["regex_signals"] = sorted({f["code"] for f in
                                          security.scan(json.dumps(args,
                                                                   ensure_ascii=False))})
    except Exception as e:                       # noqa: BLE001
        result["regex_error"] = "%s: %s" % (type(e).__name__, e)

    if failed:
        result["decision"] = "deny" if mode == "enforce" else "report"
    else:
        result["decision"] = "allow"
    result["regex_only"] = bool(result["regex_signals"]) and not failed
    result["structural_only"] = bool(failed) and not result["regex_signals"]
    result["note"] = ("授权由结构化契约决定；正则命中只是附加信号，"
                      "不承担授权决策（推进计划 §5）")
    return result
