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
import math
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
    # AGENT-E2E-01：自主修 Bug 标准工具集。fs.write 是写工具（需审批），
    # writable_paths 用 "*" 表示「任意相对路径，由 SandboxExecutor 围栏兜底」。
    "fs.read": {
        "identities": ["coder"],
        "allowed_domains": [], "allowed_resource_pattern": None,
        "writable_paths": [], "max_amount": None,
        "methods": ["read"], "side_effect": "none",
        "requires_approval": False,
    },
    "fs.write": {
        "identities": ["coder"],
        "allowed_domains": [], "allowed_resource_pattern": None,
        "writable_paths": ["*"], "max_amount": None,
        "methods": ["write"], "side_effect": "write",
        "requires_approval": True,
    },
    "code.run_in_sandbox": {
        "identities": ["coder"],
        "allowed_domains": [], "allowed_resource_pattern": None,
        "writable_paths": [], "max_amount": None,
        "methods": ["read"], "side_effect": "none",
        "requires_approval": False,
    },
}

# 这些参数键按结构化规则核对，不靠正则猜。
URL_KEYS = ("url", "endpoint", "callback")
PATH_KEYS = ("path", "file", "writable_path", "target_path")
AMOUNT_KEYS = ("amount", "total", "max_amount")
RESOURCE_KEYS = ("order_id", "resource_id", "doc_id")

# ---------------- 严格参数模型（评审 R02）----------------
# 历史坑：`_find` 只返回**第一个非空别名**。传 amount=1 / total=20000 时，
# 金额检查只看 amount=1（合规），下游业务读 total=20000（越界）——
# 校验值和执行值不是同一个。这不是「提示不够」，是契约本身没定义清楚。
#
# 现在每个工具声明：必填字段、允许字段、别名组。规则三条：
#   1. 未知字段 → 拒绝（默认不宽容，避免「多塞一个字段改变下游行为」）；
#   2. 别名组内多个字段值不一致 → 拒绝（不许猜哪个才是真金额）；
#   3. 必填缺失 → 拒绝。
# 归一化只做一次（fold_aliases），之后授权、审批、执行消费**同一个对象**。
SCHEMA_VERSION = "tool-schema-v1"

TOOL_SCHEMAS = {
    "kb.search": {"required": ["query"],
                  "fields": ["query", "doc_id", "top_k", "note"]},
    "kb.search_all": {"required": [], "fields": ["query", "doc_id", "note"]},
    "order.get_order": {"required": ["order_id"], "fields": ["order_id", "note"]},
    "order.get_quote_order": {"required": ["order_id"],
                              "fields": ["order_id", "note"]},
    "pricing.calc_total": {"required": ["items", "discount_policy"],
                           "fields": ["items", "discount_policy", "note"]},
    "pricing.quote": {"required": ["items"], "fields": ["items", "note"]},
    "calc.apply_rules": {"required": ["items"], "fields": ["items", "note"]},
    "crm.tier": {"required": ["account"], "fields": ["account", "note"]},
    "task.make_plan": {"required": ["goal_key"], "fields": ["goal_key", "note"]},
    "order.settle": {
        "required": ["order_id", "total"],
        # 金额四兄弟：只允许一个语义，值必须一致，统一折成 canonical 名 total
        "aliases": {"total": ["amount", "total", "max_amount", "payable"]},
        "fields": ["order_id", "total", "amount", "max_amount", "payable",
                   "approval_request_id", "candidate_version",
                   "idempotency_key", "method", "note"],
    },
    "http.send": {"required": ["url"],
                  "fields": ["url", "method", "payload", "approval_request_id",
                             "candidate_version", "idempotency_key"]},
    "shell.exec": {"required": ["cmd"],
                   "fields": ["cmd", "cwd", "approval_request_id"]},
    # AGENT-E2E-01：自主修 Bug 标准工具集
    "fs.read": {"required": ["path"], "fields": ["path"]},
    "fs.write": {"required": ["path", "content"],
                 "fields": ["path", "content", "approval_request_id"]},
    "code.run_in_sandbox": {"required": ["code"], "fields": ["code"]},
}


def _norm_scalar(v):
    """只为「值是否一致」服务：数字按数值比，字符串按原样比。"""
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, (int, float)):
        return ("num", float(v))
    return ("str", str(v))


def validate_args(tool, args, *, strict=True):
    """严格参数校验。返回 [{field, rule, value, ok, detail}]。

    strict=False 时未知字段只报告不判死（给历史工具留出口），
    但别名冲突**永远**是错误 —— 那是「同一语义两个值」，没有兼容的解释空间。
    """
    out = []
    schema = TOOL_SCHEMAS.get(tool)
    if not isinstance(args, dict):
        return [{"field": "<args>", "rule": "must_be_object", "value":
                 type(args).__name__, "ok": False, "detail": "参数必须是对象"}]
    if schema is None:
        return [{"field": "<tool>", "rule": "known_schema", "value": tool,
                 "ok": False, "detail": "没有登记参数模型，无法判断参数是否合法"}]

    # 1) 必填：按**归一化之后**的对象判断 —— 传 amount 而模型里叫 total 是兼容的，
    #    真正的冲突由下面第 2 条单独判死。
    folded, _ = fold_aliases(tool, args)
    for k in schema.get("required") or []:
        ok = k in folded and folded[k] not in (None, "")
        out.append({"field": k, "rule": "required", "value": str(folded.get(k)),
                    "ok": ok, "detail": "必填字段"})

    # 2) 别名冲突：同一语义多个字段、值不一致 → 拒绝
    for canon, group in (schema.get("aliases") or {}).items():
        present = [(k, args[k]) for k in group
                   if k in args and args[k] not in (None, "")]
        if len(present) > 1:
            values = {_norm_scalar(v) for _, v in present}
            ok = len(values) == 1
            out.append({"field": ",".join(k for k, _ in present),
                        "rule": "alias_conflict",
                        "value": ", ".join("%s=%s" % (k, v) for k, v in present),
                        "ok": ok,
                        "detail": ("同一语义的多个字段取值不一致，拒绝猜哪一个是真值"
                                   if not ok else "多别名取值一致，可归一")})

    # 3) 未知字段
    allowed = set(schema.get("fields") or [])
    if allowed:
        unknown = sorted(k for k in args if k not in allowed)
        if unknown:
            out.append({"field": ",".join(unknown), "rule": "unknown_field",
                        "value": ",".join(unknown), "ok": not strict,
                        "detail": "未在参数模型中声明" +
                                  ("" if strict else "（非严格模式：仅报告）")})
    return out


def fold_aliases(tool, args):
    """把别名折成 canonical 名，返回 (新对象, 是否改过)。

    只在**校验通过**之后调用。冲突时不要指望它替你选一个 —— 冲突已经该被拒。
    """
    schema = TOOL_SCHEMAS.get(tool) or {}
    aliases = schema.get("aliases") or {}
    if not isinstance(args, dict) or not aliases:
        return dict(args) if isinstance(args, dict) else args, False
    out, changed = dict(args), False
    for canon, group in aliases.items():
        src = [k for k in group if k in out and out[k] not in (None, "")]
        if not src:
            continue
        if len({_norm_scalar(out[k]) for k in src}) > 1:
            # 冲突时**不归一**：宁可保持原样也不替调用方选一个值。
            # 选错一个方向就是把越界金额洗成合规金额。
            continue
        out[canon] = out[src[0]]
        changed = True
    return out, changed


def _find(args, keys):
    for k in keys:
        if k in args and args[k] not in (None, ""):
            return k, args[k]
    return None, None


def _amount_of(tool, args):
    """金额字段：canonical 名排最前，其余别名按模型声明顺序。

    评审 R02 的反例是 `amount=1, total=20000` 时只看了 amount。
    现在冲突本身会被 `alias_conflict` 判死；这里再按 canonical 优先取一次，
    保证即使将来放宽冲突判定，检查的也仍然是契约认定的那个字段。
    """
    schema = TOOL_SCHEMAS.get(tool) or {}
    order = list(AMOUNT_KEYS)
    for canon, group in (schema.get("aliases") or {}).items():
        order = [canon] + [k for k in order if k != canon] + [
            k for k in group if k not in order and k != canon]
    seen, uniq = set(), []
    for k in order:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return _find(args, tuple(uniq))


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

    # 3) 可写路径白名单（只对**写**工具检查；只读工具的 path 不是写目标）
    key, path = _find(args, PATH_KEYS)
    if key and contract.get("side_effect") == "write":
        paths = contract.get("writable_paths") or []
        # AGENT-E2E-01：编码工具写的是工作目录内的相对文件名（mod.py 等），
        # 无法预置精确值。约定 writable_paths 含 "*" 表示「任意相对路径，
        # 由 SandboxExecutor 的 realpath 围栏兜底」——契约层只确认写工具
        # 已带审批，路径越界由沙箱执行器拒绝，职责不重叠。
        ok = (str(path) in paths) or ("*" in paths)
        add(key, "writable_path", path, ok,
            "允许可写路径：%s" % (paths or "无"))

    # 4) 最大金额：优先看 canonical 名，避免「另一个别名更大却没被检查」
    key, amount = _amount_of(tool, args)
    if key:
        cap = contract.get("max_amount")
        try:
            val = float(amount)
            # NaN/-inf 用比较会被「<=」放过去；负数金额本身也不合法。
            ok = (cap is not None and math.isfinite(val) and val >= 0
                  and val <= float(cap))
        except (TypeError, ValueError):
            ok = False
        add(key, "max_amount", amount, ok,
            "上限：%s；金额必须是有限非负数" % ("N/A" if cap is None else cap))

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
              policy=None, policy_digest_value=None, strict=True):
    """返回结构化授权结论。`regex_signals` 只是附加信号，不参与 decision。

    `verified_args` 是**归一化之后**的对象：授权、审批摘要、真实执行都用它，
    不许下游再从原始 args 里另挑一套字段（评审 R02 的根因）。
    """
    table = contracts or DEFAULT_CONTRACTS
    pol = policy or {"allowed_tools": [], "write_tools": ["order.settle"]}
    contract = table.get(tool)
    if mode not in ("off", "audit", "enforce"):
        mode = "audit"        # 未知模式按默认档处理，不能静默变成放行
    verified, folded = fold_aliases(tool, args)
    result = {
        "version": CONTRACT_VERSION, "agent": agent, "tool": tool,
        "schema_version": SCHEMA_VERSION,
        "mode": mode, "contract_known": contract is not None,
        "policy_digest": policy_digest_value,
        "checks": [], "regex_signals": [], "decision": "allow", "reasons": [],
        "verified_args": verified, "aliases_folded": folded,
    }
    if mode == "off":
        # 对照档：不做检测、不下判定。allow 只表示「没拦」，不表示「合规」。
        result["detection"] = False
        result["note"] = ("off 为对照档，未执行任何结构化检查；"
                          "allow 仅表示未拦截，不代表调用合规")
        return result
    result["detection"] = True
    if contract is None:
        result["checks"] = [{"field": "tool", "rule": "known_contract",
                             "value": tool, "ok": False,
                             "detail": "没有登记的工具契约"}]
        result["reasons"].append("未知工具契约：%s" % tool)
        result["decision"] = "deny" if mode == "enforce" else "report"
        return result

    # 参数模型先行：未知字段 / 别名冲突 / 缺必填，必须先判掉，
    # 否则后面的「金额只看第一个别名」这类错误还会重演。
    arg_checks = validate_args(tool, args, strict=strict)
    result["checks"] = arg_checks + _checks(agent, tool, verified, contract, pol)
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


# ---- 把内置契约登记进动态注册表（外部工具可继续 register）----
try:
    from . import tool_registry as _registry
except ImportError:                                    # sys.path 直插的旧用法
    import tool_registry as _registry                  # type: ignore

for _name, _contract in DEFAULT_CONTRACTS.items():
    _registry.REGISTRY.register(_name, _contract, source="builtin")
