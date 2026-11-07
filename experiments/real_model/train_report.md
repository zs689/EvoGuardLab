# 真实模型自举闭环：训练报告

> 由 `ai-service/real_model_train.py` 自动生成。
> 模型 `qwen3:4b` ｜ 后端 `ollama` ｜ 用时 423s ｜ run 310 次

## 一、划分（test 一律不参与）

| 用途 | 来源 | 用例数 |
|---|---|---|
| 探索（train） | dev task #1（每 agent）／复合 dev 每个模式各 1 条 | 39 |
| 验证（val） | dev task #2（每 agent）／复合 dev 每个模式另 1 条 | 39 |
| 评测 | test（6 task/agent + 30 复合） | 234 |

## 二、探索

| 项 | 值 |
|---|---|
| 规则表直接救回（不需要新策略） | 31 |
| 需要探索才救回 | 7 |
| 其中靠组合链救回 | 5 |
| 提炼并入库的经验 | 38 |
| 经验准入被拒 | 0 |
| 生成的候选技能 | 38 |
| 其中组合技能（链长>1） | 5 |

## 三、两套库的差别（这就是被激活的变量）

| 库 | 入库条件 | 条数 | 给谁用 |
|---|---|---|---|
| 严格库 | 独立验证集 gain 达标 且 负迁移不超标 | 6 | D |
| 放宽库 | 本次恢复成功即入库，**不验证** | 38 | C |

判定分布：{"REJECT": 32, "PROMOTE": 6}

## 四、逐条验证结果

| 技能 | Agent | 故障 | 策略 | 增益(pp) | 负迁移 | 判定 |
|---|---|---|---|---|---|---|
| ['retry'] | order | tool_timeout | retry | 0.0 | 0% | **REJECT** |
| ['retry'] | order | tool_exception | retry | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | order | invalid_json | fallback_tool | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | order | missing_field | fallback_tool | 0.0 | 0% | **REJECT** |
| ['cross_verify'] | order | silent_corruption | cross_verify | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | order | retrieval_failure | fallback_tool | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | order | context_loss | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | order | process_interruption | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['reformat'] | order | format_violation | reformat | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | order | wrong_tool | fallback_tool | 0.0 | 0% | **REJECT** |
| ['cross_verify'] | order | tool_hallucination | cross_verify | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | order | premature_termination | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['retry'] | kb | tool_timeout | retry | 0.0 | 0% | **REJECT** |
| ['retry'] | kb | tool_exception | retry | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | kb | invalid_json | fallback_tool | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | kb | missing_field | fallback_tool | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | kb | retrieval_failure | fallback_tool | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | kb | context_loss | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | kb | process_interruption | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['cross_verify'] | kb | stale_data | cross_verify | 100.0 | 0% | **PROMOTE** |
| ['reformat'] | kb | format_violation | reformat | 0.0 | 0% | **REJECT** |
| ['cross_verify'] | kb | tool_hallucination | cross_verify | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | kb | premature_termination | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['retry'] | planner | tool_timeout | retry | 0.0 | 0% | **REJECT** |
| ['retry'] | planner | tool_exception | retry | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | planner | invalid_json | fallback_tool | 0.0 | 0% | **REJECT** |
| ['fallback_tool'] | planner | missing_field | fallback_tool | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | planner | context_loss | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | planner | process_interruption | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['cross_verify'] | planner | incorrect_planning | cross_verify | 0.0 | 0% | **REJECT** |
| ['reformat'] | planner | format_violation | reformat | 0.0 | 0% | **REJECT** |
| ['cross_verify'] | planner | tool_hallucination | cross_verify | 0.0 | 0% | **REJECT** |
| ['replay_checkpoint'] | planner | premature_termination | replay_checkpoint | 0.0 | 0% | **REJECT** |
| ['fallback_tool', 'cross_verify'] | order | missing_field+silent_corruption | fallback_tool→cross_verify | 100.0 | 0% | **PROMOTE** |
| ['fallback_tool', 'cross_verify'] | order | wrong_tool+format_violation | fallback_tool→cross_verify | 100.0 | 0% | **PROMOTE** |
| ['fallback_tool', 'cross_verify'] | kb | retrieval_failure+tool_hallucination | fallback_tool→cross_verify | 100.0 | 0% | **PROMOTE** |
| ['fallback_tool', 'cross_verify'] | kb | stale_data+context_loss | fallback_tool→cross_verify | 100.0 | 0% | **PROMOTE** |
| ['fallback_tool', 'cross_verify'] | planner | incorrect_planning+premature_termination | fallback_tool→cross_verify | 100.0 | 0% | **PROMOTE** |

## 五、诚实说明

1. 这是**真实模型自举**（探索轨迹由 Qwen3 产生），不再是跨模型迁移。
2. 但任务本身简单：模型在答案步几乎不犯错，所以探索失败基本都来自故障机制，
   不是模型推理错。技能学到的是「遇到这类故障该走哪几步」。
3. dev 每 agent 只有 2 个 task，train/val 各 1 个，样本极小，不能当统计结论。
4. 放宽库不验证就入库 —— 这是**故意**的，用来量出「不验证」的代价。
