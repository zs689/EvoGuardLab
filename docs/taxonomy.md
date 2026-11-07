# 故障分类法：我们覆盖了什么、故意不覆盖什么

> 目的：让"15 类故障"有出处，而不是拍脑袋堆数量。
> 只借鉴公开论文的**分类法**，没有复制任何代码（MAST / TRAIL 均为论文+数据集，非代码库）。

---

## 一、两份参考分类法

| 来源 | 规模 | 结构 | 关键数据 |
|---|---|---|---|
| **MAST**<br>Cemri et al., arXiv:2503.13657（NeurIPS 2025） | 14 类故障模式<br>1642 条真实轨迹 | 3 簇：规格与设计 41.8% / 协作失配 36.9% / 任务验证 21.3% | 多 Agent 系统真实任务失败率 **41%–86.7%** |
| **TRAIL**<br>Patronus AI, arXiv:2505.08638 | 20 个叶子错误类型<br>148 条人工标注轨迹（841 个错误） | 3 支柱：推理 / 系统执行 / 规划与协作 | 顶尖模型做轨迹级定位的联合准确率仅 **11%**；**格式违规/指令不依从占真实错误约 42%**；"上下文处理失败"与"工具选择错误"几乎测不出（F1≈0） |

两条对我们最关键的信息：

1. **格式类错误是真实世界里最大的一类（42%）**，而它恰好最容易自动化检测 —— 这是我们补 `format_violation` 的原因。
2. **上下文处理和工具选择，连顶尖模型都定位不出来** —— 说明这两类要么需要更强的可观测信号，要么本身就是研究空白。我们保留它们，但如实标注检测难度。

---

## 二、15 类覆盖映射

| # | 我们的类型 | TRAIL 对应 | MAST 对应 | 能否诊断 | 恢复策略 |
|---|---|---|---|---|---|
| 1 | `tool_timeout` | System Execution / Resource Mgmt / Timeout | — | ✅ 显式错误 | retry |
| 2 | `tool_exception` | System Execution / API & System / Service Error | — | ✅ 显式错误 | retry |
| 3 | `invalid_json` | Output Generation / Formatting Error | — | ✅ 结构校验 | fallback_tool |
| 4 | `missing_field` | Output Generation / Formatting Error | — | ✅ schema 校验 | fallback_tool |
| 5 | `silent_corruption` | Information Processing / Tool-Output Misinterpretation | — | ✅ **靠业务不变量交叉验算** | cross_verify |
| 6 | `retrieval_failure` | Information Processing / Poor Retrieval | — | ✅ 入参出参不一致 | fallback_tool |
| 7 | `context_loss` | Context Mgmt / Context Handling Failure | — | ✅ 声明字段缺失 | replay_checkpoint |
| 8 | `process_interruption` | Resource Mgmt | — | ✅ 中断标记 | replay_checkpoint |
| 9 | `stale_data` | Information Processing / Poor Retrieval（陈旧证据） | — | ✅ 版本号可观测 | （规则表无策略 → 靠学习） |
| 10 | `incorrect_planning` | Task Mgmt / Task Orchestration Error | 任务验证 | ❌ **已知盲区** | human_takeover |
| 11 | `format_violation` | **Output Generation / Instruction Non-compliance** | — | ✅ 严格 JSON 校验 | reformat |
| 12 | `wrong_tool` | **Decision Making / Tool Selection Error** | — | ✅ 工具契约校验 | fallback_tool |
| 13 | `tool_hallucination` | **Hallucination / Tool-related** | — | ✅ 答案数字不在任何工具返回里 | cross_verify |
| 14 | `resource_abuse` | **Context Mgmt / Resource Abuse** | — | ✅ 同工具同参数重复计数 | replay_checkpoint |
| 15 | `premature_termination` | — | **Task Verification / Premature Termination** | ✅ early_exit 标记 | replay_checkpoint |

**第 14 类不进成功率矩阵**：它是"成本型"故障，任务会成功，只是白花钱。用成功率衡量它等于测不出来，必须用 steps / token 指标单独测。

---

## 三、故意不覆盖的（以及理由）

| TRAIL / MAST 里的类别 | 为什么不加 |
|---|---|
| Rate Limiting(429) / Auth(401,403) / Service(500) / Not Found(404) | 需要真实 HTTP 服务才有意义。在 mock 里塞一个假 429 只能测"我们会不会读 code"，测不出"退避策略对不对"。留给接真实工具时再加 |
| Resource Exhaustion（OOM/显存） | 需要真实资源压力，本地造假没有说服力 |
| Configuration Issues（工具定义写错、环境变量缺失） | 这是部署期问题，不是运行时故障；应放进 CI 而不是评测集 |
| **多 Agent 全部类别**：角色混淆、通信失配、假共识、从众偏差、逐渐偏离、无限委派 | 我们第一版是**单 Agent**（规格明确要求）。多 Agent 故障需要至少两个 Agent 才能注入，硬造出来只能骗自己 |
| 人类因素（Human-in-the-loop 拒绝、审批延迟） | 需要真人参与，无法在自动化评测里复现 |

---

## 四、已知盲区（必须写进报告，不能藏）

1. **`incorrect_planning` 诊断不出来**（34 个组合里唯一一个）。原因是"步骤顺序对不对"需要一个业务前置条件表；而我们手里唯一的顺序来源就是真值本身，用它当规则就是作弊。
   出路：另外定义一份**独立于任务列表**的流程约束（如"审批类动作必须在比价之后"），才能诚实地诊断这一类。**目前没做。**
2. **TRAIL 自己承认的两个难题我们也一样**：上下文处理失败和工具选择错误，即便我们加了专项规则，在真实（非构造）轨迹上的召回率仍是未知数。
3. **41%–86.7%** 这个真实失败率是在**真实任务**上测的；我们的故障是**注入**的，所以 A 组能干净地归零。真实系统里错误是自然发生的、叠加的，检测难度比这里高一个量级。

---

## 五、引用

- MAST：Cemri et al., *Why Do Multi-Agent LLM Systems Fail?*, arXiv:2503.13657
- TRAIL：Deshpande et al., *TRAIL: Trace Reasoning and Agentic Issue Localization*, arXiv:2505.08638
- 业务不变量（invariants）思路参考 Microsoft **AgentRx**（MIT），本仓库为自研实现，未复制代码
- 故障插桩思路参考 **agent-chaos**（Apache-2.0），本仓库为自研实现
- **AgentChaos 仓库无任何 LICENSE 文件**，未参考其代码（仅读过目录结构）
