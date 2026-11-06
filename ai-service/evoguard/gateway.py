# -*- coding: utf-8 -*-
"""模型网关：屏蔽「用 mock 还是用真模型」的差异。

设计要点（和 runtime 的分工）：
  - 网关只负责「发一次请求、拿回一段文本、数 token、分类错误」
  - 它**不**决定答案对不对，也不碰轨迹。轨迹由 runtime 记
  - 默认 mock：没有模型也能跑通全流程，但 mock 返回 None，
    调用方必须自己给出确定性兜底 —— 不允许 mock 假装成"模型想到了"

支持两种后端（都是 OpenAI 兼容协议）：
  LM Studio  http://127.0.0.1:1234/v1   （/v1/chat/completions）
  Ollama     http://127.0.0.1:11434/v1

环境变量：
  AGENTLAB_LLM=mock | local
  AGENTLAB_LLM_BASE=http://127.0.0.1:1234/v1
  AGENTLAB_LLM_MODEL=qwen2.5-4b-instruct
  AGENTLAB_LLM_TIMEOUT=60
"""
import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:1234/v1"

_THINK_RE = re.compile(r"<think(?:ing)?[^>]*>.*?</think(?:ing)?\s*>", re.S)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?[^>]*>.*", re.S)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    """模型调用失败。code 用于错误分类统计，不要吞掉。"""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


def strip_think(text):
    """剥掉 Qwen3 这类模型的思考块。未闭合时整段砍掉。"""
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text.strip()


def extract_json(text):
    """从模型输出里尽力抠出 JSON。抠不出返回 None（调用方走兜底）。"""
    if not text:
        return None
    t = strip_think(text)
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1)
    t = t.strip()
    for candidate in (t, t[t.find("{"):t.rfind("}") + 1] if "{" in t and "}" in t else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


class Provider:
    name = "base"
    is_mock = False

    def complete(self, messages, temperature=0.0, max_tokens=256):
        """返回 (text, tokens, model_id)。失败抛 LLMError。"""
        raise NotImplementedError

    def info(self):
        return {"provider": self.name, "mock": self.is_mock}


class MockProvider(Provider):
    """不产生任何内容。返回 None 是有意的：
    让它"编一句像模像样的话"会把 mock 的假象传染到评测里。"""
    name = "mock"
    is_mock = True

    def complete(self, messages, temperature=0.0, max_tokens=256):
        prompt = messages[-1]["content"] if messages else ""
        tokens = max(1, len(prompt) // 3)      # 粗略估算，只为让成本指标有数
        return None, tokens, "mock"


class LocalProvider(Provider):
    """OpenAI 兼容的本地模型（LM Studio / Ollama）。"""

    name = "local"

    def __init__(self, base=None, model=None, timeout=None, retries=1):
        self.base = (base or os.environ.get("AGENTLAB_LLM_BASE") or DEFAULT_BASE).rstrip("/")
        self.model = model or os.environ.get("AGENTLAB_LLM_MODEL") or None
        self.timeout = int(timeout or os.environ.get("AGENTLAB_LLM_TIMEOUT") or 60)
        self.retries = retries
        self._resolved = None

    # ---------- 底层 ----------
    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            if e.code in (400, 404):
                raise LLMError("E_LLM_MODEL", "模型名可能不对（%s）：%s" % (e.code, body))
            if e.code in (401, 403):
                raise LLMError("E_LLM_AUTH", "接口需要鉴权（%s）" % e.code)
            if e.code in (429,):
                raise LLMError("E_LLM_RATE", "被限流（429）")
            if e.code in (502, 503, 504):
                raise LLMError("E_LLM_DOWN",
                               "端口通但模型服务没起来（%s）。检查模型是否加载成功" % e.code)
            raise LLMError("E_LLM_HTTP", "HTTP %s：%s" % (e.code, body))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if isinstance(e, TimeoutError):
                raise LLMError("E_LLM_TIMEOUT", "等待 %s 秒无响应" % self.timeout)
            raise LLMError("E_LLM_UNREACHABLE",
                           "连不上 %s（%s）。先启动本地模型服务的 OpenAI 兼容接口"
                           % (self.base, getattr(e, "reason", e)))

    def _get(self, path):
        req = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError("E_LLM_UNREACHABLE", "连不上 %s（%s）" % (self.base, e))

    def resolve_model(self):
        if self.model:
            return self.model
        if self._resolved:
            return self._resolved
        try:
            data = self._get("/models")
            ids = [m.get("id") for m in data.get("data", [])]
        except LLMError:
            raise
        if not ids:
            raise LLMError("E_LLM_MODEL", "服务是通的，但没有已加载的模型")
        self._resolved = ids[0]
        return self._resolved

    # ---------- 对外 ----------
    def complete(self, messages, temperature=0.0, max_tokens=256):
        model = self.resolve_model()
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens, "stream": False}
        last = None
        for attempt in range(self.retries + 1):
            try:
                data = self._post("/chat/completions", payload)
                msg = data["choices"][0]["message"]
                text = strip_think(msg.get("content") or "")
                usage = data.get("usage") or {}
                tokens = int(usage.get("total_tokens") or max(1, len(text) // 2))
                return text, tokens, model
            except LLMError as e:
                last = e
                # 只有瞬时类错误值得重试；模型名错、鉴权错重试也没用
                if e.code not in ("E_LLM_TIMEOUT", "E_LLM_RATE", "E_LLM_DOWN"):
                    raise
        raise last


_CACHE = {}


def get_provider(force=None):
    """按环境变量取后端。同一个进程内复用，避免每次新建连接配置。"""
    mode = force or os.environ.get("AGENTLAB_LLM") or "mock"
    if mode in _CACHE:
        return _CACHE[mode]
    p = MockProvider() if mode == "mock" else LocalProvider()
    _CACHE[mode] = p
    return p


def reset_cache():
    _CACHE.clear()
