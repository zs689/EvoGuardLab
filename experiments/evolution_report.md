# EvoGuard Lab · 进化闭环实验报告

> 由 `ai-service/evolution_bench.py` 自动生成，数字全部来自本次运行。

## 一、这次跑通的闭环

| 步骤 | 结果 |
|---|---|
| 训练集用例 | 24 |
| 其中靠探索恢复成功 | 2 |
| 通过准入校验的经验 | 23 |
| 生成的候选技能 | 23 |
| 晋升为正式技能 | 1 |

### 候选技能验证明细

| 技能 | 目标病例 | 验证前成功率 | 验证后成功率 | 增益(pp) | 负迁移率 | 判定 |
|---|---|---|---|---|---|---|
| 遇到 tool_timeout 时改用 retry | order/tool_timeout | 100.0% | 100.0% | 0.0 | 75.0% | **REJECT** |
| 遇到 tool_exception 时改用 retry | order/tool_exception | 100.0% | 100.0% | 0.0 | 75.0% | **REJECT** |
| 遇到 invalid_json 时改用 fallback | order/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时改用 fallbac | order/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 silent_corruption 时改用 cro | order/silent_corruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时改用 fal | order/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时改用 replay_c | order/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时改用  | order/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_timeout 时改用 retry | kb/tool_timeout | 100.0% | 100.0% | 0.0 | 75.0% | **REJECT** |
| 遇到 tool_exception 时改用 retry | kb/tool_exception | 100.0% | 100.0% | 0.0 | 75.0% | **REJECT** |
| 遇到 invalid_json 时改用 fallback | kb/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时改用 fallbac | kb/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时改用 fal | kb/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时改用 replay_c | kb/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时改用  | kb/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 stale_data 时改用 cross_veri | kb/stale_data | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 tool_timeout 时改用 retry | planner/tool_timeout | 100.0% | 100.0% | 0.0 | 75.0% | **REJECT** |
| 遇到 tool_exception 时改用 retry | planner/tool_exception | 100.0% | 100.0% | 0.0 | 75.0% | **REJECT** |
| 遇到 invalid_json 时改用 fallback | planner/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时改用 fallbac | planner/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时改用 fal | planner/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时改用 replay_c | planner/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时改用  | planner/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |

## 二、四组对照（独立测试集 48 个用例）

| 模式 | 含义 | 成功率 |
|---|---|---|
| A | 无恢复 | 0.0% |
| B | 诊断+规则恢复，无经验学习 | 37.5% |
| C | 用经验但**不严格验证** | 58.3% |
| D | 完整闭环（验证+晋升） | 95.8% |

- **负迁移率（B 成功但 D 失败）**：0.0%（0/48）
- D 相对 B 提升：**58.3 个百分点**
- D 相对 C 提升：**37.5 个百分点**

## 三、怎么读

1. 如果 **D = B**，说明技能没起作用或早已被规则覆盖 —— 那这套进化是装饰。
2. 如果 **D > C**，说明「严格验证」这一步真的挡住了坏经验。
3. **负迁移率必须接近 0**。不为 0 就说明晋升门槛太松。

## 四、已知局限

1. 模型仍是 mock（`AGENTLAB_LLM=mock`），本报告衡量的是**机制是否自洽**，不是模型能力。
2. 训练/测试划分只按 task_id 切，样本量小（各 24 / 48），不能当统计结论。
3. 技能是声明式策略映射，不是生成代码，所以不涉及沙箱执行风险。
