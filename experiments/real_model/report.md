# 真实模型全量评测报告

> 由 `ai-service/real_model_bench.py` 自动生成，数字全部来自本次运行。
> 模型 `qwen3:4b` ｜ 后端 `ollama` ｜ grammar=True ｜ 代码指纹 `ecf75283f3e16de9`

## 一、实验配置（可复现）

| 项 | 值 |
|---|---|
| 模型 | qwen3:4b |
| 服务 | http://127.0.0.1:11434（Ollama 0.40.15） |
| 后端 | ollama / provider=ollama |
| 关闭思考 | True |
| JSON grammar 约束 | True |
| System prompt 指纹 | `8da48e6cbf348d7b` |
| 代码指纹 | `ecf75283f3e16de9` |
| 技能库 | **双库**：D 严格库 6 条 ｜ C 放宽库 38 条 |
| 单故障用例 | 204 |
| 复合故障用例 | 30 |
| 完成 run 数 | 936 |
| 总耗时 | 654.6 秒 |
| 模型调用次数 | 936 |

## 二、四组对照（单故障，204 用例）

| 模式 | 成功 | 成功率 | mock 基线 | 差 |
|---|---|---|---|---|
| A | 0/204 | **0.0%** | 0.0% | 0.0 pp |
| B | 54/204 | **26.5%** | 26.5% | 0.0 pp |
| C | 204/204 | **100.0%** | 100.0% | 0.0 pp |
| D | 198/204 | **97.1%** | 100.0% | -2.9 pp |

## 三、复合故障（30 用例）

| 模式 | 成功 | 成功率 |
|---|---|---|
| A | 0/30 | **0.0%** |
| B | 0/30 | **0.0%** |
| C | 0/30 | **0.0%** |
| D | 30/30 | **100.0%** |

> mock 闭环在复合故障上只测过 **A / 最优单策略 / 两步链 / D** 四组
> （0% / 0% / 100% / 100%，见 `experiments/evolution_report.md`），
> **没有**测过复合的 A/B/C/D 四组对照，所以这里不填 mock 基线 —— 
> 填 0 会被误读成「mock 测出来是 0%」。

## 四、模型行为（这才是真实模型评测的重点）

- 单故障：`bad_model_output` 0 次，LLM 错误 {'E_LLM_HTTP': 16}
- 复合故障：`bad_model_output` 0 次，LLM 错误 {'E_LLM_DOWN': 4, 'E_LLM_UNREACHABLE': 11}
- 平均延迟 734.4 ms ｜ 平均 token 104.1 ｜ 总耗时 599.3 秒

### 无 grammar 约束对照（6 次独立调用）

| 指标 | 值 |
|---|---|
| 严格 JSON 成功率 | **0.0%**（0/6） |
| 平均延迟 | 10504.5 ms |
| 错误分类 | 无 |

> 这一组是必需的：`format=json` 的 grammar 约束**从模型侧消除了自然格式违规**，
> 所以主实验里 `format_violation` 只能靠注入产生，不能声称测到了模型真实犯错率。

## 五、D 模式仍然失败的用例（不隐藏）

| Agent | 任务 | 故障 | 诊断链 | 恢复链 | 答案 |
|---|---|---|---|---|---|
| planner | release | incorrect_planning | incorrect_planning | human_takeover | {'steps': ['灰度发布', '生成变更说明', '跑回归测试', '冻结代码']} |
| planner | refund | incorrect_planning | incorrect_planning | human_takeover | {'steps': ['财务退款', '生成退货单', '确认退货原因', '核对订单']} |
| planner | procurement | incorrect_planning | incorrect_planning | human_takeover | {'steps': ['下单验收', '提交审批', '三家比价', '汇总需求']} |
| planner | incident | incorrect_planning | incorrect_planning | human_takeover | {'steps': ['复盘归档', '定位根因', '止血', '确认故障范围']} |
| planner | audit | incorrect_planning | incorrect_planning | human_takeover | {'steps': ['整改跟踪', '出具报告', '抽样检查', '准备资料']} |
| planner | training | incorrect_planning | incorrect_planning | human_takeover | {'steps': ['归档成绩', '组织考核', '邀请讲师', '确定课表']} |

## 六、按故障类型看 D 成功率

| 故障 | 成功/总 | 成功率 |
|---|---|---|
| context_loss | 18/18 | 100.0% |
| format_violation | 18/18 | 100.0% |
| incorrect_planning | 0/6 | 0.0% |
| invalid_json | 18/18 | 100.0% |
| missing_field | 18/18 | 100.0% |
| premature_termination | 18/18 | 100.0% |
| process_interruption | 18/18 | 100.0% |
| retrieval_failure | 18/18 | 100.0% |
| silent_corruption | 6/6 | 100.0% |
| stale_data | 6/6 | 100.0% |
| tool_exception | 18/18 | 100.0% |
| tool_hallucination | 18/18 | 100.0% |
| tool_timeout | 18/18 | 100.0% |
| wrong_tool | 6/6 | 100.0% |

## 七、必须讲清楚的取舍与局限

1. **速度换来的代价**：为把 936 个 run 从 5 小时压到可接受时间，
   用了原生 `/api/chat` + `think:false` + `format=json`。实测单次 14s → 0.5s。
   代价是 grammar 约束消除了模型自然产生的格式违规，第四节给了无约束对照数据。
2. **技能库来源**：D 用严格库（独立验证通过才入库）、C 用放宽库（探索成功就入库、不验证），两套库都由 `real_model_train.py` 用同一个真实模型训出来 —— 这才是「验证严不严」这个变量的真正对照。
3. **故障仍是注入的**：不是真实事故里自然共现的错误。
4. **只有一次运行**：没有多次 seed 取均值，模型随机性未量化。
5. `format_violation` 类故障由代码注入，不能当作模型真实犯错率。
