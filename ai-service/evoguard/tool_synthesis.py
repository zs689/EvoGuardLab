# -*- coding: utf-8 -*-
"""EVO-DEEP-01-T2：受控纯函数微工具合成（Tool Synthesis）。

第一性原理：Agent 反复手写同一类确定性小逻辑（解析签名游标、ISO 时间归一化）
既浪费 token 又容易写错。把它们固化成**纯函数**复用是对的，但"让模型自己
往系统里塞可执行代码"是巨大的攻击面。所以三道闸必须同时存在：

1. **AST 白名单**（安全边界）：只允许极少数内置与 re/json/math/datetime/
   hashlib；禁止一切 I/O、网络、进程、反射、动态执行、类定义、全局状态。
2. **独立沙箱单测**（正确性边界）：至少 3 条正反例，在受限 builtins 下跑通。
3. **注册表上架**（治理边界）：以 read_only / side_effect=none /
   source=synthesized 注册，写工具永远不可能由合成产生。

零第三方依赖。
"""
import ast
import importlib

#: 允许 import 的模块（其余一律拒绝）
ALLOWED_MODULES = frozenset({"re", "json", "math", "datetime", "hashlib"})

#: 允许出现在表达式里的内置（白名单式，不是黑名单）
SAFE_BUILTINS = {
    "len": len, "range": range, "int": int, "float": float, "str": str,
    "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "min": min, "max": max, "sum": sum, "sorted": sorted, "abs": abs,
    "round": round, "enumerate": enumerate, "zip": zip, "any": any,
    "all": all, "isinstance": isinstance, "repr": repr, "ord": ord,
    "chr": chr, "bytes": bytes, "frozenset": frozenset, "map": map,
    "filter": filter,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "AttributeError": AttributeError,
    "Exception": Exception, "UnicodeDecodeError": UnicodeDecodeError,
}

#: 明确禁止的名字（动态执行/反射/交互）
BANNED_NAMES = frozenset({
    "open", "eval", "exec", "compile", "input", "breakpoint", "globals",
    "locals", "vars", "dir", "help", "exit", "quit", "getattr", "setattr",
    "delattr", "hasattr", "memoryview", "__import__", "__builtins__",
    "object", "super", "type", "classmethod", "staticmethod", "property",
})

#: 明确禁止的属性（反射逃逸 / 进程 / 文件）
BANNED_ATTRS = frozenset({
    "system", "popen", "spawn", "spawnl", "spawnv", "fork", "execv", "execve",
    "__class__", "__bases__", "__subclasses__", "__globals__", "__builtins__",
    "__code__", "__closure__", "__dict__", "__getattribute__", "__reduce__",
    "__reduce_ex__", "mro", "__subclasshook__", "load_module", "find_module",
})

#: 禁止的语句类型
BANNED_NODES = (ast.ClassDef, ast.AsyncFunctionDef, ast.AsyncFor,
                ast.AsyncWith, ast.Global, ast.Nonlocal, ast.With)


class ToolSynthesisError(ValueError):
    """合成被拒（AST 越界 / 测试不过 / 命名非法）。"""


def _banned_import(name):
    return str(name or "").split(".")[0] not in ALLOWED_MODULES


def check_source(source, func_name):
    """AST 白名单校验。返回 None 表示通过，否则抛 ToolSynthesisError。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ToolSynthesisError("语法错误：%s" % exc) from exc
    found = []
    for node in ast.walk(tree):
        if isinstance(node, BANNED_NODES):
            found.append("禁止的语句 %s（第 %d 行）"
                         % (type(node).__name__, getattr(node, "lineno", 0)))
        elif isinstance(node, ast.Import):
            for al in node.names:
                if _banned_import(al.name):
                    found.append("禁止导入 %s（第 %d 行）"
                                 % (al.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level or _banned_import(node.module):
                found.append("禁止从 %s 导入（第 %d 行）"
                             % (node.module, node.lineno))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_NAMES:
                found.append("禁止调用 %s（第 %d 行）" % (fn.id, node.lineno))
            if isinstance(fn, ast.Attribute) and fn.attr in BANNED_ATTRS:
                found.append("禁止调用 .%s（第 %d 行）"
                             % (fn.attr, node.lineno))
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS:
                found.append("禁止访问 .%s（第 %d 行）"
                             % (node.attr, node.lineno))
        elif isinstance(node, ast.Name):
            if node.id in ("__builtins__", "__import__", "__loader__",
                           "__spec__"):
                found.append("禁止引用 %s（第 %d 行）"
                             % (node.id, node.lineno))
    defs = [n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef,))]
    if func_name not in defs:
        found.append("源码里找不到函数 %s" % func_name)
    if found:
        raise ToolSynthesisError("；".join(sorted(set(found))))
    return None


def _safe_import(name, *args, **kwargs):
    if _banned_import(name):
        raise ImportError("合成工具禁止导入 %s" % name)
    return importlib.import_module(name)


def run_tests(source, func_name, tests, timeout_s=5):
    """在受限 builtins 下跑正反例。tests=[{"call":"f(1)","expect":2}]。

    `expect_exc` 用于反例：期望抛出的异常类型名。
    """
    if len(tests or []) < 3:
        raise ToolSynthesisError("至少需要 3 条正反例测试（当前 %d）"
                                 % len(tests or []))
    builtins = dict(SAFE_BUILTINS)
    builtins["__import__"] = _safe_import
    ns = {"__builtins__": builtins, "__name__": "synth_tool"}
    try:
        exec(compile(source, "<synthesized>", "exec"), ns)   # noqa: S102
    except Exception as exc:                                # noqa: BLE001
        raise ToolSynthesisError("源码执行失败：%s: %s"
                                 % (type(exc).__name__, exc)) from exc
    fn = ns.get(func_name)
    if not callable(fn):
        raise ToolSynthesisError("函数 %s 未定义或不可调用" % func_name)
    for i, t in enumerate(tests, 1):
        expr = str(t.get("call") or "").strip()
        if not expr:
            raise ToolSynthesisError("第 %d 条测试缺少 call" % i)
        try:
            got = eval(expr, {"__builtins__": builtins, func_name: fn})  # noqa: S307
        except Exception as exc:                            # noqa: BLE001
            want = t.get("expect_exc")
            if want and type(exc).__name__ == want:
                continue
            raise ToolSynthesisError(
                "第 %d 条测试异常：%s: %s" % (i, type(exc).__name__, exc)
            ) from exc
        if "expect_exc" in t:
            raise ToolSynthesisError(
                "第 %d 条测试期望抛 %s，但正常返回 %r"
                % (i, t["expect_exc"], got))
        if "expect" in t and got != t["expect"]:
            raise ToolSynthesisError(
                "第 %d 条测试不符：期望 %r 实际 %r"
                % (i, t["expect"], got))
    return True


def synthesize_tool(name, source, tests, *, registry=None, func_name=None,
                    description="", schema=None, identities=None):
    """校验 -> 沙箱测试 -> 上架。返回 {name, func, contract}。

    拒绝时不注册任何东西（失败即无副作用）。
    """
    name = str(name or "").strip()
    if not name or not name.replace(".", "").replace("_", "").isalnum():
        raise ToolSynthesisError("工具名非法：%r" % name)
    fn_name = func_name or name.split(".")[-1]
    check_source(source, fn_name)
    run_tests(source, fn_name, tests)
    builtins = dict(SAFE_BUILTINS)
    builtins["__import__"] = _safe_import
    ns = {"__builtins__": builtins, "__name__": "synth_tool"}
    exec(compile(source, "<synthesized>", "exec"), ns)       # noqa: S102
    func = ns[fn_name]
    contract = {"read_only": True, "side_effect": "none",
                "source": "synthesized", "description": description[:200]}
    if registry is not None:
        # 必须走 from_json_schema：裸 register 没有参数模型与身份，
        # 真实执行前的结构契约会直接拒绝（<tool>.known_schema 不合规）。
        import tool_contract as _tc
        _tc.register_schema(name, schema)
        registry.from_json_schema(
            name, schema or {"type": "object", "properties": {}},
            scope="read_only", source="synthesized",
            identities=list(identities or ["react"]))
        # 不要再调 registry.register：它会用简化契约**覆盖**上面生成的结构
        # 契约（参数模型/身份全丢），真实执行前的 preflight 必然拒绝。
    return {"name": name, "func": func, "contract": contract}


def describe():
    return {"allowed_modules": sorted(ALLOWED_MODULES),
            "banned_names": sorted(BANNED_NAMES),
            "min_tests": 3,
            "stages": ["ast_gate", "sandbox_tests", "register_read_only"]}
