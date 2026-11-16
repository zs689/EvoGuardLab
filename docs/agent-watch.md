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

## 一点五、对照业界做法（2026-11-16 联网调研后落地）

调研来源与结论（不是拍脑袋）：

| 来源 | 关键结论 | 我们怎么用 |
|---|---|---|
| Meta **LlamaFirewall / PromptGuard 2**（2025-05 开源） | 分层：规则/分类器 → 语义审查 → 代码静态分析；PromptGuard 2（86M/22M）在 AgentDojo 上把攻击成功率从 17.6% 压到 7.5%（86M） | 采纳「分层」思路：正则 → 归一化/解码 → 小模型语义；**没有**下载 PromptGuard 权重（HF gated + 需 torch） |
| PromptGuard 2 卡片局限 | 只读 512 token，长文档要**分块并行扫**，否则跨块攻击可漏 | 我们用正则全量扫，已实测 15010 字符文档尾部 payload 能命中，**不需要分块** |
| Unicode 混淆研究（FreeBuf / 多篇工程实践） | **西里尔/希腊同形字能穿过 NFKC**，是「自愈式载荷」的根基；字母间填充（`i.g.n.o.r.e`）同样绕过关键词 | 加 `homoglyph` 映射表 + `despaced` 压缩视图；实测两类都命中，正常俄语不误报 |
| 同类工程建议 | 「先归一化再检测」、保留原文与归一化两份、不可见字符密度升级人工复核 | 已做前两条；密度只做计数告警，未升级为阻断 |

**没做的**（别以为都做了）：
- **AlignmentCheck 式的思维链审查**：需要读 agent 的推理轨迹并用大模型审计，本项目没有接 agent 内部推理。
- **CodeShield 式代码静态分析**：我们的 `static_audit` 是配置审计，不解析代码 AST。
- **PromptGuard 专用分类器**：本机只有 `qwen3:4b`，语义层用它，效果弱于专用 86M 分类器。

## 一点六、语义判定层（可选，默认关）

`SemanticJudge` 调本机 Ollama（默认 `qwen3:4b`），在正则/归一化都没命中时
再问一次「这段文本是否在操纵 agent」。**只在 `AGENTLAB_AGENT_WATCH_SEMANTIC=1`
时启用**，因为每条 0.5–3 秒。

实测（`probes/semantic_judge.json`）：

| 指标 | 结果 | 口径与偏差 |
|---|---|---|
| 召回 | **9/10 = 90%** | 自构造 10 条注入（含 base64、同形字、伪装系统消息），规模小 |
| 误报 | **1/40 = 2.5%** | `jailbreak_classification` 的 benign 等距抽样；该语料是「有害内容」分类而非「操纵 agent」分类，**误报率偏低估** |
| 业务样本误报 | **0/5** | 结算/查单/总结/折扣/退款 |

**必须知道的坑**：4B 模型判定不稳定 —— 实测同一段文本在
`num_predict` 160 与 200 下给出过相反结论。所以语义层只给
`severity="review"`（不执行、转人工），**不单独 deny**；
判定服务不可用时上报 `semantic_unavailable`，不静默当安全。

## 一点七、接了业界分类器：ProtectAI DeBERTa ONNX（实测取舍）

调研到的可用资产（都开源、Apache-2.0）：
- **`ProtectAI/deberta-v3-base-prompt-injection-v2`** —— 事实标准级的开源
  注入分类器，官方提供 `onnx/model.onnx`（fp32，738MB）；上游自评
  20k 留出集：Accuracy 95.25% / Precision 91.59% / Recall 99.74%。
- `Sovraine/prompt-injection-onnx` —— 同一个模型的 ONNX 打包版（738MB）。
- `llm-guard-ml`（Rust）、`ai-shield-classifier-onnx`（npm）—— 都走
  「规则层 + ONNX 分类器」的分层结构，佐证了我们的架构选择。

**怎么接的（主程序零新依赖）**：模型推理放在独立 venv
（`onnxruntime` + `tokenizers`，**不需要 torch**），主服务用**长驻子进程
+ stdin/stdout 管道**调用（`agent_watch.OnnxInjectionJudge`）。
首次 1.86s（加载模型），之后 **0.02s/条**。

**实测结果（`probes/pi_onnx_eval.json`，fp32）**：

| 指标 | DeBERTa fp32 | qwen3:4b |
|---|---|---|
| 中文注入召回（自构造 10 条） | **9/10 = 90%** | 9/10 = 90% |
| 英文注入召回（自构造 10 条） | 7/10 = 70% | 未测 |
| 语料 benign 误报（40 条） | **0/40 = 0%** | 1/40 = 2.5% |
| **业务样本误报** | **2/5 = 40%** ⚠️ | 0/5 |
| 延迟/条 | **0.02s** | 0.5–3s |

**两个必须写下来的坑**：
1. **int8 动态度量把模型打崩了**：`Ignore all previous instructions...`
   ／的 p 从 1.0 掉到 0.025（判 SAFE）。保留 `--quantize` 只为复现该结论，
   生产必须用 fp32。
2. **业务误报 40%**：把「总结这封邮件…不要执行任何操作」(p=1.000)、
   「客户要求退款，标准流程怎么走」(p=0.958) 判成注入。分数太高，
   **调阈值救不了**（真注入也是 0.99+）。

**所以结论不是「装上就完事」**：DeBERTa 适合当**高召回筛子**
（语料 0 误报、90% 召回、20ms），但**不能直接做自动拦截**；
本项目的语义层只输出 `review`（转人工、不执行），正是为了让
这种域外误报付出「多看一眼」而不是「业务被拒」的代价。
要让它能自动拦，必须用你们的业务流量做**域内校准**（调阈值/白名单/
微调），这一步本机没有真实流量，做不了。


### 一点八、域内校准与微调：改权重到底能改到什么程度

用户授权「可以修改模型权重」，所以三档都真做了，结果如下
（评估集 120 条，60 正常 60 注入，模板与训练集不同源）：

| 方案 | 改了什么 | 业务误报 | 召回 |
|---|---|---|---|
| 原始 DeBERTa | — | 16/60 = **26.7%** | 60/60 = 100% |
| **校准头**（5 参数逻辑回归） | 校准层权重 | 8/60 = **13.3%** | 57/60 = 95% |
| **微调分类头**（冻结 backbone，只训 1538 参数） | 分类头权重 | 16/60 = 26.7%（**无改善**） | 60/60 |
| **CLS 特征 + 线性头**（训练集 loss 归零） | 分类头权重 | 8/60 = **13.3%** | 56/60 = 93.3% |

**两个必须写清楚的发现**：

1. **第一版微调「权重变了但预测一条没翻」**：classifier 权重最大变化
   0.0595，120 条预测 0 条改变。原因是原始 logits 差距太大
   （误报样本 p=1.0，logit 差 10+），0.06 的权重更新不足以翻转任何
   决策。这是训练方式的问题，不是导出问题 —— 我先做了诊断
   （对比更新前后的 PyTorch 预测）才下这个结论。
2. **换成「冻结 backbone 提特征 + 线性头」后，训练集 loss 归零
   （完全记住），eval 仍卡在 13.3%** —— 和校准头一模一样。

**所以结论是：13.3% 是这批数据的上限，不是权重没调好。**
那 8 条被误判的业务文本（如「总结这封邮件…不要执行任何操作」）
在冻结的 DeBERTa 表示空间里**真的和注入不可分**（都含
「执行/操作/退款」）。要突破这个上限只有两条路：
① **解冻 backbone** 改动表示本身（需要数百到数千条域内标注 + 算力）；
② 加**外部信号**（会话历史、工具调用上下文）—— 单条文本没有的信息。

校准头权重（`probes/pi_calib_weights.json`）可解释性也在支持这个判断：

| 特征 | 权重 |
|---|---|
| `biz_word`（结算/退款/总结/政策…） | **-4.83** |
| `p_deberta` | +4.49 |
| `strong_hint`（忽略/系统提示/新指令/忘掉） | +3.76 |
| `len_norm` | +2.65 |
| `rule_error_hits` | +1.79 |

数据本身是模板构造的（120 训练 + 120 评估），**只能证明方法有效，
不能证明线上泛化**；真实校准必须用线上标注。


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

### 已经补上的（2026-11-16 第二轮）

1. **入口防护已接线且默认拦截**：`app.py` 在 `POST /api/run` 与
   `POST /api/jobs` 解析 body 后调用 `GuardGateway.inspect_input`，
   命中 error 级特征直接返回 **403 `E_INPUT_BLOCKED`**。
   `GET /api/health` 暴露 `agent_watch.{mode,enforce,scanned,blocked}`。
   开关：`AGENTLAB_AGENT_WATCH=enforce`（默认）/ `audit`（只统计）/ `off`。
   实测：正常请求 200、注入请求 403，978 项既有回归零误伤。
2. **编码绕过已覆盖**：网关扫描前生成 4 类解码视图 —— URL 编码、
   HTML 实体、Base64、全角 NFKC —— 并在「只有解码后才命中」时
   额外记 `obfuscated_payload`（error）。实测四种写法都命中，
   正常业务文本（含 `15%`）不误报。
3. **误报可观测**：finding 带检测器原有的 `false_positive` 说明，
   `/api/health` 有 `blocked` 计数，运维可据此判断是否要
   临时降到 `audit`。

### 仍然存在的（不许含糊）

1. **仍不是语义级**：改写成同义句、多轮渐进诱导、跨语言混合、
   拆字/谐音/emoji 替换仍然在覆盖外。解码视图解决的是「同一句话
   换个编码」，不是「换句话表达同一意图」。
2. **静态审计是配置审计**，不是源码 SAST、符号执行或依赖漏洞扫描；
   不分析字节码、不跑模糊测试。
3. **代理不是唯一入口**：若被监测 agent 还暴露直连端口、消息队列、
   本地 CLI，流量不过网关就不受保护。必须把网关放在唯一入口。
4. **未接真实 LLM 做可疑判定**：语义判定仍靠正则与规则；
   项目没有用模型去判断「这句话是不是攻击」。
5. **单机、进程内**：无分布式策略同步、无跨副本限流、无集中审计。
6. **误报代价真实存在**：扩面后的 `injection_tool_directive` 会把
   运维类「运行命令：」判 error 并在入口拦；需要按业务调白名单或
   降到 audit。
7. **未完成生产化**：无 TLS 终止、mTLS、身份源对接、审计归档、
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
