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
  AGENTLAB_LLM=mock | local | ollama | ollama_raw
  AGENTLAB_LLM_BASE=http://127.0.0.1:1234/v1   （local 用 /v1，ollama 用同主机 /api）
  AGENTLAB_LLM_MODEL=qwen3:4b
  AGENTLAB_LLM_TIMEOUT=60

后端选择（实测结论，2026-11-07，Ollama 0.40.15 + qwen3:4b）：
  local      OpenAI 兼容 /v1/chat/completions。Qwen3 默认思考，单次 ~14s，
             reasoning 长度不稳定，max_tokens 给少时 content 会空 -> E_LLM_THINKING。
  ollama     原生 /api/chat + think:false + format=json。单次 ~0.6s，三类题型
             100% 严格 JSON。grammar 约束会从模型侧消除「自然产生的格式违规」，
             所以用它跑评测时必须如实声明这一取舍（见 real_model 报告）。
  ollama_raw 原生 /api/chat + think:false，**不加** format=json。
             用来做「无 grammar 约束」对照，测模型自然格式违规率。
"""
import json
import os
import re
import threading
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


def extract_json_strict(text):
    """严格版：整个回答必须就是一个 JSON 对象。

    为什么要严格：宽松版会从"好的，我来回答：{...}"这种散文里把 JSON 抠出来，
    于是"没按格式输出"这件事永远测不出来 —— 而 TRAIL 的实测数据说，
    格式违规/指令不依从占真实 agent 错误的约 42%，是最大的一类。
    """
    if not text:
        return None
    t = strip_think(text).strip()
    if t.startswith("```"):
        t = _FENCE_RE.sub(r"\1", t).strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        return json.loads(t)
    except Exception:
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

    def list_models(self):
        """列出已加载的模型。缺这个方法时调用方只能猜模型名，实测会直接崩。"""
        data = self._get("/models")
        return [m.get("id") for m in data.get("data", [])]

    def resolve_model(self):
        if self.model:
            return self.model
        if self._resolved:
            return self._resolved
        ids = self.list_models()
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
                # 思考型模型（Qwen3 等）在 token 预算不足时会把预算全花在 reasoning 上，
                # content 变成空串。这种情况必须明确报出来，否则会被当成"模型什么都没说"。
                if not text and (msg.get("reasoning") or "").strip():
                    raise LLMError("E_LLM_THINKING",
                                   "模型把 token 全用在思考上，content 为空。把 max_tokens 调大（>=600）。")
                usage = data.get("usage") or {}
                tokens = int(usage.get("total_tokens") or max(1, len(text) // 2))
                return text, tokens, model
            except LLMError as e:
                last = e
                # 只有瞬时类错误值得重试；模型名错、鉴权错重试也没用
                if e.code not in ("E_LLM_TIMEOUT", "E_LLM_RATE", "E_LLM_DOWN"):
                    raise
        raise last


class OllamaNativeProvider(Provider):
    """Ollama 原生 /api/chat：能真正关掉 Qwen3 的思考模式。

    为什么不能用 OpenAI 兼容层：实测 `think:false` 在 /v1 上被忽略，
    单次 14~22s 且 reasoning 长度波动；原生 /api/chat 的 think:false 生效。
    单靠 think:false 还不够 —— 模型会把思考过程写进 content，
    必须叠加 format=json 的 grammar 约束，它才只能输出 JSON（实测 0.5s）。

    json_mode=False 时故意不加 grammar，用来测「模型自然格式违规率」。
    """

    def __init__(self, base=None, model=None, timeout=None,
                 json_mode=True, num_predict=800):
        self.base = (base or os.environ.get("AGENTLAB_LLM_BASE")
                     or "http://127.0.0.1:11434/v1").rstrip("/")
        # /v1 只是配置习惯，原生接口在根路径 /api/* 上
        if self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self.model = model or os.environ.get("AGENTLAB_LLM_MODEL") or None
        self.timeout = int(timeout or os.environ.get("AGENTLAB_LLM_TIMEOUT") or 60)
        self.json_mode = json_mode
        self.num_predict = num_predict
        self.name = "ollama" if json_mode else "ollama_raw"
        self._resolved = None

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
            if e.code in (502, 503, 504):
                raise LLMError("E_LLM_DOWN", "模型服务没起来（%s）" % e.code)
            raise LLMError("E_LLM_HTTP", "HTTP %s：%s" % (e.code, body))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if isinstance(e, TimeoutError):
                raise LLMError("E_LLM_TIMEOUT", "等待 %s 秒无响应" % self.timeout)
            raise LLMError("E_LLM_UNREACHABLE",
                           "连不上 %s（%s）" % (self.base, getattr(e, "reason", e)))

    def _get(self, path):
        req = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError("E_LLM_UNREACHABLE", "连不上 %s（%s）" % (self.base, e))

    def list_models(self):
        data = self._get("/api/tags")
        return [m.get("name") for m in data.get("models", [])]

    def resolve_model(self):
        if self.model:
            return self.model
        if self._resolved:
            return self._resolved
        ids = self.list_models()
        if not ids:
            raise LLMError("E_LLM_MODEL", "服务是通的，但没有已加载的模型")
        self._resolved = ids[0]
        return self._resolved

    def complete(self, messages, temperature=0.0, max_tokens=256):
        model = self.resolve_model()
        payload = {
            "model": model, "messages": messages, "stream": False,
            "think": False,
            # num_predict 是**上限**不是目标：grammar 约束下模型写完 JSON 就 EOS，
            # 实测 ~43 字符 / 0.5s。这里刻意不跟随调用方的 max_tokens=1500，
            # 否则配置被悄悄放大，评测记录的参数就和实际不一致了。
            "options": {"temperature": temperature,
                        "num_predict": self.num_predict},
        }
        if self.json_mode:
            payload["format"] = "json"
        data = self._post("/api/chat", payload)
        msg = data.get("message") or {}
        text = strip_think(msg.get("content") or "")
        tokens = int(data.get("eval_count") or 0) + int(data.get("prompt_eval_count") or 0)
        return text, tokens or max(1, len(text) // 2), model


_CACHE = {}


class ResilientProvider(Provider):
    """熔断 + 有界并发 + 排队上限（EG-014）。

    熔断器和超时预算早就在 `enterprise/resilience.py` 里，但**没接进真正
    被请求调用的 provider** —— 下游挂了，请求照样打过去，一直打到超时。
    这里把它包在实际调用链上。

    为什么默认只在服务进程开（`app.py` 启动时设 `AGENTLAB_LLM_RESILIENCE=1`）：
    离线评测要的是**真实失败率**，如果评测中途熔断，会把「模型不行」误记成
    「被自己的熔断器挡住」，那是对实验数据的污染。评测脚本不设这个变量。
    """

    def __init__(self, inner, breaker=None, max_concurrency=4,
                 queue_timeout=2.0, name=None):
        self.inner = inner
        self.name = name or getattr(inner, "name", type(inner).__name__)
        if breaker is None:
            from enterprise import resilience as R
            breaker = R.CircuitBreaker(
                failure_threshold=int(
                    os.environ.get("AGENTLAB_CB_THRESHOLD") or 5),
                cooldown_s=float(os.environ.get("AGENTLAB_CB_COOLDOWN") or 30))
        self.breaker = breaker
        self._sem = threading.Semaphore(max(1, int(max_concurrency)))
        self.queue_timeout = float(queue_timeout)
        self.stats = {"ok": 0, "failed": 0,
                      "rejected_open": 0, "rejected_busy": 0}

    def complete(self, messages, temperature=0.0, max_tokens=256):
        if not self._sem.acquire(timeout=self.queue_timeout):
            self.stats["rejected_busy"] += 1
            raise LLMError("E_OVERLOADED",
                           "模型调用并发已满，排队 %.1fs 超时"
                           % self.queue_timeout)
        try:
            if not self.breaker.allow():
                self.stats["rejected_open"] += 1
                raise LLMError("E_CIRCUIT_OPEN",
                               "下游连续失败，熔断打开（%s）"
                               % self.breaker.state)
            try:
                out = self.inner.complete(
                    messages, temperature=temperature,
                    max_tokens=max_tokens)
            except Exception:
                self.breaker.record(False)
                self.stats["failed"] += 1
                raise
            self.breaker.record(True)
            self.stats["ok"] += 1
            return out
        finally:
            self._sem.release()

    def snapshot(self):
        d = dict(self.stats)
        d["breaker"] = self.breaker.snapshot()
        d["inner"] = type(self.inner).__name__
        return d


def get_provider(force=None):
    """按环境变量取后端。同一个进程内复用，避免每次新建连接配置。"""
    mode = force or os.environ.get("AGENTLAB_LLM") or "mock"
    if mode in _CACHE:
        return _CACHE[mode]
    if mode == "mock":
        p = MockProvider()
    elif mode == "ollama":
        p = OllamaNativeProvider(json_mode=True)
    elif mode == "ollama_raw":
        p = OllamaNativeProvider(json_mode=False)
    else:
        p = LocalProvider()
    _CACHE[mode] = _maybe_resilient(p, mode)
    return _CACHE[mode]


def _maybe_resilient(provider, mode):
    """服务进程显式开启；评测脚本不开，避免熔断污染真实失败率。"""
    flag = (os.environ.get("AGENTLAB_LLM_RESILIENCE") or "").strip().lower()
    if flag not in ("1", "true", "on"):
        return provider
    if isinstance(provider, ResilientProvider):
        return provider
    return ResilientProvider(
        provider,
        max_concurrency=int(os.environ.get("AGENTLAB_LLM_MAX_CONCURRENCY") or 4),
        queue_timeout=float(os.environ.get("AGENTLAB_LLM_QUEUE_TIMEOUT") or 2.0),
        name=mode)


def reset_cache():
    _CACHE.clear()
