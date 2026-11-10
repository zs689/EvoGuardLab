# -*- coding: utf-8 -*-
"""代码安全检查：把「生成出来的代码」当成需要防的东西。

对照 LlamaFirewall 的 CodeShieldScanner（`scanners/code_shield_scanner.py`）：
它把文本丢给 8 种语言的 Semgrep 规则、有任何 issue 就 BLOCK。这里学它三件事：
  1. **按 CWE 分类**（SQL 注入 / 命令注入 / 反序列化 / 弱哈希 / 路径穿越 / 硬编码凭据）；
  2. **语言感知**：Python 走 AST（精确到调用与关键字参数），其它语言走正则；
  3. **只扫代码块**，不把普通中文段落当代码扫 —— 上游对所有语言全量扫，
     跨语言会互相误报，这是刻意不学的部分。

不依赖 semgrep / torch / 任何第三方包，纯标准库。
"""
import ast
import re

LANG_ALIASES = {
    "py": "python", "python": "python", "python3": "python",
    "js": "javascript", "javascript": "javascript", "ts": "javascript",
    "typescript": "javascript", "jsx": "javascript", "tsx": "javascript",
    "sql": "sql", "sh": "shell", "bash": "shell", "shell": "shell",
    "html": "html",
}

CODE_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\s*\n(.*?)```", re.S)

# 语言特征：用于没写围栏时判断「这到底是不是代码」
_HINTS = {
    "python": (re.compile(r"^\s*(?:def|class|import|from|async def)\s+\w", re.M),),
    "javascript": (re.compile(r"\b(?:const|let|var|function|=>)\b"),),
    "sql": (re.compile(r"(?i)\b(?:select|insert|update|delete|drop)\b\s"),),
    "shell": (re.compile(r"^\s*(?:curl|wget|sudo|chmod|rm|bash|sh)\s", re.M),),
    "html": (re.compile(r"<\s*(?:html|body|div|script|iframe)\b", re.I),),
}

CWE = {
    78: "命令注入", 89: "SQL 注入", 94: "代码注入（eval/exec）",
    502: "不可信反序列化", 327: "弱哈希", 377: "不安全的临时文件",
    798: "硬编码凭据", 22: "路径穿越", 79: "XSS", 95: "代码注入（eval）",
}


def _f(code, cwe, severity, line, match, detail, fp):
    return {"code": code, "cwe": cwe, "cwe_name": CWE.get(cwe, ""),
            "severity": severity, "line": line, "match": str(match)[:120],
            "detail": detail, "false_positive": fp}


def detect_language(text, hint=None):
    if hint and hint.lower() in LANG_ALIASES:
        return LANG_ALIASES[hint.lower()]
    best, best_hits = None, 0
    for lang, pats in _HINTS.items():
        hits = sum(1 for p in pats if p.search(text))
        if hits > best_hits:
            best, best_hits = lang, hits
    if best_hits:
        return best
    # 没有明显特征时再用 AST 兜底：只有赋值/调用/导入这类节点才算代码。
    # 不这么做会漏掉「password = "..."」这种只有一行的片段（实测漏过），
    # 而纯文本（中文句子、单词）解析会 SyntaxError，不会被误判。
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    code_nodes = (ast.Call, ast.Assign, ast.AnnAssign, ast.Import,
                  ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                  ast.ClassDef, ast.With, ast.Try)
    return "python" if any(isinstance(n, code_nodes)
                          for n in ast.walk(tree)) else None


def split_blocks(text):
    """抽出 ```lang 围栏里的代码块；没有围栏就整段当候选。"""
    blocks = [(m.group(1), m.group(2)) for m in CODE_FENCE.finditer(text or "")]
    if blocks:
        return blocks
    return [(None, text or "")]


# ---------------- Python：AST 精确检查 ----------------
_DANGEROUS_CALLS = {
    ("os", "system"): (78, "error", "os.system 直接执行字符串命令"),
    ("os", "popen"): (78, "error", "os.popen 执行字符串命令"),
    ("pickle", "loads"): (502, "error", "pickle.loads 反序列化不可信数据"),
    ("marshal", "loads"): (502, "error", "marshal.loads 反序列化不可信数据"),
    ("hashlib", "md5"): (327, "warn", "MD5 已不适合做安全哈希"),
    ("hashlib", "sha1"): (327, "warn", "SHA1 已不适合做安全哈希"),
    ("tempfile", "mktemp"): (377, "warn", "mktemp 有竞态风险，改用 mkstemp"),
}
_BARE_DANGEROUS = {
    "eval": (94, "error", "eval 执行任意表达式"),
    "exec": (94, "error", "exec 执行任意代码"),
    "compile": (94, "warn", "compile 可配合 exec 执行动态代码"),
}
_SECRET_NAMES = re.compile(
    r"(?i)^(?:password|passwd|pwd|secret|token|api_key|apikey|access_key|"
    r"private_key|client_secret)$")


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def check_python(code, line_offset=0):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [_f("code_unparsable", 0, "info", (e.lineno or 0) + line_offset,
                   "SyntaxError", "代码块无法解析，跳过 AST 检查（不当作通过）",
                   "截断的代码片段会命中，属正常")]
    out = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0) + line_offset
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            hit = _DANGEROUS_CALLS.get(tuple(name.split(".")))
            if hit:
                cwe, sev, detail = hit
                out.append(_f("code_" + name.replace(".", "_"), cwe, sev, line,
                              name, detail, "示例代码/教学文本会命中"))
            bare = name.split(".")[-1] if name else ""
            if name in _BARE_DANGEROUS:
                cwe, sev, detail = _BARE_DANGEROUS[name]
                out.append(_f("code_" + name, cwe, sev, line, name, detail,
                              "讲 eval 的文章会命中"))
            if name in ("subprocess.run", "subprocess.call",
                        "subprocess.Popen", "subprocess.check_output"):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is True:
                        out.append(_f("code_subprocess_shell", 78, "error", line,
                                      name + "(shell=True)",
                                      "shell=True 会把参数拼进 shell，易被注入",
                                      "固定命令字面量时风险较低"))
            if bare == "load" and _dotted(node.func).startswith("yaml"):
                if not any(k.arg == "Loader" for k in node.keywords) and \
                        not node.args[1:]:
                    out.append(_f("code_yaml_load", 502, "error", line,
                                  "yaml.load", "yaml.load 默认 Loader 可构造任意对象",
                                  "新版 PyYAML 已收紧默认 Loader，但仍建议显式 SafeLoader"))
            if bare in ("execute", "executemany") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.BinOp) or isinstance(arg, ast.JoinedStr) or \
                        (isinstance(arg, ast.Call) and _dotted(arg.func) == ""
                         and isinstance(arg.func, ast.Attribute)
                         and arg.func.attr == "format"):
                    out.append(_f("code_sql_concat", 89, "error", line,
                                  "execute(" + type(arg).__name__ + ")",
                                  "SQL 语句由拼接/格式化构造，参数未参数化",
                                  "拼接的是常量时无风险"))
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and _SECRET_NAMES.match(tgt.id) \
                        and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str) \
                        and len(node.value.value) >= 8:
                    out.append(_f("code_hardcoded_secret", 798, "error", line,
                                  "%s = <string>" % tgt.id,
                                  "硬编码凭据写进代码",
                                  "占位符/示例值会命中，需人工确认"))
    return out


# ---------------- 其它语言：正则 ----------------
_OTHER = [
    ("javascript", "code_js_child_process", 78, "error",
     re.compile(r"\bchild_process\b[^\n]{0,40}\b(?:exec|execSync|spawnSync)\s*\("),
     "child_process 执行命令", "脚手架代码会命中"),
    ("javascript", "code_js_eval", 95, "error",
     re.compile(r"(?<![\w.])eval\s*\(|new\s+Function\s*\("),
     "eval / new Function 执行动态代码", "模板引擎内部实现会命中"),
    ("javascript", "code_js_inner_html", 79, "warn",
     re.compile(r"\.(?:innerHTML|outerHTML)\s*=|dangerouslySetInnerHTML"),
     "直接写入 HTML，可能引入 XSS", "写入的是固定常量时无风险"),
    ("javascript", "code_js_document_write", 79, "warn",
     re.compile(r"document\.write\s*\("), "document.write 可被注入", "老代码常见"),
    ("sql", "code_sql_concat", 89, "error",
     re.compile(r"(?i)\b(?:select|insert|update|delete)\b[^\n;]{0,160}"
                r"(?:\+\s*[A-Za-z_]\w*|\|\|\s*[A-Za-z_]\w*|\$\{)"),
     "SQL 由拼接/插值构造", "拼接的是常量时无风险"),
    ("sql", "code_sql_no_where", 89, "warn",
     re.compile(r"(?i)\b(?:delete\s+from|update)\s+\w+\s*(?:;|$)(?![\s\S]*\bwhere\b)"),
     "UPDATE/DELETE 没有 WHERE 条件", "清库脚本会命中"),
    ("shell", "code_shell_pipe_sh", 78, "error",
     re.compile(r"\b(?:curl|wget)\b[^\n|]{0,120}\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"),
     "下载内容直接交给 shell 执行", "安装脚本会命中"),
    ("shell", "code_shell_rm_rf", 78, "error",
     re.compile(r"\brm\s+-[rf]{1,2}\s+/(?=\s|$|\*)"), "删除根目录",
     "讲安全的文章会命中"),
    ("html", "code_html_script", 79, "warn",
     re.compile(r"<\s*script\b", re.I), "内联脚本", "正常页面也会命中"),
    ("html", "code_html_onerror", 79, "error",
     re.compile(r"\son(?:error|load)\s*=", re.I), "内联事件处理器", "前端代码会命中"),
    ("html", "code_html_iframe", 79, "warn",
     re.compile(r"<\s*iframe\b", re.I), "内嵌框架可被钓鱼", "嵌视频时正常"),
]


_JS_CHILD_PROCESS = re.compile(
    r"require\s*\(\s*['\"]child_process['\"]\s*\)"
    r"|from\s+['\"]child_process['\"]")
_JS_BARE_EXEC = re.compile(r"(?<![\w.])(?:exec|execSync|spawnSync)\s*\(")


def check_other(code, language, line_offset=0):
    out = []
    for lang, code_id, cwe, sev, pat, detail, fp in _OTHER:
        if lang != language:
            continue
        for m in pat.finditer(code):
            line = code.count("\n", 0, m.start()) + 1 + line_offset
            out.append(_f(code_id, cwe, sev, line, m.group(0)[:80], detail, fp))
    # 解构后的裸 exec()/execSync() 只有确认引了 child_process 才算命令执行，
    # 否则 `re.exec(str)` 这类正常用法会被大量误报。
    if language == "javascript" and _JS_CHILD_PROCESS.search(code):
        for m in _JS_BARE_EXEC.finditer(code):
            line = code.count("\n", 0, m.start()) + 1 + line_offset
            out.append(_f("code_js_child_process_alias", 78, "error", line,
                          m.group(0)[:80],
                          "从 child_process 解构出的 exec/execSync 执行命令",
                          "命令是固定字面量时风险较低"))
    return out


def scan_code(text, language=None):
    """扫代码块，返回按 (line, code) 排序的发现列表。"""
    out = []
    for hint, block in split_blocks(text):
        if not block.strip():
            continue
        lang = detect_language(block, hint)
        if lang is None:
            continue                        # 不像代码就不扫，避免把中文段落误报
        offset = text[:text.find(block)].count("\n") if block in text else 0
        if lang == "python":
            out.extend(check_python(block, offset))
        else:
            out.extend(check_other(block, lang, offset))
    # 去重（同 code + 同 match + 同行）
    seen, uniq = set(), []
    for f in sorted(out, key=lambda x: (x["line"], x["code"])):
        key = (f["code"], f["match"], f["line"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


def worst_severity(findings):
    if any(f["severity"] == "error" for f in findings):
        return "error"
    if any(f["severity"] == "warn" for f in findings):
        return "warn"
    return "info" if findings else "none"
