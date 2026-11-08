# -*- coding: utf-8 -*-
"""Agent 定义：把「不运行也能拿到的东西」结构化。

来源三种，都归一到这一个模型：
  1. 定义文件（JSON）—— prompt / tools / config，最可靠；
  2. 代码仓库—— 从源码里启发式提取（langchain / autogen / crewai 等），可靠性低，标 low；
  3. 手写—— 用户照着填。

为什么单独建模：静态规则要能跨来源复用。规则只看 AgentDef，
不关心它来自 OpenAI 的 JSON 还是 LangChain 的 .py。
"""
import json
import re


class ToolDef(object):
    __slots__ = ("name", "description", "parameters", "raw")

    def __init__(self, name, description="", parameters=None, raw=None):
        self.name = name or ""
        self.description = description or ""
        self.parameters = parameters or {}
        self.raw = raw or {}

    @property
    def required(self):
        return list((self.parameters or {}).get("required") or [])

    @property
    def props(self):
        return dict((self.parameters or {}).get("properties") or {})


class AgentDef(object):
    __slots__ = ("name", "system_prompt", "tools", "config", "source",
                 "source_kind", "confidence", "notes")

    def __init__(self, name="unknown", system_prompt="", tools=None, config=None,
                 source="", source_kind="json", confidence="high", notes=None):
        self.name = name
        self.system_prompt = system_prompt or ""
        self.tools = tools or []
        self.config = config or {}
        self.source = source
        self.source_kind = source_kind      # json / repo / manual
        self.confidence = confidence        # high / low（仓库提取只能算 low）
        self.notes = notes or []

    def tool(self, name):
        return next((t for t in self.tools if t.name == name), None)

    def __repr__(self):
        return "<AgentDef %s tools=%d prompt=%d chars>" % (
            self.name, len(self.tools), len(self.system_prompt))


# ---------------- 从 JSON 定义加载 ----------------
def from_json(doc, source=""):
    """支持两种常见写法：

    A) 我们自己约定的扁平格式：
       {"name":..., "system_prompt":..., "tools":[{name,description,parameters}], "config":{}}
    B) OpenAI Assistants / Chat Completions 风格：
       {"name":..., "instructions":..., "tools":[{"type":"function","function":{...}}]}
    """
    if not isinstance(doc, dict):
        raise ValueError("定义文件必须是 JSON 对象")

    name = doc.get("name") or doc.get("agent_name") or "unknown"
    prompt = (doc.get("system_prompt") or doc.get("instructions")
              or doc.get("system") or doc.get("prompt") or "")

    raw_tools = doc.get("tools") or []
    tools = []
    for t in raw_tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            f = t["function"]
            tools.append(ToolDef(f.get("name"), f.get("description"),
                                 f.get("parameters"), raw=t))
        elif t.get("function") and isinstance(t["function"], dict):
            f = t["function"]
            tools.append(ToolDef(f.get("name"), f.get("description"),
                                 f.get("parameters"), raw=t))
        else:
            tools.append(ToolDef(t.get("name"), t.get("description"),
                                 t.get("parameters") or t.get("input_schema"), raw=t))

    cfg = doc.get("config") or {}
    for k in ("model", "temperature", "timeout", "timeout_s", "max_iterations",
              "max_steps", "retries", "max_retries", "output_schema",
              "require_approval", "approval", "sandbox", "tools_allowed"):
        if k in doc and k not in cfg:
            cfg[k] = doc[k]

    return AgentDef(name=name, system_prompt=prompt, tools=tools, config=cfg,
                    source=source, source_kind="json", confidence="high")


# ---------------- 从代码仓库启发式提取 ----------------
PROMPT_PATTERNS = [
    re.compile(r'(?:SYSTEM_PROMPT|system_prompt|system_message|system)\s*[:=]\s*'
               r'(?:"""|\'\'\')(.+?)(?:"""|\'\'\')', re.S),
    re.compile(r'(?:SystemMessage|HumanMessage)\(\s*(?:content\s*=\s*)?'
               r'(?:"""|\'\'\')(.+?)(?:"""|\'\'\')', re.S),
    re.compile(r'(?:instructions|prompt)\s*[:=]\s*["\']([^"\']{60,})["\']'),
    # --- 框架构造函数写法 ---
    # 第一版只认 `SYSTEM_PROMPT = """..."""`，结果扫 AutoGen 的
    # AssistantAgent(system_message="...") 时一个 prompt 都没拿到。
    # 各框架把 prompt 放在构造参数里，名字还不一样：
    #   OpenAI Agents SDK / PydanticAI : instructions=
    #   AutoGen                        : system_message=
    #   Google ADK                     : instruction=
    #   CrewAI                         : role= / goal= / backstory=
    re.compile(r'(?:system_message|system_prompt|system_instruction|instructions?|'
               r'role|goal|backstory|persona|system|prompt)\s*=\s*'
               r'f?["\']([^"\']{12,600})["\']'),
    re.compile(r'(?:system_message|system_prompt|instructions?|role|goal|backstory)'
               r'\s*=\s*(?:"""|\'\'\')([\s\S]{12,800}?)(?:"""|\'\'\')', re.S),
    # 多行拼接：system_prompt=( "You are ..." "Be concise." )
    re.compile(r'(?:system_message|system_prompt|instructions?)\s*=\s*'
               r'\(([\s\S]{0,900}?)\)'),
]
# 允许 `@agent.tool` / `@mcp.tool` / `@tool_plain` 这类带前缀的装饰器。
# 第一版只认 `@tool`，PydanticAI 的示例因此 0 个工具被提取。
TOOL_DECORATOR = re.compile(
    r"@(?:[\w.]*\.)?(?:tool|function_tool|tool_plain|tool_\w+)\b")
# 装饰器 + def 的签名（不含函数体），desc 单独再找，避免一个巨型正则里嵌套引号
TOOL_DEC_NAME = re.compile(
    r"@(?:[\w.]*\.)?(?:tool|function_tool|tool_plain|tool_\w+)"
    r"(?:\([^)]*\))?[\s\S]{0,200}?def\s+(\w+)\s*\(")
TOOL_DOCSTRING = re.compile(
    r"(?:\"\"\"([\s\S]{0,600}?)\"\"\"|'''([\s\S]{0,600}?)''')")
TOOL_DEF_JSON = re.compile(r'"name"\s*:\s*"([\w.\-]+)"\s*,\s*"description"\s*:\s*"([^"]{0,300})"')


def from_repo_files(files, source=""):
    """files: [(相对路径, 文本)]。启发式提取，**置信度一律 low**。

    为什么不吹高置信度：从源码里猜 prompt 边界必然漏、必然错。
    静态检查的结论要标清「基于猜出来的定义」，否则用户会当成事实。
    """
    prompts, tools, notes = [], [], []
    for path, text in files:
        for pat in PROMPT_PATTERNS:
            for m in pat.finditer(text):
                # 新模式可能有多个捕获组，取第一个非空的。
                raw = (next((g for g in m.groups() if g), "") or "").strip()
                # 括号拼接块（system_prompt=( "a" "b" )）：把里面的字面量连起来，
                # 否则一整段带引号的源码会被当成 prompt 文本。
                if raw.count('"') + raw.count("'") >= 4:
                    parts = re.findall(r'["\']([^"\']{6,})["\']', raw)
                    if len(parts) > 1:
                        raw = " ".join(parts)
                if len(raw) >= 20:   # 20 字以上才当 prompt，避免把短常量误认为提示词
                    prompts.append((path, raw))
        if TOOL_DECORATOR.search(text):
            # 必须连 docstring 一起抓。只抓函数名会让「工具没有描述」这条规则
            # 在真实仓库里大面积误报 —— 那是提取器的缺陷，不是被检对象的问题（踩过）。
            got_desc = 0
            for m in TOOL_DEC_NAME.finditer(text):
                name = m.group(1)
                desc = ""
                dm = TOOL_DOCSTRING.search(text, m.end())
                if dm and dm.start() - m.end() < 400:
                    desc = (dm.group(1) or dm.group(2) or "").strip()
                if desc:
                    got_desc += 1
                tools.append(ToolDef(name, description=desc, raw={"file": path}))
            if not got_desc:
                notes.append("%s: 发现 @tool 装饰器，但没读到 docstring" % path)
        for m in TOOL_DEF_JSON.finditer(text):
            tools.append(ToolDef(m.group(1), m.group(2), raw={"file": path}))

    # 同一工具名去重
    seen, uniq = set(), []
    for t in tools:
        if t.name and t.name not in seen:
            seen.add(t.name)
            uniq.append(t)

    prompt = max((p for _, p in prompts), key=len, default="")
    if not prompt:
        notes.append("没有提取到 system prompt（可能写在别处，或不是字面量）")
    return AgentDef(name=(source or "repo").split("/")[-1], system_prompt=prompt,
                    tools=uniq, config={}, source=source, source_kind="repo",
                    confidence="low", notes=notes)


def dump(defn):
    return {
        "name": defn.name,
        "system_prompt": defn.system_prompt,
        "tools": [{"name": t.name, "description": t.description,
                   "parameters": t.parameters} for t in defn.tools],
        "config": defn.config,
    }
