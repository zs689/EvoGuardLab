# EvoGuard Lab · 进化闭环实验报告

> 由 `ai-service/evolution_bench.py` 自动生成，数字全部来自本次运行。

## 一、这次跑通的闭环

| 步骤 | 结果 |
|---|---|
| 训练集用例 | 78 |
| 其中复合故障用例 | 10 |
| 探索恢复成功 | 14 |
| 其中靠策略链救回 | 10 |
| 成功提炼的经验 | 77 |
| 生成的候选技能 | 77 |
| 晋升为正式技能 | 7 |

> 晋升的组合技能（链长 > 1）：**10** 个。为 0 就说明策略链没被学到，
> 不是「进化」，只是把单策略换了个名字。

### 候选技能验证明细

| 技能 | 目标病例 | 验证前成功率 | 验证后成功率 | 增益(pp) | 负迁移率 | 判定 |
|---|---|---|---|---|---|---|
| 遇到 tool_timeout 时按序执行 retry | order/tool_timeout | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_exception 时按序执行 retr | order/tool_exception | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 invalid_json 时按序执行 fallba | order/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时按序执行 fallb | order/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 silent_corruption 时按序执行 c | order/silent_corruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时按序执行 f | order/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时按序执行 replay | order/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时按序执 | order/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 format_violation 时按序执行 re | order/format_violation | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 wrong_tool 时按序执行 fallback | order/wrong_tool | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_hallucination 时按序执行  | order/tool_hallucination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 premature_termination 时按序 | order/premature_termination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_timeout 时按序执行 retry | order/tool_timeout | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_exception 时按序执行 retr | order/tool_exception | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 invalid_json 时按序执行 fallba | order/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时按序执行 fallb | order/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 silent_corruption 时按序执行 c | order/silent_corruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时按序执行 f | order/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时按序执行 replay | order/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时按序执 | order/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 format_violation 时按序执行 re | order/format_violation | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 wrong_tool 时按序执行 fallback | order/wrong_tool | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_hallucination 时按序执行  | order/tool_hallucination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 premature_termination 时按序 | order/premature_termination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_timeout 时按序执行 retry | kb/tool_timeout | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_exception 时按序执行 retr | kb/tool_exception | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 invalid_json 时按序执行 fallba | kb/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时按序执行 fallb | kb/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时按序执行 f | kb/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时按序执行 replay | kb/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时按序执 | kb/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 stale_data 时按序执行 cross_ve | kb/stale_data | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 format_violation 时按序执行 re | kb/format_violation | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_hallucination 时按序执行  | kb/tool_hallucination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 premature_termination 时按序 | kb/premature_termination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_timeout 时按序执行 retry | kb/tool_timeout | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_exception 时按序执行 retr | kb/tool_exception | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 invalid_json 时按序执行 fallba | kb/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时按序执行 fallb | kb/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时按序执行 f | kb/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时按序执行 replay | kb/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时按序执 | kb/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 stale_data 时按序执行 cross_ve | kb/stale_data | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 format_violation 时按序执行 re | kb/format_violation | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_hallucination 时按序执行  | kb/tool_hallucination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 premature_termination 时按序 | kb/premature_termination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_timeout 时按序执行 retry | planner/tool_timeout | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_exception 时按序执行 retr | planner/tool_exception | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 invalid_json 时按序执行 fallba | planner/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时按序执行 fallb | planner/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时按序执行 f | planner/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时按序执行 replay | planner/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时按序执 | planner/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 incorrect_planning 时按序执行  | planner/incorrect_planning | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 format_violation 时按序执行 re | planner/format_violation | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_hallucination 时按序执行  | planner/tool_hallucination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 premature_termination 时按序 | planner/premature_termination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_timeout 时按序执行 retry | planner/tool_timeout | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_exception 时按序执行 retr | planner/tool_exception | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 invalid_json 时按序执行 fallba | planner/invalid_json | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时按序执行 fallb | planner/missing_field | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时按序执行 f | planner/retrieval_failure | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时按序执行 replay | planner/context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 process_interruption 时按序执 | planner/process_interruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 format_violation 时按序执行 re | planner/format_violation | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 tool_hallucination 时按序执行  | planner/tool_hallucination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 premature_termination 时按序 | planner/premature_termination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 missing_field 时按序执行 fallb | order/missing_field+silent_corruption | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 missing_field 时按序执行 fallb | order/missing_field+silent_corruption | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 wrong_tool 时按序执行 fallback | order/wrong_tool+format_violation | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 wrong_tool 时按序执行 fallback | order/wrong_tool+format_violation | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 retrieval_failure 时按序执行 f | kb/retrieval_failure+tool_hallucination | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 retrieval_failure 时按序执行 f | kb/retrieval_failure+tool_hallucination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 context_loss 时按序执行 fallba | kb/stale_data+context_loss | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 context_loss 时按序执行 fallba | kb/stale_data+context_loss | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |
| 遇到 premature_termination 时按序 | planner/incorrect_planning+premature_termination | 0.0% | 100.0% | 100.0 | 0.0% | **PROMOTE** |
| 遇到 premature_termination 时按序 | planner/incorrect_planning+premature_termination | 100.0% | 100.0% | 0.0 | 0.0% | **REJECT** |

## 二、四组对照（独立测试集 204 个用例）

| 模式 | 含义 | 成功率 |
|---|---|---|
| A | 无恢复 | 0.0% |
| B | 诊断+规则恢复，无经验学习 | 26.5% |
| C | 用经验但**不严格验证** | 100.0% |
| D | 完整闭环（验证+晋升） | 100.0% |

- **负迁移率（B 成功但 D 失败）**：0.0%（0/204）
- D 相对 B 提升：**73.5 个百分点**
- D 相对 C 提升：**0.0 个百分点**

## 三、复合故障：策略链到底有没有用（30 个用例）

单故障只需诊断一次、用一个策略，策略链和单策略在数字上没有区别，
所以必须换靶场：**同一次运行注入两个落在不同逻辑步的持久故障**。
对照组 SINGLE 允许从 5 个单策略里挑最好的一个（对单策略最有利的设定）。

| 组 | 含义 | 成功 | 成功率 |
|---|---|---|---|
| A | 无恢复 | 0 | 0.0% |
| SINGLE | 最优单策略（5 选 1） | 0 | 0.0% |
| CHAIN | 两步策略链 fallback_tool -> cross_verify | 30 | 100.0% |
| D | 完整闭环（自动查已晋升技能） | 30 | 100.0% |

- **组合相对单策略提升：100.0 个百分点**（0.0% → 100.0%）
- 为什么单策略必然不够：诊断每轮只返回**一个**坏点；处理完重放，
  第二个坏点才暴露。链的第 2 轮会重新诊断、按新根因换策略。
- 修复动作受「本轮诊断根因」授权，策略不能顺手把沿途所有认识的故障都修了，
  否则单策略就能偷跑完两步，实验结论会是假的。

## 四、怎么读

1. 如果 **D = B**，说明技能没起作用或早已被规则覆盖 —— 那这套进化是装饰。
2. 如果 **D > C**，说明「严格验证」这一步真的挡住了坏经验。
3. **负迁移率必须接近 0**。不为 0 就说明晋升门槛太松。
4. 如果 **CHAIN = SINGLE**，说明策略链没有增量，不该写进论文/简历。

## 五、已知局限（不粉饰）

1. 模型仍是 mock（`AGENTLAB_LLM=mock`），本报告衡量的是**机制是否自洽**，不是模型能力。
2. 训练/测试划分只按 task_id 切，样本量小（各 78 / 204），不能当统计结论。
3. 复合故障是**构造出来的**，不是真实事故里自然共现的错误；
   它证明的是「机制上限」，不是「真实分布下的收益」。
4. 流程不变量 `INV-PLAN-ORDER` 靠通用动作词判断首尾阶段，
   纯执行型流程（步骤里没有启动/收尾词）仍有检测盲区，见 `docs/taxonomy.md`。
5. 技能是声明式策略映射，不是生成代码，不涉及沙箱执行风险；
   但也意味着策略空间仍是**固定词表**，不是开放式规划。
