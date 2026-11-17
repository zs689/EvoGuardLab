# -*- coding: utf-8 -*-
"""工具注册表：把契约、职责、角色权限从硬编码常量变成可动态注册的能力。

为什么要有这一层（外部审查 EG-GEN-001）：
`tool_contract.DEFAULT_CONTRACTS`、`action_scope.TOOL_SCOPE`、
`security.AGENT_TOOLS` 全是手写常量，只覆盖内置的几个模拟工具。
接外部真实 Agent（OpenAPI / MCP / 自定义 JSON Schema）时：
- 参数契约认不出，越界阻断失效；
- 未登记工具被当成 `info` 放行，等于没有约束。

这一层提供：
1. `register()` —— 注册单个工具（契约 + 职责 + 危险标记）；
2. `from_json_schema()` / `from_openapi()` / `from_mcp()` —— 三种标准定义自动生成契约；
3. `declare_agent()` —— 角色 → 工具白名单；
4. **未声明工具的默认策略可配**，默认 `deny`（不是放行）。

零依赖，只用标准库。注册表是进程内单例，但**可导出/导入快照**，
便于多副本共享（见 `snapshot()` / `load_snapshot()`）。
"""
import json
import os
import re

READ_ONLY = "read_only"
WRITE = "write"
EXTERNAL = "external"
UNKNOWN = "unknown"

#: 未声明工具的默认策略：`deny` 视为高危（动作请求直接判越界），
#: `info` 退回旧行为（只记录）。默认 deny —— 静默放行比误报危险。
UNKNOWN_POLICY = (os.environ.get("EVOGUARD_UNKNOWN_TOOL")
                  or os.environ.get("AGENTLAB_UNKNOWN_TOOL")
                  or "deny").strip().lower()

_WRITE_HINT = re.compile(
    r"(create|update|delete|remove|send|exec|settle|transfer|pay|refund|"
    r"grant|revoke|write|upload|post|put|patch|deploy|install)", re.I)
_READ_HINT = re.compile(
    r"(get|list|search|read|view|query|fetch|lookup|describe|navigate)", re.I)

URL_KEYS = ("url", "endpoint", "callback", "webhook")
PATH_KEYS = ("path", "file", "writable_path", "target_path", "filename")
AMOUNT_KEYS = ("amount", "total", "max_amount", "price", "sum")
RESOURCE_KEYS = ("order_id", "resource_id", "doc_id", "id", "key", "name")


class ToolRegistry:
    def __init__(self):
        self._tools = {}       # name -> {"contract":..., "scope":..., "source":...}
        self._agents = {}      # agent_type -> set(tools)

    # ---------------- 注册 ----------------
    def register(self, name, contract=None, scope=None, source="manual"):
        """注册/覆盖一个工具。`scope` 不给就按名字推断。"""
        name = str(name)
        c = dict(contract or {})
        c.setdefault("identities", [])
        c.setdefault("allowed_domains", [])
        c.setdefault("allowed_resource_pattern", None)
        c.setdefault("writable_paths", [])
        c.setdefault("max_amount", None)
        c.setdefault("methods", ["read"] if not _WRITE_HINT.search(name) else ["write"])
        c.setdefault("side_effect",
                     "write" if _WRITE_HINT.search(name) else "none")
        c.setdefault("requires_approval", bool(_WRITE_HINT.search(name)))
        c["_source"] = source
        self._tools[name] = {"contract": c,
                             "scope": scope or _infer_scope(name, c),
                             "source": source}
        return name

    def register_many(self, mapping, scope=None, source="manual"):
        return [self.register(k, v, scope, source) for k, v in mapping.items()]

    def declare_agent(self, agent_type, tools, replace=False):
        if replace:
            self._agents[agent_type] = set(tools)
        else:
            self._agents.setdefault(agent_type, set()).update(tools)
        return sorted(self._agents[agent_type])

    # ---------------- 查询 ----------------
    def get(self, name):
        item = self._tools.get(name)
        return dict(item["contract"]) if item else None

    def scope_of(self, name):
        item = self._tools.get(name)
        return item["scope"] if item else UNKNOWN

    def is_dangerous(self, name):
        c = self.get(name) or {}
        return bool(c.get("requires_approval")
                    or c.get("side_effect") in ("write", "external"))

    def allowed_tools(self, agent_type):
        return set(self._agents.get(agent_type) or ())

    def declared(self):
        return {k: dict(v["contract"], _scope=v["scope"], _source=v["source"])
                for k, v in self._tools.items()}

    def coverage(self):
        """给报告用：登记了多少、按方式分组。"""
        by = {}
        for v in self._tools.values():
            by[v["source"]] = by.get(v["source"], 0) + 1
        return {"total": len(self._tools), "by_source": by,
                "agents": {k: len(v) for k, v in self._agents.items()},
                "unknown_policy": UNKNOWN_POLICY}

    def reset(self):
        self._tools.clear()
        self._agents.clear()

    # ---------------- 快照（多副本共享） ----------------
    def snapshot(self):
        return {"version": 1,
                "tools": {k: {"contract": v["contract"], "scope": v["scope"],
                              "source": v["source"]}
                          for k, v in self._tools.items()},
                "agents": {k: sorted(v) for k, v in self._agents.items()}}

    def load_snapshot(self, data, merge=True):
        if not isinstance(data, dict):
            return 0
        if not merge:
            self.reset()
        n = 0
        for name, item in (data.get("tools") or {}).items():
            self._tools[name] = {"contract": dict(item.get("contract") or {}),
                                 "scope": item.get("scope") or UNKNOWN,
                                 "source": item.get("source") or "snapshot"}
            n += 1
        for agent, tools in (data.get("agents") or {}).items():
            self.declare_agent(agent, tools, replace=True)
        return n

    # ---------------- 适配器 ----------------
    def from_json_schema(self, name, schema, scope=None, source="json_schema"):
        """从 JSON Schema 生成契约。

        能从 schema 拿到的：参数键（→ URL/PATH/AMOUNT/RESOURCE 归类）、
        是否有额外属性、必填项。拿不到的是「允许的资源 ID 形态」和
        「金额上限」—— 这两项必须由调用方显式传，**不猜**。
        """
        schema = schema or {}
        props = schema.get("properties") or {}
        keys = list(props.keys())
        contract = {
            "allowed_resource_pattern": None,
            "additional_properties": bool(schema.get("additionalProperties", True)),
            "required": list(schema.get("required") or ()),
            "param_keys": keys,
        }
        if any(k in props for k in URL_KEYS):
            scope = scope or EXTERNAL
            contract["side_effect"] = "external"
        elif _WRITE_HINT.search(name):
            scope = scope or WRITE
            contract["side_effect"] = "write"
        else:
            scope = scope or READ_ONLY
            contract["side_effect"] = "none"
        if any(k in props for k in PATH_KEYS):
            contract["writable_paths"] = [k for k in props if k in PATH_KEYS]
        if any(k in props for k in AMOUNT_KEYS):
            contract["max_amount"] = None      # 显式未声明 = 不做金额上限猜测
        self.register(name, contract, scope, source)
        return name

    def from_openapi(self, spec, source="openapi"):
        """从 OpenAPI 3.x 生成工具：`operationId` 或 `METHOD /path` 作名字。"""
        spec = spec or {}
        out = []
        for path, ops in (spec.get("paths") or {}).items():
            for method, op in (ops or {}).items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                name = op.get("operationId") or ("%s %s" % (method.upper(), path))
                body = (((op.get("requestBody") or {}).get("content") or {})
                        .get("application/json") or {}).get("schema") or {}
                scope = READ_ONLY if method.lower() == "get" else WRITE
                self.from_json_schema(name, body, scope=scope, source=source)
                out.append(name)
        return out

    def from_mcp(self, tools, source="mcp"):
        """从 MCP tool 列表生成：`{"name","description","inputSchema"}`。"""
        out = []
        for t in tools or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            schema = t.get("inputSchema") or t.get("input_schema") or {}
            self.from_json_schema(t["name"], schema, source=source)
            out.append(t["name"])
        return out


def _infer_scope(name, contract):
    if contract.get("side_effect") == "external":
        return EXTERNAL
    if contract.get("side_effect") == "write" or _WRITE_HINT.search(name):
        return WRITE
    if _READ_HINT.search(name) or contract.get("side_effect") == "none":
        return READ_ONLY
    return UNKNOWN


#: 进程内单例
REGISTRY = ToolRegistry()


def scope_of(name):
    return REGISTRY.scope_of(name)


def allowed_tools(agent_type):
    return REGISTRY.allowed_tools(agent_type)


def unknown_policy():
    """当前未声明工具策略：deny（默认）/ info。每次读 env，运行期可切。"""
    return (os.environ.get("EVOGUARD_UNKNOWN_TOOL")
            or os.environ.get("AGENTLAB_UNKNOWN_TOOL")
            or UNKNOWN_POLICY).strip().lower()


def unknown_is_dangerous():
    """未声明工具是否按高危处理（默认是）。"""
    return unknown_policy() == "deny"


def describe():
    return REGISTRY.coverage()


def _load_snapshot_from_env():
    p = os.environ.get("EVOGUARD_TOOL_REGISTRY") or os.environ.get(
        "AGENTLAB_TOOL_REGISTRY")
    if p and os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return REGISTRY.load_snapshot(json.load(f), merge=True)
        except (OSError, ValueError):
            return 0
    return 0
