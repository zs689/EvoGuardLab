# AgentWatch：监测其他 Agent + 强制防护（2026-11-16）

> 张硕把项目目标重新定义为：「监测其他 agent 运行或未运行时是否有 bug
> 和漏洞，并防止人/AI 入侵攻击被监测的 agent」。
> 本文件记录**已落地的最小可用闭环**，以及**还没做到的部分**。
> 实现：`ai-service/evoguard/agent_watch.py`（独立模块，不改原有
> `security.py` / `runtime.py` / `agents.py` 的既有语义）。

## 一、四个能力

| 能力 | 入口 | 覆盖「运行时」还是「未运行时」 |
|---|---|---|
| 接入任意外部 agent | `AgentTarget.from_callable / from_http / from_manifest` | 两者 |
| 静态漏洞审计 | `static_audit(target)` | **未运行时**：不执行 agent |
| 运行态探针 | `runtime_probe(target)` | **运行时** |
| 强制防护 | `GuardGateway(enforce=True).invoke()` + HTTP 代理 | 运行时 |

### 1. 接入外部 agent

三种方式，被监测对象**不是**仓库内置的三个 Target Agent：

```python
from evoguard import agent_watch as AW

# a) 本地函数（协议：invoke(request) -> dict）
t = AW.AgentTarget.from_callable("my_agent", fn, tools, manifest)

# b) 远端 HTTP agent
t = AW.AgentTarget.from_http("remote", "http://host/invoke", tools, manifest)

# c) 只有声明、不运行 —— 专供静态审计
t = AW.AgentTarget.from_manifest({"name": "...", "tools": [...], ...})
```

协议里 `tool_outputs` 是**不可信的工具返回文本**，会进入 agent 的决策。
外部 agent 读它并据此决定下一步工具 —— 这才是间接注入链存在的前提
（原有内置 agent 的工具序列是硬编码 Python，那条链不存在）。

### 2. 静态审计（不运行）

检查 9 类问题：工具白名单缺失/通配、危险工具无审批、`enforce` 未接线、
输出编码缺失、系统提示未声明不可信边界、声明与实现不一致、
硬编码凭据、工具调用无上限、以及正常任务能力。
返回 `findings` 带 `code/severity/detail/evidence`，`passed` 只表示
「没查到高危项」，**不代表没有漏洞**。

### 3. 运行态探针

四类确定性探针：`normal`、`indirect_injection`、`unauthorized_tool`、
`tool_failure`。判定 `indirect_injection_hijack`、`unauthorized_tool_call`、
`silent_failure`、`normal_task_failed`，并保留**原始 tool_calls** 供复核。

### 4. 强制防护

`GuardGateway` **默认 enforce=True**（与 `security.DEFAULT_MODE=audit` 相反）：
输入扫描 → 工具输出扫描（命中即**不透传**）→ agent 规划 → 逐项授权工具调用
（白名单 + 危险工具审批）→ 全通过才执行。任一 deny 返回
`executed=False` 且**不把 agent 结果透出去**。

`scripts/agent_watch_proxy.py` 是网络层落地：外部流量先过网关再到被监测
agent，`POST /invoke` deny 返回 403 带六字段契约。
`POST /bypass` 默认关闭，只在 `--allow-bypass` 下用于**对照验证**。

## 二、验证证据（可复算）

```bash
python scripts/verify_agent_watch.py --with-http \
  --json docs/review-remediation/probes/agent_watch_verification.json
python -m unittest tests.test_agent_watch -v
```

产物 `probes/agent_watch_verification.json`：**8/8 通过**；
回归测试 `tests/test_agent_watch.py`：**13/13**。

关键对照（证明防护真的生效，不是自说自话）：

| 场景 | 结果 |
|---|---|
| 关掉网关 + 注入载荷 | 被监测 agent **真的**调用了 `wire_transfer`，`executed=True` |
| 开启网关 + 同一载荷 | `executed=False`、`effect=tool_output_stopped`、403 |
| 直连后端 `/bypass` | 攻击成功 |
| 经代理 `/invoke` | 403 拦下 |
| 正常业务 | `call_executed`，未被误伤 |

两个被测 agent 都是外部 callable；一个会读工具输出指令（易受攻击），
一个显式忽略（安全）。静态审计对前者查出 ≥3 项高危、对后者通过；
运行态探针只对前者报警。

顺带修掉了红队清单里的两条已知漏检：
`security._PRE` 的 `injection_tool_directive` / `exfil_encoded_blob`
前置子串写错导致 `调用工具：`、无 `?` 的 `&data=` 漏检；
并把 `injection_tool_directive` 正则扩到「执行脚本:」「运行命令：」形态。
网关层另外用**不依赖 `_PRE` 的全正则扫描**做兜底，防止以后再写错降噪表。

## 三、能力边界（必须读，别拿这段去对外吹）

1. **原有服务默认仍不拦**。这次新增的是独立能力层；
   `ai-service/api/app.py` 走的仍是 `security.DEFAULT_MODE=audit`。
   要让 EvoGuardLab 自己受保护，必须**显式**把 `GuardGateway` 或
   HTTP 代理部署到入口。文档写了不等于已接线。
2. **规则级，不是语义级**。检测是正则 + 白名单 + 声明策略；
   改写/编码/多语言/多轮诱导/语义等价注入仍可能过。
   探针是四条确定性字符串，**不穷尽所有攻击**。
3. **静态审计是配置/声明审计**，不是源码 SAST、符号执行或依赖漏洞扫描；
   不分析字节码、不跑模糊测试。
4. **网关可被部署绕过**：若被监测 agent 还暴露别的入口（直连端口、
   消息队列、本地 CLI），流量不经过代理就不受保护。必须把代理放在唯一入口。
5. **未接真实 LLM 权重做判定**：外部 agent 由调用方提供，本项目没有
   用大模型判断「是否可疑」；语义判定留给调用方。
6. **单机、进程内**：无分布式策略同步、无跨副本限流、无集中审计。
7. **误报代价上升**：扩面后的 `injection_tool_directive` 会把运维类
   「运行命令：」也判 error；网关默认拦截，需按业务调白名单。
8. **未完成生产化**：没有 TLS 终止、mTLS、身份源对接、审计归档、
   灰度/回滚 runbook；`/bypass` 端点仅用于验证，生产禁止开启。

## 四、与原有基准的关系

并列，不替代。原有工作仍是「Does a deny actually stop the tool call?」
的可复现故障恢复基准（v2 负结果归档、v3 预注册、965 项回归、
镜像层/小时级稳定性证据）。AgentWatch 是**新增的外部 agent 监测与防护层**，
它的存在不改写原有评测结论，也不让那些数字变成「LLM 注入防护有效」。

## 五、要达到「生产可用」还差什么

| 缺口 | 最小下一步 |
|---|---|
| 语义级检测 | 接真模型做可疑度判定，并单独测误报（v3 预注册那套线） |
| 入口强制 | 把代理设为唯一入口、关掉直连；app.py 默认改 fail-closed |
| 真实攻击集 | 用外部公开注入语料替换确定性探针（需先解决上游 commit/license） |
| 外部 agent 生态 | 提供 SDK / 协议文档 / 多语言适配器 |
| 多副本 | 共享策略与限流、集中审计 |
| 独立复跑 | v3 那条「未参与调参的一方」仍然缺人 |
