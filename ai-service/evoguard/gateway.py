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
import io
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:1234/v1"


def load_dotenv(path=None):
    """零依赖加载项目 .env（KEY=VALUE 逐行解析）。

    只填**尚未设置**的变量：系统环境变量（如 User 级的 DEEPSEEK_API_KEY）
    优先级最高，.env 仅作缺省。返回加载到的键名列表。
    """
    if path is None:
        # 从本文件向上找项目根目录的 .env
        here = os.path.dirname(os.path.abspath(__file__))
        for _ in range(4):
            here = os.path.dirname(here)
            cand = os.path.join(here, ".env")
            if os.path.isfile(cand):
                path = cand
                break
    if not path or not os.path.isfile(path):
        return []
    loaded = []
    try:
        with io.open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and os.environ.get(k) is None:
                    os.environ[k] = v
                    loaded.append(k)
    except OSError:
        pass
    return loaded


# 测试/CI 隔离开发者本地 .env：避免「本机配了云端 provider 但测试进程没
# key」导致测试走真实网络 500。两种隔离途径：
#   1) 显式 EVOGUARD_SKIP_DOTENV=1
#   2) unittest discover -s tests 模式（sys.argv 含 discover）——
#      discover 不会导入 tests/__init__.py，所以必须在这里判断。
_skip_dotenv = (os.environ.get("EVOGUARD_SKIP_DOTENV", "").strip().lower()
                in ("1", "true", "on"))
_in_discover = "discover" in [str(a) for a in sys.argv]
if not _skip_dotenv and not _in_discover:
    load_dotenv()

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
    supports_tools = False

    def complete(self, messages, temperature=0.0, max_tokens=256):
        """返回 (text, tokens, model_id)。失败抛 LLMError。"""
        raise NotImplementedError

    def complete_ex(self, messages, *, tools=None, temperature=0.0,
                    max_tokens=256):
        """带原生 tool_calls 的扩展入口（AGENT-PLUG-01-T2）。

        默认实现向后兼容：不支持原生工具的模型退化为纯文本补全。
        """
        text, tokens, model = self.complete(
            messages, temperature=temperature, max_tokens=max_tokens)
        return ToolCallReply(text, tokens, model, [])

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
            if e.code >= 500:                       # 500 也是服务端错，可重试
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
            if e.code >= 500:                       # 500 也是服务端错，可重试
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


#: 市面主流模型服务的预设档案（模型无关：按协议归类，不绑死厂商）。
#: 用户只需 `EVOGUARD_PROVIDER=<名字>` + 设置对应 KEY 环境变量即可接入；
#: 任何未列出的服务，只要说 OpenAI 兼容协议，用 openai_compat 自填 BASE 即可。
#: 字段：backend / base_url / 默认 key 环境变量名 / 默认模型 / 是否支持原生 tools
PROVIDER_PRESETS = {
    # ---- OpenAI 兼容（Bearer + /v1/chat/completions）----
    "openai":        ("openai_compat", "https://api.openai.com/v1",
                      "OPENAI_API_KEY", "gpt-4o-mini", True),
    "deepseek":      ("openai_compat", "https://api.deepseek.com/v1",
                      "DEEPSEEK_API_KEY", "deepseek-chat", True),
    "qwen":          ("openai_compat", "https://dashscope.aliyuncs.com/compatible-mode/v1",
                      "DASHSCOPE_API_KEY", "qwen-plus", True),
    "glm":           ("openai_compat", "https://open.bigmodel.cn/api/paas/v4",
                      "ZHIPU_API_KEY", "glm-4-flash", True),
    "kimi":          ("openai_compat", "https://api.moonshot.cn/v1",
                      "MOONSHOT_API_KEY", "moonshot-v1-8k", True),
    "groq":          ("openai_compat", "https://api.groq.com/openai/v1",
                      "GROQ_API_KEY", "llama-3.3-70b-versatile", True),
    "openrouter":    ("openai_compat", "https://openrouter.ai/api/v1",
                      "OPENROUTER_API_KEY", "openai/gpt-4o-mini", True),
    "siliconflow":   ("openai_compat", "https://api.siliconflow.cn/v1",
                      "SILICONFLOW_API_KEY", "Qwen/Qwen2.5-7B-Instruct", True),
    "minimax":       ("openai_compat", "https://api.minimax.chat/v1",
                      "MINIMAX_API_KEY", "MiniMax-Text-01", True),
    "baichuan":      ("openai_compat", "https://api.baichuan-ai.com/v1",
                      "BAICHUAN_API_KEY", "Baichuan4", True),
    "volcengine":    ("openai_compat", "https://ark.cn-beijing.volces.com/api/v3",
                      "ARK_API_KEY", "doubao-seed-1-6", True),
    "gemini":        ("openai_compat", "https://generativelanguage.googleapis.com/v1beta/openai",
                      "GEMINI_API_KEY", "gemini-2.0-flash", True),
    "mistral":       ("openai_compat", "https://api.mistral.ai/v1",
                      "MISTRAL_API_KEY", "mistral-small-latest", True),
    "together":      ("openai_compat", "https://api.together.xyz/v1",
                      "TOGETHER_API_KEY", "meta-llama/Llama-3.3-70B-Instruct-Turbo", True),
    "local_openai":  ("openai_compat", "http://127.0.0.1:1234/v1",
                      None, None, False),          # LM Studio / vLLM / llama.cpp
    # ---- Anthropic 原生 Messages 协议 ----
    "anthropic":     ("anthropic_compat", "https://api.anthropic.com/v1",
                      "ANTHROPIC_API_KEY", "claude-3-5-haiku-latest", True),
    # ---- 本地 Ollama（原生 /api/chat，无需 key）----
    "ollama":        ("ollama", "http://127.0.0.1:11434",
                      None, None, False),
}


def list_providers():
    """返回所有预设（供配置向导/文档展示）。"""
    return {k: {"backend": v[0], "base_url": v[1], "api_key_env": v[2],
                "default_model": v[3], "supports_tools": v[4]}
            for k, v in PROVIDER_PRESETS.items()}


class ProviderSpec:
    """AGENT-PLUG-01：一行配置描述一个模型后端的能力档案。

    严禁在 Agent 主逻辑里硬编码模型名或厂商私有参数 —— 私有差异全部收敛
    到这里（`extra_body` / `supports_*`），换模型只改配置不改代码。
    """

    def __init__(self, backend="openai_compat", base_url=None, model=None,
                 api_key_env=None, supports_tools=False,
                 supports_json_mode=False, supports_think_flag=False,
                 extra_body=None, timeout=60):
        self.backend = str(backend)
        self.base_url = base_url
        self.model = model
        self.api_key_env = api_key_env
        self.supports_tools = bool(supports_tools)
        self.supports_json_mode = bool(supports_json_mode)
        self.supports_think_flag = bool(supports_think_flag)
        self.extra_body = dict(extra_body or {})
        self.timeout = int(timeout)

    @classmethod
    def from_env(cls, role=None):
        """按角色读环境变量；`EVOGUARD_PROVIDER=<预设名>` 一行接入主流服务。

        优先级：显式 EVOGUARD_MODEL_* > 预设值 > 全局 AGENTLAB_LLM_* > mock。
        换 key 只需设置预设对应的环境变量，无需改代码或本文件。
        """
        prefix = "EVOGUARD_MODEL_" + role.upper() if role else "AGENTLAB_LLM"
        def _g(k, d=None):
            return os.environ.get(prefix + "_" + k) or d

        preset_name = (os.environ.get("EVOGUARD_PROVIDER") or "").strip().lower()
        pre = PROVIDER_PRESETS.get(preset_name)
        p_backend, p_base, p_keyenv, p_model, p_tools = pre if pre else (
            None, None, None, None, None)
        fallback_backend = (os.environ.get("AGENTLAB_LLM") or "mock").strip()

        # supports_tools：显式 > 预设 > 关（保守，弱模型走 prompt-JSON 不会崩）
        st = _g("SUPPORTS_TOOLS", None)
        if st is None:
            st = "1" if p_tools else "0"
        return cls(
            backend=_g("BACKEND", p_backend or fallback_backend),
            base_url=_g("BASE",
                        p_base or os.environ.get("AGENTLAB_LLM_BASE")),
            model=_g("MODEL", p_model or os.environ.get("AGENTLAB_LLM_MODEL")),
            api_key_env=_g("API_KEY_ENV", p_keyenv),
            supports_tools=str(st).lower() in ("1", "true", "on"),
            supports_json_mode=str(_g("SUPPORTS_JSON", "")).lower()
            in ("1", "true", "on"),
            supports_think_flag=str(_g("SUPPORTS_THINK", "")).lower()
            in ("1", "true", "on"),
            timeout=int(_g("TIMEOUT", 60)),
        )

    def describe(self):
        return {"backend": self.backend, "model": self.model,
                "supports_tools": self.supports_tools,
                "supports_json_mode": self.supports_json_mode,
                "supports_think_flag": self.supports_think_flag}


class ToolCallReply:
    """带原生 tool_calls 的模型回复（AGENT-PLUG-01-T2）。"""

    def __init__(self, text="", tokens=0, model="", tool_calls=None, raw=None):
        self.text = text or ""
        self.tokens = int(tokens or 0)
        self.model = model or ""
        self.tool_calls = list(tool_calls or [])
        self.raw = raw

    def as_tuple(self):
        return self.text, self.tokens, self.model


class OpenAICompatProvider(Provider):
    """通用 OpenAI 兼容后端（OpenAI/DeepSeek/Qwen/vLLM/LM Studio/Ollama /v1）。

    与 LocalProvider 的区别：支持 `tools`/`tool_calls` 原生调用、Bearer 鉴权、
    以及 `extra_body`（厂商私有参数统一放这里，不散落在 Agent 逻辑）。
    """

    name = "openai_compat"

    def __init__(self, spec=None, **kw):
        self.spec = spec or ProviderSpec(**kw)
        self.base = (self.spec.base_url or DEFAULT_BASE).rstrip("/")
        self.model = self.spec.model
        self.timeout = self.spec.timeout
        self.supports_tools = self.spec.supports_tools
        if not self.spec.supports_json_mode:
            # 仅当档案未声明 JSON mode 时，才退化为 prompt-JSON（由 Agent 侧决策）
            pass

    def _headers(self):
        h = {"Content-Type": "application/json"}
        # API Key 解析优先级（换到云端开发时无需改代码）：
        #   1) ProviderSpec.api_key_env 指定的变量名
        #   2) OPENAI_API_KEY_ENV 指定的变量名（间接层）
        #   3) 标准 OPENAI_API_KEY
        # 显式 api_key_env 即"必须鉴权"；未指定则允许无鉴权本地端点
        # （Ollama/LM Studio 的 /v1 不需要 key），兼容老行为。
        env = (self.spec.api_key_env
               or os.environ.get("OPENAI_API_KEY_ENV"))
        key = os.environ.get(env) if env else os.environ.get("OPENAI_API_KEY")
        if key:
            h["Authorization"] = "Bearer " + key
        elif self.spec.api_key_env or os.environ.get("OPENAI_API_KEY_REQUIRED"):
            # 显式要求鉴权却拿不到 key：明确报错，不静默发无鉴权请求吃 401
            raise LLMError("E_LLM_AUTH",
                           "未找到 API Key：请在环境变量设置 %s"
                           % (env or "OPENAI_API_KEY"))
        return h

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                raise LLMError("E_LLM_AUTH", "接口需要鉴权（%s）" % e.code)
            if e.code in (400, 404):
                raise LLMError("E_LLM_MODEL", "模型/端点不对（%s）：%s" % (e.code, body))
            if e.code == 429:
                raise LLMError("E_LLM_RATE", "被限流（429）")
            if e.code >= 500:                       # 500 也是服务端错，可重试
                raise LLMError("E_LLM_DOWN", "模型服务没起来（%s）" % e.code)
            raise LLMError("E_LLM_HTTP", "HTTP %s：%s" % (e.code, body))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if isinstance(e, TimeoutError):
                raise LLMError("E_LLM_TIMEOUT", "等待 %s 秒无响应" % self.timeout)
            raise LLMError("E_LLM_UNREACHABLE",
                           "连不上 %s（%s）" % (self.base, getattr(e, "reason", e)))

    def resolve_model(self):
        return self.model or "gpt-4o-mini"

    def complete(self, messages, temperature=0.0, max_tokens=256):
        return self.complete_ex(messages, temperature=temperature,
                                max_tokens=max_tokens).as_tuple()

    def complete_ex(self, messages, *, tools=None, temperature=0.0,
                    max_tokens=256):
        payload = {"model": self.resolve_model(), "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens,
                   "stream": False}
        payload.update(self.spec.extra_body)
        if tools and self.supports_tools:
            payload["tools"] = tools
        data = self._post("/chat/completions", payload)
        msg = data["choices"][0]["message"]
        text = strip_think(msg.get("content") or "")
        calls = []
        for c in (msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"_raw": fn.get("arguments")}
            calls.append({"id": c.get("id"), "name": fn.get("name"), "args": args})
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or max(1, len(text) // 2))
        return ToolCallReply(text, tokens, self.resolve_model(), calls, data)


class AnthropicCompatProvider(OpenAICompatProvider):
    """Anthropic Messages 兼容后端（/v1/messages）。"""

    name = "anthropic_compat"

    def _headers(self):
        h = {"Content-Type": "application/json",
             "anthropic-version": "2023-06-01"}
        env = self.spec.api_key_env or "ANTHROPIC_API_KEY"
        key = os.environ.get(env) if env else None
        if key:
            h["x-api-key"] = key
        return h

    def complete_ex(self, messages, *, tools=None, temperature=0.0,
                    max_tokens=256):
        # Anthropic 把 system 独立出来，messages 只放 user/assistant。
        system_parts, convo = [], []
        for m in messages:
            if m.get("role") == "system":
                system_parts.append(str(m.get("content") or ""))
            else:
                convo.append({"role": m.get("role"),
                              "content": str(m.get("content") or "")})
        payload = {"model": self.resolve_model(), "messages": convo,
                   "max_tokens": max_tokens, "temperature": temperature}
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        payload.update(self.spec.extra_body)
        if tools and self.supports_tools:
            payload["tools"] = [{"name": t["function"]["name"],
                                 "description": t["function"].get("description", ""),
                                 "input_schema": t["function"].get("parameters", {})}
                                for t in tools]
        data = self._post("/messages", payload)
        text = "".join(b.get("text", "") for b in (data.get("content") or [])
                       if b.get("type") == "text")
        calls = [{"id": b.get("id"), "name": b.get("name"), "args": b.get("input") or {}}
                 for b in (data.get("content") or []) if b.get("type") == "tool_use"]
        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        return ToolCallReply(strip_think(text), tokens or max(1, len(text) // 2),
                             self.resolve_model(), calls, data)


#: 自定义后端注册表（AGENT-PLUG-01-T1）。register_provider(name, factory)。
_CUSTOM_PROVIDERS = {}


def register_provider(name, factory):
    """注册一个自定义后端工厂：factory(spec) -> Provider 实例。"""
    _CUSTOM_PROVIDERS[str(name)] = factory
    return name


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
        # 能力标志必须穿透包装层，否则 Agent 侧 getattr(provider,
        # "supports_tools") 恒为 False，原生 tool_calls 永远走不到。
        self.supports_tools = bool(getattr(inner, "supports_tools", False))
        self.is_mock = bool(getattr(inner, "is_mock", False))
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
                      "rejected_open": 0, "rejected_busy": 0,
                      # EG-014：HALF_OPEN 只放 1 个探测，单独计数可观测，
                      # 便于确认「惊群被挡住」而非熔断失效。
                      "probe_admitted": 0}

    def complete(self, messages, temperature=0.0, max_tokens=256):
        if not self._sem.acquire(timeout=self.queue_timeout):
            self.stats["rejected_busy"] += 1
            raise LLMError("E_OVERLOADED",
                           "模型调用并发已满，排队 %.1fs 超时"
                           % self.queue_timeout)
        try:
            was_half_open = (self.breaker.state == self.breaker.HALF_OPEN \
                             if hasattr(self.breaker, "HALF_OPEN")
                             else self.breaker.state == "half_open")
            if not self.breaker.allow():
                self.stats["rejected_open"] += 1
                raise LLMError("E_CIRCUIT_OPEN",
                               "下游连续失败，熔断打开（%s）"
                               % self.breaker.state)
            if was_half_open:
                self.stats["probe_admitted"] += 1
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

    def complete_ex(self, messages, *, tools=None, temperature=0.0,
                    max_tokens=256):
        """透传原生 tool_calls（熔断/并发包装不能把结构化回复吃掉）。"""
        if not self._sem.acquire(timeout=self.queue_timeout):
            self.stats["rejected_busy"] += 1
            raise LLMError("E_OVERLOADED",
                           "模型调用并发已满，排队 %.1fs 超时"
                           % self.queue_timeout)
        try:
            was_half_open = (self.breaker.state == self.breaker.HALF_OPEN \
                             if hasattr(self.breaker, "HALF_OPEN")
                             else self.breaker.state == "half_open")
            if not self.breaker.allow():
                self.stats["rejected_open"] += 1
                raise LLMError("E_CIRCUIT_OPEN",
                               "下游连续失败，熔断打开（%s）"
                               % self.breaker.state)
            if was_half_open:
                self.stats["probe_admitted"] += 1
            try:
                out = self.inner.complete_ex(
                    messages, tools=tools, temperature=temperature,
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


def get_provider(force=None, *, role=None, spec=None):
    """AGENT-PLUG-01：模型无关热插拔入口。

    - `role`：按角色路由（planner/executor/judge），读 EVOGUARD_MODEL_<ROLE>_*；
      未设时回退默认模型；
    - `spec`：显式给 ProviderSpec（测试/编程式接入）；
    - `force`：后端名（mock/ollama/ollama_raw/local/openai_compat/
      anthropic_compat 或 register_provider 注册的自定义名）。

    只改配置即可换模型，Agent 业务代码零改动。
    """
    sp = spec or ProviderSpec.from_env(role=role)
    mode = (force or sp.backend or "mock").strip()
    # HARD-网关防静默降级：force 传的若是**预设名**（deepseek/qwen/openai…），
    # 必须自动套预设的 backend/base/key/model。否则会落到 else->LocalProvider
    # 连 127.0.0.1:1234，调用失败后判官静默回退规则，报告却写着该厂商——
    # 这是实测踩过的假绿坑（scripts/provider_from_preset.py 有记录）。
    # 显式 spec 里已给的字段优先，不被预设覆盖。
    _pre = PROVIDER_PRESETS.get(mode.lower())
    if _pre and spec is None:
        _be, _base, _keyenv, _model, _tools = _pre
        sp = ProviderSpec(
            backend=_be,
            base_url=sp.base_url or _base,
            model=sp.model or _model,
            api_key_env=sp.api_key_env or _keyenv,
            supports_tools=sp.supports_tools,
            supports_json_mode=sp.supports_json_mode,
            extra_body=sp.extra_body, timeout=sp.timeout)
        mode = _be
    # 缓存键要包含"实际生效的模型/端点"，否则按角色路由会串用别的角色的连接：
    # 同一个 mode 但不同 base/model 必须分开缓存。
    key = "%s|%s|%s|%s" % (mode, sp.base_url or "", sp.model or "",
                           role or "")
    if key in _CACHE:
        return _CACHE[key]

    if mode in _CUSTOM_PROVIDERS:
        p = _CUSTOM_PROVIDERS[mode](sp)
    elif mode == "mock":
        p = MockProvider()
    elif mode == "ollama":
        p = OllamaNativeProvider(json_mode=True,
                                 base=sp.base_url, model=sp.model,
                                 timeout=sp.timeout)
    elif mode == "ollama_raw":
        p = OllamaNativeProvider(json_mode=False,
                                 base=sp.base_url, model=sp.model,
                                 timeout=sp.timeout)
    elif mode == "openai_compat":
        p = OpenAICompatProvider(sp)
    elif mode == "anthropic_compat":
        p = AnthropicCompatProvider(sp)
    else:
        p = LocalProvider(base=sp.base_url, model=sp.model,
                          timeout=sp.timeout)
    _CACHE[key] = _maybe_resilient(p, key)
    return _CACHE[key]


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
