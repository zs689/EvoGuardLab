# -*- coding: utf-8 -*-
"""静态规则（16 条，3 组）：不执行 Agent，只看定义。

能查什么：prompt 缺约束、工具描述缺副作用、写操作没审批、参数无类型、
配置缺超时/重试/上限、密钥硬编码、工具过多导致选错……
查不了什么：Agent 实际会怎么走、会不会真的调错工具 —— 那需要轨迹。

同样的三条硬约束：给证据字段、写清误报、不改数据。
"""
import re

from ..config import DEFAULT

GROUPS = {
    "prompt": "提示词层：模型看不到的边界",
    "tool": "工具定义层：模型选错工具的根源",
    "config": "配置层：超时 / 重试 / 上限 / 输出约束",
}

RULES = {}
ALL_RULES = []

# 关键词要覆盖真实写法。第一版只认「不要编造」，而好定义里写的是
# 「查不到就明说查不到，不要推测或编造」—— 命中不了，把合格的定义也报了。
REFUSAL_HINTS = ("不知道", "编造", "查不到", "无法确定", "不确定", "没有把握",
                 "不能确定", "如实", "do not hallucinate", "don't make up",
                 "if unsure", "not sure", "unable to", "don't know",
                 "cannot answer", "make up")
TOOL_POLICY_HINTS = ("工具", "调用", "tool", "function", "调用前", "查询后")
SECRET_PATTERNS = [
    ("OpenAI key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("AWS key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("通用 token", re.compile(r"(?:api[_-]?key|token|secret)\s*[:=]\s*['\"][^'\"]{12,}['\"]",
                            re.I)),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
PII_PATTERNS = [
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
]
SHELL_HINTS = ("shell", "bash", "exec", "subprocess", "eval", "python_repl",
               "run_code", "execute", "terminal", "cmd")


def rule(rule_id, group, severity, title, why, false_positive, needs_high=False):
    """needs_high=True 表示这条规则只在**显式定义文件**上生效。

    为什么需要：从源码启发式提取的定义（confidence=low）里，prompt / config
    都是猜的。「没在源码里看到 timeout」不等于「运行时没有 timeout」——
    配置可能在框架默认值、环境变量、外部 config 里。实测扫 9 个框架的
    官方示例时，no_timeout 等 4 条规则命中率接近 100%，全是这类噪音。
    命中率接近 100% 的规则是噪音，不是发现。
    """
    if group not in GROUPS:
        raise ValueError("未知分组：%s" % group)
    if not false_positive:
        raise ValueError("规则 %s 缺少误报说明" % rule_id)

    def deco(fn):
        fn._needs_high = bool(needs_high)
        RULES[rule_id] = {"id": rule_id, "group": group, "severity": severity,
                          "title": title, "why": why,
                          "false_positive": false_positive,
                          "needs_high": bool(needs_high), "fn": fn}
        ALL_RULES.append(fn)
        return fn
    return deco


def _issue(rule_id, defn, evidence="", detail="", fix=""):
    m = RULES[rule_id]
    return {
        "rule_id": rule_id, "group": m["group"], "title": m["title"],
        "severity": m["severity"], "agent": defn.name,
        "trace_id": "-", "task": "-",
        "step_seq": None, "step_name": None,
        "evidence": evidence,
        "detail": detail, "fix": fix,
        "false_positive": m["false_positive"],
        "confidence": defn.confidence,
    }


# ============================ 提示词层 ============================
@rule("prompt_missing", "prompt", "error",
      "没有 system prompt",
      "模型只能靠用户输入猜角色与边界，行为不可控。",
      "定义文件里 prompt 写在别处（如代码模板、外部文件），解析器没找到。", needs_high=True)
def rule_prompt_missing(defn, cfg=DEFAULT):
    if (defn.system_prompt or "").strip():
        return []
    return [_issue("prompt_missing", defn, evidence="system_prompt",
                   detail="system prompt 为空或未提取到",
                   fix="至少写清：角色、可做什么、不许做什么、不确定时怎么办")]


@rule("prompt_no_refusal_boundary", "prompt", "warn",
      "prompt 里没有「不知道就说不知道」的约束",
      "缺这条时，模型在拿不到数据的情况下倾向于编一个答案。",
      "约束可能写在别处（工具描述、后置校验、另一个 prompt 片段）。", needs_high=True)
def rule_prompt_no_refusal_boundary(defn, cfg=DEFAULT):
    p = (defn.system_prompt or "").lower()
    if not p:
        return []
    if any(h.lower() in p for h in REFUSAL_HINTS):
        return []
    return [_issue("prompt_no_refusal_boundary", defn, evidence="system_prompt",
                   detail="没有找到任何「不确定/查不到时如何作答」的约束",
                   fix="加一句：拿不到数据就明说查不到，不要推测；给出兜底话术")]


@rule("prompt_no_tool_policy", "prompt", "warn",
      "有工具但 prompt 没说什么时候用",
      "工具怎么选、什么时候必须调用、失败怎么办，全靠模型即兴发挥。",
      "策略可能写在工具描述里（description 里写了「何时使用」）。", needs_high=True)
def rule_prompt_no_tool_policy(defn, cfg=DEFAULT):
    if not defn.tools:
        return []
    p = (defn.system_prompt or "").lower()
    if any(h.lower() in p for h in TOOL_POLICY_HINTS):
        return []
    return [_issue("prompt_no_tool_policy", defn, evidence="system_prompt",
                   detail="声明了 %d 个工具，但 prompt 里没有工具使用规则" % len(defn.tools),
                   fix="写清：什么情况下必须先调工具、拿到什么才能回答、工具失败怎么办")]


@rule("prompt_secret_leak", "prompt", "error",
      "prompt 里疑似硬编码了密钥",
      "prompt 会进入日志、trace、第三方服务 —— 密钥等于泄露。",
      "可能是示例文本、占位符；需要人工确认。")
def rule_prompt_secret_leak(defn, cfg=DEFAULT):
    p = defn.system_prompt or ""
    hits = [name for name, pat in SECRET_PATTERNS if pat.search(p)]
    if not hits:
        return []
    return [_issue("prompt_secret_leak", defn, evidence="system_prompt",
                   detail="疑似密钥：%s" % ", ".join(hits),
                   fix="密钥移到环境变量/密钥管理；prompt 里只放占位符")]


@rule("prompt_pii", "prompt", "error",
      "prompt 里含个人信息",
      "示例数据里的真实手机号/邮箱会被模型复述进输出。",
      "可能是脱敏不彻底或测试数据；需要人工确认。")
def rule_prompt_pii(defn, cfg=DEFAULT):
    if not cfg.check_pii:
        return []
    p = defn.system_prompt or ""
    hits = [name for name, pat in PII_PATTERNS if pat.search(p)]
    if not hits:
        return []
    return [_issue("prompt_pii", defn, evidence="system_prompt",
                   detail="检出：%s" % ", ".join(hits),
                   fix="示例数据换成虚构值；prompt 里不出现真实个人信息")]


# ============================ 工具定义层 ============================
@rule("tool_write_no_approval", "tool", "error",
      "有写操作工具，但没有任何审批/确认机制",
      "模型一句「帮他退款」就能直接改数据，出错不可逆。",
      "写操作的审批在框架层（业务网关、数据库权限），不在 Agent 定义里。", needs_high=True)
def rule_tool_write_no_approval(defn, cfg=DEFAULT):
    writes = [t for t in defn.tools if cfg.is_write(t.name)]
    if not writes:
        return []
    cfg_keys = {k.lower() for k in defn.config}
    has = ("require_approval" in cfg_keys or "approval" in cfg_keys
           or any("approval" in str(v).lower() for v in defn.config.values()))
    if has:
        return []
    return [_issue("tool_write_no_approval", defn,
                   evidence=", ".join(t.name for t in writes[:5]),
                   detail="%d 个写操作工具，配置里没有审批字段" % len(writes),
                   fix="写操作走审批（human-in-the-loop）；至少要幂等键 + 二次确认")]


@rule("tool_desc_missing", "tool", "warn",
      "工具没有描述",
      "描述是模型选工具的唯一依据；没有描述就只能靠名字猜。",
      "有些框架从函数 docstring 生成描述，解析器没读到。", needs_high=True)
def rule_tool_desc_missing(defn, cfg=DEFAULT):
    bad = [t for t in defn.tools if not (t.description or "").strip()]
    if not bad:
        return []
    return [_issue("tool_desc_missing", defn,
                   evidence=", ".join(t.name for t in bad[:6]),
                   detail="%d/%d 个工具没有 description" % (len(bad), len(defn.tools)),
                   fix="每个工具写清：做什么、什么时候用、返回什么、有什么副作用")]


@rule("tool_desc_no_side_effect", "tool", "warn",
      "写操作的描述里没提副作用",
      "模型不知道这一下会改数据，就不会请求确认。",
      "副作用写在参数说明或系统提示里；也可能由框架统一处理审批。", needs_high=True)
def rule_tool_desc_no_side_effect(defn, cfg=DEFAULT):
    bad = []
    for t in defn.tools:
        if not cfg.is_write(t.name):
            continue
        d = (t.description or "").lower()
        if not any(k in d for k in ("写", "创建", "修改", "删除", "提交", "扣款", "退款",
                                    "submit", "create", "delete", "update", "irreversible")):
            bad.append(t.name)
    if not bad:
        return []
    return [_issue("tool_desc_no_side_effect", defn, evidence=", ".join(bad[:6]),
                   detail="写工具的描述里没有副作用说明：%s" % ", ".join(bad[:6]),
                   fix="描述里明确写「会修改数据，不可撤销」，让模型先确认")]


@rule("tool_arg_untyped", "tool", "warn",
      "工具参数没有类型定义",
      "没有 schema 就没法校验，模型传错类型只能等工具报错。",
      "框架可能用运行时签名推断类型（Pydantic / dataclass），解析器没读到。", needs_high=True)
def rule_tool_arg_untyped(defn, cfg=DEFAULT):
    bad = []
    for t in defn.tools:
        props = t.props
        if not props:
            continue
        untyped = [k for k, v in props.items()
                   if not isinstance(v, dict) or "type" not in v]
        if untyped:
            bad.append("%s(%s)" % (t.name, ",".join(untyped[:3])))
    if not bad:
        return []
    return [_issue("tool_arg_untyped", defn, evidence=", ".join(bad[:4]),
                   detail="参数缺 type：%s" % ", ".join(bad[:4]),
                   fix="补全 JSON Schema（type/enum/required），让调用前就能校验")]


@rule("tool_too_many", "tool", "warn",
      "工具数量过多",
      "工具越多，选错的概率越高；也被称为「工具过载」。",
      "工具按业务域分组动态挂载（每次只给相关的几个）时，总数多不是问题。", needs_high=True)
def rule_tool_too_many(defn, cfg=DEFAULT):
    n = len(defn.tools)
    if n <= 15:
        return []
    return [_issue("tool_too_many", defn, evidence="%d 个工具" % n,
                   detail="Tool 数量 %d，超过经验阈值 15" % n,
                   fix="按场景动态挂载子集；或拆成多个专职 Agent")]


@rule("tool_name_collision", "tool", "warn",
      "工具名或描述高度相似",
      "两个工具看起来都能干这件事时，模型会稳定地选错其中一个。",
      "相似是业务本身决定的（如 get_order / get_order_detail），需要描述里写清区别。", needs_high=True)
def rule_tool_name_collision(defn, cfg=DEFAULT):
    names = [t.name.lower() for t in defn.tools if t.name]
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if a == b:
                pairs.append("%s 重名" % a)
            elif a in b or b in a:
                pairs.append("%s / %s" % (a, b))
    if not pairs:
        return []
    return [_issue("tool_name_collision", defn, evidence=", ".join(pairs[:4]),
                   detail="疑似重复工具：%s" % ", ".join(pairs[:4]),
                   fix="重命名或合并；描述里写清两者边界，否则模型只能猜")]


@rule("unsafe_shell_tool", "tool", "error",
      "有执行任意命令/代码的工具，且没有沙箱说明",
      "这类工具被注入一句话就能读文件、发请求、删数据。",
      "工具内部有白名单或沙箱，但没写在定义里；需要人工确认实现。")
def rule_unsafe_shell_tool(defn, cfg=DEFAULT):
    risky = [t for t in defn.tools
             if any(h in (t.name or "").lower() for h in SHELL_HINTS)]
    if not risky:
        return []
    has_box = any("sandbox" in str(k).lower() or "sandbox" in str(v).lower()
                  for k, v in defn.config.items())
    if has_box:
        return []
    return [_issue("unsafe_shell_tool", defn,
                   evidence=", ".join(t.name for t in risky[:4]),
                   detail="存在命令/代码执行工具：%s" % ", ".join(t.name for t in risky[:4]),
                   fix="改成白名单命令；或明确沙箱 + 无网络 + 只读挂载")]


# ============================ 配置层 ============================
@rule("no_timeout", "config", "warn",
      "没有超时配置",
      "一次卡死的工具调用会把整个会话挂住。",
      "超时可能由框架默认值或网关统一设置。", needs_high=True)
def rule_no_timeout(defn, cfg=DEFAULT):
    keys = {k.lower() for k in defn.config}
    if keys & {"timeout", "timeout_s", "timeout_ms", "request_timeout"}:
        return []
    return [_issue("no_timeout", defn, evidence="config",
                   detail="配置里没有 timeout",
                   fix="给工具调用和模型调用分别设超时，并定义超时后的降级")]


@rule("no_retry_policy", "config", "warn",
      "没有重试/失败策略",
      "瞬时故障会直接变成任务失败。",
      "重试由框架或网关负责（如 SDK 默认重试）。", needs_high=True)
def rule_no_retry_policy(defn, cfg=DEFAULT):
    keys = {k.lower() for k in defn.config}
    if keys & {"retries", "max_retries", "retry", "retry_policy", "backoff"}:
        return []
    return [_issue("no_retry_policy", defn, evidence="config",
                   detail="配置里没有 retries / retry_policy",
                   fix="对瞬时错误做有限重试 + 退避；区分可重试与不可重试")]


@rule("no_max_iterations", "config", "warn",
      "没有最大轮次上限",
      "Agent 可能无限自问自答，账单和延迟一起失控。",
      "上限可能在编排层（如 LangGraph 的 recursion_limit）设置。", needs_high=True)
def rule_no_max_iterations(defn, cfg=DEFAULT):
    keys = {k.lower() for k in defn.config}
    if keys & {"max_iterations", "max_steps", "max_turns", "recursion_limit"}:
        return []
    return [_issue("no_max_iterations", defn, evidence="config",
                   detail="配置里没有 max_iterations / max_steps",
                   fix="设硬上限；到达上限要显式失败并说明原因，不要静默截断")]


@rule("no_output_schema", "config", "warn",
      "没有输出结构约束",
      "自由文本输出下游没法消费，格式错误只能靠重试兜。",
      "输出结构由调用方解析时校验（或在 prompt 里用示例强约束）。", needs_high=True)
def rule_no_output_schema(defn, cfg=DEFAULT):
    keys = {k.lower() for k in defn.config}
    if keys & {"output_schema", "response_format", "output_format", "json_mode"}:
        return []
    p = (defn.system_prompt or "").lower()
    if "json" in p or "格式" in p:
        return []
    return [_issue("no_output_schema", defn, evidence="config",
                   detail="没有 output_schema，prompt 里也没提输出格式",
                   fix="用 JSON Schema / response_format 强约束；解析失败要显式报错")]


# ============================ 执行 ============================
def check(defn, cfg=None, rules=None):
    cfg = cfg or DEFAULT
    issues = []
    for fn in (rules or ALL_RULES):
        # 低置信度对象上跳过「缺失型」规则：源码里没读到 ≠ 定义里没有。
        if getattr(fn, "_needs_high", False) and defn.confidence != "high":
            continue
        try:
            issues.extend(fn(defn, cfg) or [])
        except Exception as e:
            issues.append({
                "rule_id": "rule_crashed", "group": "config",
                "title": "规则执行异常", "severity": "info",
                "agent": defn.name, "trace_id": "-", "task": "-",
                "step_seq": None, "step_name": None, "evidence": "",
                "detail": "%s: %s" % (getattr(fn, "__name__", fn), e),
                "fix": "", "false_positive": "检查器自己的 bug",
                "confidence": defn.confidence,
            })
    return issues


def select(groups=None, only=None, skip=None):
    out = []
    for rid, m in RULES.items():
        if groups and m["group"] not in groups:
            continue
        if only and rid not in only:
            continue
        if skip and rid in skip:
            continue
        out.append(m["fn"])
    return out
