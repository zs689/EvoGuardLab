# -*- coding: utf-8 -*-
"""统一的轨迹模型：不管原始格式是 OTel、LangSmith 还是本项目的 SQLite，
先全部转成这一个结构，规则层只认它。

为什么要这层：规则写死了某一种格式，就等于只支持一种 Agent。
把「格式差异」全部挡在 loader 里，规则才能共用。
"""
import json


class Step(object):
    """一步执行。kind 只有四种：chat（模型）/ tool（工具）/ context / other。"""

    __slots__ = ("seq", "kind", "name", "tool", "args", "result", "status",
                 "error", "tokens", "ts", "duration_ms", "raw")

    def __init__(self, seq, kind, name, tool=None, args=None, result=None,
                 status="ok", error=None, tokens=0, ts=None, duration_ms=0,
                 raw=None):
        self.seq = seq
        self.kind = kind
        self.name = name
        self.tool = tool
        self.args = args
        self.result = result
        self.status = status
        self.error = error
        self.tokens = tokens
        self.ts = ts
        self.duration_ms = duration_ms or 0
        self.raw = raw or {}

    def text(self):
        """把 result 压成可搜索的文本（数字溯源规则要用）。"""
        if self.result is None:
            return ""
        if isinstance(self.result, str):
            return self.result
        return json.dumps(self.result, ensure_ascii=False)

    def __repr__(self):
        return "<Step %s %s %s>" % (self.seq, self.kind, self.name)


class Trace(object):
    """一次完整执行。"""

    __slots__ = ("trace_id", "agent", "task", "steps", "answer", "meta", "source")

    def __init__(self, trace_id, agent="unknown", task="", steps=None,
                 answer=None, meta=None, source=""):
        self.trace_id = trace_id
        self.agent = agent
        self.task = task
        self.steps = steps or []
        self.answer = answer
        self.meta = meta or {}
        self.source = source

    # --- 便捷查询（规则层主要用这些） ---
    def of_kind(self, kind):
        return [s for s in self.steps if s.kind == kind]

    def tools(self):
        return self.of_kind("tool")

    def chats(self):
        return self.of_kind("chat")

    def tool_names(self):
        return [s.tool or s.name for s in self.tools()]

    def find_tool(self, name):
        return [s for s in self.tools() if (s.tool or s.name) == name]

    def __len__(self):
        return len(self.steps)

    def __repr__(self):
        return "<Trace %s agent=%s steps=%d>" % (self.trace_id, self.agent, len(self.steps))


KIND_CHAT, KIND_TOOL = "chat", "tool"
