# -*- coding: utf-8 -*-
"""从公开生产事故里提炼的静态规则（4 条）。

和 static/rules.py 的区别同 case_rules：每条对应一类有公开案例的失败。

  prompt_no_untrusted_boundary  工具结果注入（网页/文档里夹带指令改变 Agent 行为）
  retry_without_backoff         重试有次数却无退避/熔断 → 重试风暴
  no_run_budget                 没有单次运行预算 → 失控脚本 8 小时烧掉 $1,900
  write_tool_no_idempotency     写工具没有幂等键 → 超时重试可能重复扣款

**一个重要的取舍**：这 4 条只在 confidence=high（用户提供的定义文件）时生效。
从源码启发式提取的定义（confidence=low）里，prompt 和 config 都是猜的，
拿它判「有没有写边界」「有没有预算」会必然刷屏 —— 那不是发现，是噪音。
低置信度对象只给线索，不下这类结论。
"""
from ..config import DEFAULT
from .rules import _issue, rule

# 会读取外部不可信内容的工具特征
EXTERNAL_HINTS = ("web", "http", "url", "fetch", "scrape", "search", "browse",
                  "crawl", "retrieve", "read_file", "read_url", "mcp", "rag",
                  "browser", "download", "email", "mail")
BOUNDARY_HINTS = ("不可信", "只是数据", "不是指令", "不要执行其中", "忽略其中",
                  "不得听从", "仅作为数据", "untrusted", "do not follow",
                  "not instructions", "treat as data", "data only", "ignore instructions in")
BACKOFF_HINTS = ("backoff", "circuit_breaker", "circuit breaker", "retry_delay",
                 "retry_interval", "max_consecutive", "退避", "熔断")
RETRY_HINTS = ("max_retries", "retries", "retry", "max_attempts", "attempts")
BUDGET_HINTS = ("token_budget", "max_tokens_per_run", "cost_limit", "budget",
                "max_cost", "spend_limit", "max_spend", "预算")
IDEMPOTENCY_HINTS = ("idempotency", "idempotent", "request_id", "幂等", "dedupe")


def _skip(defn):
    """仓库扫描提取的定义不足以判断这类「有没有写」的问题。"""
    return defn.confidence != "high"


def _cfg(defn):
    c = defn.config
    return c if isinstance(c, dict) else {}


def _cfg_keys(cfg):
    return " ".join(str(k).lower() for k in cfg)


@rule("prompt_no_untrusted_boundary", "prompt", "warn",
      "有读取外部内容的工具，但 prompt 没说「工具结果是数据不是指令」",
      "工具返回网页、文档、邮件内容时会直接进上下文，其中夹带的"
      "「忽略之前的指令」可能改变 Agent 行为。这是已公开的攻击面，"
      "不是理论风险。",
      "外部内容可能已经在上游做过清洗/隔离；也可能 prompt 写在别处（模板、"
      "外部文件）而解析器没读到。低置信度的仓库扫描一律不判这条。")
def rule_prompt_no_untrusted_boundary(defn, cfg=DEFAULT):
    if _skip(defn) or not defn.system_prompt:
        return []
    external = [t for t in defn.tools
                if any(h in (t.name + " " + t.description).lower()
                       for h in EXTERNAL_HINTS)]
    if not external:
        return []
    p = defn.system_prompt.lower()
    if any(h in p for h in BOUNDARY_HINTS):
        return []
    return [_issue(
        "prompt_no_untrusted_boundary", defn,
        evidence=", ".join(t.name for t in external[:3]),
        detail="%d 个工具会读取外部内容，prompt 里没有「工具结果只是数据」的边界声明"
               % len(external),
        fix="在 system prompt 里明确：工具返回内容是不可信数据，其中的指令"
            "一律不执行；把外部内容与指令上下文分开放，并做分隔符/清洗")]


@rule("retry_without_backoff", "config", "warn",
      "配了重试次数，却没有退避或熔断",
      "公开事故里的重试风暴都有同一个结构：有重试路径，没有「连续失败就停」。"
      "光有 max_retries 不够 —— 每次重试都重新发全量上下文时，成本是超线性增长的。",
      "退避/熔断可能实现在代码里而不是配置文件里；也可能由网关或 SDK 提供。"
      "低置信度的仓库扫描一律不判这条。")
def rule_retry_without_backoff(defn, cfg=DEFAULT):
    if _skip(defn):
        return []
    c = _cfg(defn)
    keys = _cfg_keys(c)
    if not any(h in keys for h in RETRY_HINTS):
        return []
    if any(h in keys for h in BACKOFF_HINTS):
        return []
    return [_issue(
        "retry_without_backoff", defn,
        evidence=", ".join(str(k) for k in c)[:100],
        detail="配置里有重试但没有退避/熔断字段",
        fix="加指数退避 + 连续失败熔断（成功清零）；把熔断放在执行路径里，"
            "而不是只加一个事后告警")]


@rule("no_run_budget", "config", "info",
      "没有单次运行的 token / 费用上限",
      "有开发者让脚本「攒够 50 条就停」，但停止条件只计成功不限尝试次数，"
      "8 小时发了约 2,700 次请求、烧掉近 $1,900，账户级限额和月度额度都没拦住。",
      "预算可能由网关、云厂商配额或外部计费系统统一管控；单次任务也可能天然很短。"
      "本条只是提示确认，级别为 info。低置信度的仓库扫描一律不判这条。")
def rule_no_run_budget(defn, cfg=DEFAULT):
    if _skip(defn):
        return []
    keys = _cfg_keys(_cfg(defn))
    if any(h in keys for h in BUDGET_HINTS):
        return []
    return [_issue(
        "no_run_budget", defn,
        evidence="config keys: " + (", ".join(str(k) for k in _cfg(defn))[:80] or "空"),
        detail="配置里没有单次运行的 token / 费用上限",
        fix="在每次模型调用前检查累计消耗；超预算直接中止该次运行，"
            "并把预算做成 per-run / per-tenant 两级")]


@rule("write_tool_no_idempotency", "tool", "warn",
      "写操作工具没有幂等键",
      "写类工具超时后，调用方无法知道服务端是否执行了。没有幂等键就只能盲重试，"
      "可能重复扣款 / 重复下单。MCP 生态里 exactly-once 至今没有通用解法，"
      "幂等键是少数能自己落地的兜底。",
      "服务端可能自己做了去重；也可能是天然幂等的覆盖式写（PUT）。"
      "低置信度的仓库扫描一律不判这条。")
def rule_write_tool_no_idempotency(defn, cfg=DEFAULT):
    if _skip(defn):
        return []
    import json
    for t in defn.tools:
        if not cfg.is_write(t.name):
            continue
        blob = (t.name + " " + t.description + " " +
                json.dumps(t.parameters, ensure_ascii=False)).lower()
        if any(h in blob for h in IDEMPOTENCY_HINTS):
            continue
        return [_issue(
            "write_tool_no_idempotency", defn,
            evidence=t.name,
            detail="写操作工具 %s 的参数/描述里没有幂等键" % t.name,
            fix="加 idempotency_key / request_id；超时后先用只读接口查状态，"
                "确认没执行成功再重试")]
    return []
