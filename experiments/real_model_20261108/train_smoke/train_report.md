# 真实模型自举闭环：训练报告

> 由 `ai-service/real_model_train.py` 自动生成。
> 验证协议 `paired-v2`；N/A 表示不可估计，样本不足转 NEEDS_REVIEW。
> 模型 `qwen3:4b` ｜ 后端 `ollama` ｜ 用时 11s ｜ run 28 次

## 一、划分（test 一律不参与）

| 用途 | 来源 | 用例数 |
|---|---|---|
| 探索（train） | dev task #1（每 agent）／复合 dev 每个模式各 1 条 | 4 |
| 验证（val） | dev task #2（每 agent）／复合 dev 每个模式另 1 条 | 4 |
| 评测 | test（6 task/agent + 30 复合） | 234 |

## 二、探索

| 项 | 值 |
|---|---|
| 规则表直接救回（不需要新策略） | 4 |
| 需要探索才救回 | 0 |
| 其中靠组合链救回 | 0 |
| 提炼并入库的经验 | 4 |
| 经验准入被拒 | 0 |
| 生成的候选技能 | 4 |
| 其中组合技能（链长>1） | 0 |

## 三、两套库的差别（这就是被激活的变量）

| 库 | 入库条件 | 条数 | 给谁用 |
|---|---|---|---|
| 严格库 | 独立验证集 gain 达标 且 负迁移不超标 | 0 | D |
| 放宽库 | 本次恢复成功即入库，**不验证** | 4 | C |

判定分布：{"NEEDS_REVIEW": 4}

## 四、逐条验证结果

| 技能 | Agent | 故障 | 策略 | 增益(pp) | 负迁移 | 判定 | 原因 |
|---|---|---|---|---|---|---|---|
| ['retry'] | order | tool_timeout | retry | 0.0 | 0.0% | **NEEDS_REVIEW** | target has fewer than 3 independent tasks/families; guard has fewer than 3 independent tasks/families; guard has fewer than 3 baseline-success tasks/families |
| ['retry'] | order | tool_exception | retry | 0.0 | 0.0% | **NEEDS_REVIEW** | target has fewer than 3 independent tasks/families; guard has fewer than 3 independent tasks/families; guard has fewer than 3 baseline-success tasks/families |
| ['fallback_tool'] | order | invalid_json | fallback_tool | 0.0 | 0.0% | **NEEDS_REVIEW** | target has fewer than 3 independent tasks/families; guard has fewer than 3 independent tasks/families; guard has fewer than 3 baseline-success tasks/families |
| ['fallback_tool'] | order | missing_field | fallback_tool | 0.0 | 0.0% | **NEEDS_REVIEW** | target has fewer than 3 independent tasks/families; guard has fewer than 3 independent tasks/families; guard has fewer than 3 baseline-success tasks/families |

## 五、诚实说明

1. 本轮后端为 `ollama`；mock 是机制冒烟，不能作为真实模型结论。
2. 但任务本身简单：模型在答案步几乎不犯错，所以探索失败基本都来自故障机制，
   不是模型推理错。技能学到的是「遇到这类故障该走哪几步」。
3. dev 每 agent 只有 2 个 task，train/val 各 1 个，样本极小，不能当统计结论。
4. 放宽库不验证就入库 —— 这是**故意**的，用来量出「不验证」的代价。
