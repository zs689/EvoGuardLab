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
import os
import concurrent.futures as _cf
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
#: 禁止的语句类型。`While` 一并禁掉：进程内的纯 Python 死循环**无法可靠
#: 中断**（线程持 GIL + 占满 CPU，executor 的 finally 也会被拖住，实测卡到
#: 被外部 SIGTERM）。合成的纯函数微工具不需要 while，for 足够表达。
BANNED_NODES = (ast.ClassDef, ast.AsyncFunctionDef, ast.AsyncFor,
                ast.AsyncWith, ast.While, ast.Global, ast.Nonlocal, ast.With)


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
            if isinstance(fn, ast.Attribute) and (fn.attr.startswith("_")
                                                  or fn.attr in BANNED_ATTRS):
                found.append("禁止调用 .%s（第 %d 行）"
                             % (fn.attr, node.lineno))
            if isinstance(fn, ast.Name) and fn.id == "range":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) \
                            and isinstance(arg.value, int) \
                            and arg.value > 100000:
                        found.append("range 上界过大（第 %d 行）"
                                     % node.lineno)
                    if isinstance(arg, ast.BinOp) \
                            and isinstance(arg.op, ast.Pow):
                        found.append("range 上界含幂运算（第 %d 行）"
                                     % node.lineno)
        elif isinstance(node, ast.Attribute):
            # HARD-01-T1：不再靠黑名单猜危险属性 —— 任何下划线开头
            # 属性一律拒（dunder / 私有引用 / 模块内部逃逸都在这里断）。
            if node.attr.startswith("_") or node.attr in BANNED_ATTRS:
                found.append("禁止访问 .%s（第 %d 行）"
                             % (node.attr, node.lineno))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            # HARD-01-T1：幂运算右操作数必须是小字面量，否则 10**(10**8)
            # 这种常数炸弹会在沙箱里直接 OOM。
            r = node.right
            if isinstance(r, ast.Constant) and isinstance(r.value, int) \
                    and r.value > 1000:
                found.append("幂指数过大（第 %d 行）" % node.lineno)
            elif not isinstance(r, ast.Constant):
                found.append("幂指数必须是小整数字面量（第 %d 行）"
                             % node.lineno)
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



def check_test_call(expr):
    """HARD-01-T1：tests[].call 只许「函数名(纯字面量参数)」。

    禁止属性访问/下标/推导式/lambda/布尔运算等任何可逃逸语法。
    """
    expr = str(expr or "").strip()
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ToolSynthesisError("测试表达式语法错误：%s" % exc) from exc
    body = tree.body
    if not isinstance(body, ast.Call) or not isinstance(body.func, ast.Name):
        raise ToolSynthesisError("测试 call 必须是 函数名(字面量参数)")
    if body.keywords:
        raise ToolSynthesisError("测试 call 不允许关键字参数")
    for arg in ast.walk(body):
        # 负数字面量（-1）是合法纯参数：UnaryOp 只允许 +/- 包 Constant
        unary_ok = (isinstance(arg, ast.UnaryOp)
                    and isinstance(arg.op, (ast.USub, ast.UAdd))
                    and isinstance(arg.operand, ast.Constant)
                    and isinstance(arg.operand.value, (int, float)))
        banned = (ast.Attribute, ast.Subscript, ast.Lambda,
                  ast.ListComp, ast.DictComp, ast.SetComp,
                  ast.GeneratorExp, ast.BinOp, ast.BoolOp,
                  ast.IfExp, ast.NamedExpr, ast.Starred)
        if (isinstance(arg, banned)
                or (isinstance(arg, ast.UnaryOp) and not unary_ok)):
            raise ToolSynthesisError(
                "测试 call 含禁止的语法：%s" % type(arg).__name__)
        if isinstance(arg, ast.Call) and arg is not body:
            raise ToolSynthesisError("测试 call 不允许嵌套调用")
        if isinstance(arg, ast.Name) and arg is not body.func:
            raise ToolSynthesisError("测试 call 只允许被测函数名")
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                and len(arg.value) > 4096:
            raise ToolSynthesisError("测试字面量过长")
    return body.func.id


def _safe_import(name, *args, **kwargs):
    if _banned_import(name):
        raise ImportError("合成工具禁止导入 %s" % name)
    return importlib.import_module(name)


def _run_tests_child(source, func_name, tests):
    """子进程 worker：受限 builtins 下 exec 源码 + 逐条 AST 校验过的 call。"""
    builtins = dict(SAFE_BUILTINS)
    builtins["__import__"] = _safe_import
    ns = {"__builtins__": builtins, "__name__": "synth_tool"}
    exec(compile(source, "<synthesized>", "exec"), ns)       # noqa: S102
    fn = ns.get(func_name)
    if not callable(fn):
        raise ToolSynthesisError("函数 %s 未定义或不可调用" % func_name)
    scope = {"__builtins__": builtins, func_name: fn}
    for i, t in enumerate(tests, 1):
        expr = str(t.get("call") or "").strip()
        check_test_call(expr)                 # 子进程内再校验一次（纵深）
        try:
            got = eval(expr, scope)            # noqa: S307
        except Exception as exc:               # noqa: BLE001
            want = t.get("expect_exc")
            if want and type(exc).__name__ == want:
                continue
            raise ToolSynthesisError(
                "第 %d 条测试异常：%s: %s"
                % (i, type(exc).__name__, exc)) from exc
        if "expect_exc" in t:
            raise ToolSynthesisError(
                "第 %d 条测试期望抛 %s，但正常返回 %r"
                % (i, t["expect_exc"], got))
        if "expect" in t and got != t["expect"]:
            raise ToolSynthesisError(
                "第 %d 条测试不符：期望 %r 实际 %r"
                % (i, t["expect"], got))
    return True


#: 子进程 bootstrap（OSS-02：不再写 _synth_child.py 到包目录）。
#: 用 `python -I -c BOOTSTRAP`：-I 隔离用户 site-packages 与 PYTHONPATH，
#: 只把**包目录本身**加进 sys.path 以 import 纯校验函数；零临时文件，
#: 非 editable 安装 / 只读 site-packages 下也能跑。
#: OSS-02：模块化的静态子进程入口（见 _synth_child.py）。
#: bootstrap 只做 sys.path 注入 + import；-I 隔离用户 site-packages/PYTHONPATH。
#: 绝不在运行期写包目录。
_CHILD_BOOTSTRAP = (
    "import sys;sys.path.insert(0,HACK_HERE);"
    "import _synth_child;_synth_child.main()"
)


def build_child_env():
    """OSS-02：子进程最小环境白名单——彻底剥离宿主机密钥/token。

    合成工具即便用合法 import 间接读到 os.environ，也拿不到任何凭据。
    Windows 必需 SYSTEMROOT/WINDIR，否则 python 子进程无法启动。
    """
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def run_tests(source, func_name, tests, timeout_s=5):
    """HARD-01-T2：在**独立短命子进程**里跑测试，超时直接 kill。

    第一性原理：Python 线程无法强杀且共享主进程内存/GIL。旧实现用
    ThreadPoolExecutor + wait=False 放弃等待，泄漏线程会继续吃满 CPU
    并拖垮主服务。这里改用 subprocess + stdin/stdout JSON，超时
    proc.kill() 彻底回收，主进程零线程泄漏、零 OOM 风险。
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys
    if len(tests or []) < 3:
        raise ToolSynthesisError("至少需要 3 条正反例测试（当前 %d）"
                                 % len(tests or []))
    # 主进程先做 AST 校验（fail fast），子进程内再校验一次（纵深）
    check_source(source, func_name)
    for i, t in enumerate(tests, 1):
        try:
            check_test_call(str(t.get("call") or ""))
        except ToolSynthesisError as exc:
            raise ToolSynthesisError("第 %d 条测试：%s" % (i, exc)) from exc
    here = os.path.dirname(os.path.abspath(__file__))
    payload = _json.dumps({"source": source, "func": func_name,
                           "tests": tests}, ensure_ascii=False)
    # OSS-02：清洗子进程环境——只留最小必需变量，彻底剥离宿主机 API key /
    # token / EVOGUARD_* 配置。合成工具即便绕过 AST 检查读 os.environ，
    # 也拿不到任何凭据。Windows 必需 SYSTEMROOT，否则子进程起不来。
    child_env = build_child_env()
    try:
        proc = _sp.Popen([_sys.executable, "-I", "-c",
                          _CHILD_BOOTSTRAP.replace("HACK_HERE", repr(here))], cwd=here,
                         stdin=_sp.PIPE, stdout=_sp.PIPE,
                         stderr=_sp.PIPE, text=True, env=child_env)
    except OSError as exc:
        raise ToolSynthesisError("无法启动隔离子进程：%s" % exc) from exc
    try:
        out, _err = proc.communicate(payload, timeout=float(timeout_s))
    except _sp.TimeoutExpired:
        proc.kill()
        proc.communicate()                    # 回收，避免僵尸
        raise ToolSynthesisError(
            "沙箱测试超时（>%ss），已强杀子进程；疑似死循环/阻塞"
            % timeout_s)
    finally:
        if proc.poll() is None:               # 兜底：任何异常路径都杀
            proc.kill()
            proc.communicate()
    try:
        rep = _json.loads((out or "").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ToolSynthesisError(
            "隔离子进程无有效输出(rc=%s, stderr=%s)"
            % (proc.returncode, (_err or "")[-300:])) from exc
    if not rep.get("ok"):
        raise ToolSynthesisError("沙箱测试失败：" + str(rep.get("error")))
    return True


def synthesize_tool(name, source, tests, *, registry=None, func_name=None,
                    description="", schema=None, identities=None,
                    store=None):
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
    if store is not None:
        store.save_synthesized_tool(name, source, fn_name, schema, tests,
                                    version=1)
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



def reload_synthesized(store, registry):
    """HARD-01-T3：从库里重载合成工具，先 sha256 + AST 双重复验。

    数据库被篡改（源码换了但 sha256 没更新）直接拒绝加载，
    不把库写权限等同于代码执行权限。
    """
    import hashlib as _hl
    rows = store.load_synthesized_tools()
    loaded = []
    for rec in rows:
        src = str(rec.get("source") or "")
        got = _hl.sha256(src.encode("utf-8")).hexdigest()
        if got != str(rec.get("source_sha256") or ""):
            raise ToolSynthesisError(
                "合成工具 %s 源码 sha256 不符，拒绝加载（库可能被篡改）"
                % rec.get("name"))
        fn = str(rec.get("func_name") or "")
        check_source(src, fn)                 # AST 重新过一遍白名单
        run_tests(src, fn, rec.get("tests") or [])
        builtins = dict(SAFE_BUILTINS)
        builtins["__import__"] = _safe_import
        ns = {"__builtins__": builtins, "__name__": "synth_tool"}
        exec(compile(src, "<synthesized-reloaded>", "exec"), ns)  # noqa: S102
        import tool_contract as _tc
        _tc.register_schema(rec["name"], rec.get("schema") or {})
        registry.from_json_schema(
            rec["name"], rec.get("schema") or {"type": "object",
                                              "properties": {}},
            scope="read_only", source="synthesized", identities=["react"])
        loaded.append(rec["name"])
    return loaded


def describe():
    return {"allowed_modules": sorted(ALLOWED_MODULES),
            "banned_names": sorted(BANNED_NAMES),
            "min_tests": 3,
            "stages": ["ast_gate", "sandbox_tests", "register_read_only"]}
