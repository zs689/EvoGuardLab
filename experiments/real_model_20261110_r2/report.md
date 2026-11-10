# 真实模型全量评测报告

> 由 `ai-service/real_model_bench.py` 自动生成，数字全部来自本次运行。
> 模型 `qwen3:4b` ｜ 后端 `ollama` ｜ grammar=True ｜ 代码指纹 `ac92853400a15ec2`

## 一、实验配置（可复现）

| 项 | 值 |
|---|---|
| 模型 | qwen3:4b |
| 服务 | http://127.0.0.1:11434（Ollama 0.40.15） |
| 后端 | ollama / provider=ollama |
| 关闭思考 | True |
| JSON grammar 约束 | True |
| System prompt 指纹 | `8da48e6cbf348d7b` |
| 代码指纹 | `ac92853400a15ec2` |
| 技能库 | **双库**：D 严格库 11 条 ｜ C 放宽库 127 条 |
| 单故障用例 | 204 |
| 复合故障用例 | 30 |
| 难度组用例 | 36 |
| 完成 run 数 | 1080 |
| 总耗时 | 702.4 秒 |
| 模型调用次数 | 1058 |

## 二、四组对照（单故障，204 用例）

| 模式 | 成功/可评 | 不可评 | 成功率 | 老口径(成功/总数) | mock 基线 | 差 |
|---|---|---|---|---|---|---|
| A | 0/204 | 0 | **0.0%** | 0.0% | 0.0% | 0.0 pp |
| B | 45/204 | 0 | **22.1%** | 22.1% | 26.5% | -4.4 pp |
| C | 169/204 | 0 | **82.8%** | 82.8% | 100.0% | -17.2 pp |
| D | 163/204 | 0 | **79.9%** | 79.9% | 100.0% | -20.1 pp |

> **口径**：只有**框架异常**（run 根本没跑完）才判不可评、剔出分母。
> 模型报错（`E_LLM_*`）**不剔** —— 只要 run 跑完、有答案，就计失败。
> 这一条是查出来的，不是拍出来的：本轮 15 条失败伴随模型报错，逐条看轨迹后发现它们全都有答案，
> 失败原因在注入的故障上（如 `incorrect_planning` 让工具返回倒序计划、规则表又无策略 → 转人工），
> 模型那步报不报错结果都一样。把这类剔出分母，等于用「服务抖了一下」洗白真实失败。
> 伴随模型报错的失败条数单独记在 `failures_with_llm_error`（本轮 D 为 2 条），**不动分母**。
> `bad_model_output`（模型吐出非 JSON）一律计分 —— 那是模型质量问题，不许剔。
> 全量可评率：单故障 **100.0%**、复合 **100.0%**（本轮无框架异常）。

## 三、复合故障（30 用例）

| 模式 | 成功/可评 | 不可评 | 成功率 | 老口径 |
|---|---|---|---|---|
| A | 0/30 | 0 | **0.0%** | 0.0% |
| B | 0/30 | 0 | **0.0%** | 0.0% |
| C | 0/30 | 0 | **0.0%** | 0.0% |
| D | 16/30 | 0 | **53.3%** | 53.3% |

> mock 闭环在复合故障上只测过 **A / 最优单策略 / 两步链 / D** 四组
> （0% / 0% / 100% / 100%，见 `experiments/evolution_report.md`），
> **没有**测过复合的 A/B/C/D 四组对照，所以这里不填 mock 基线 —— 
> 填 0 会被误读成「mock 测出来是 0%」。

## 三b、难度组（36 用例）：工具只给原料，答案要模型自己算

前两组是**抽取式**任务：工具把答案算好了，模型只要搬运，所以在它们上面
模型几乎不会答错 —— 那测的是恢复机制，不是模型能力。这一组把工具从
「给答案」改成「给原料」：

- `calc`：只返回小计/满减规则/会员折扣率，**不返回总额**；模型必须按
  「先满减、再乘折扣」的顺序自己算（顺序反了结果不同）。
- `xref`：同一主题返回三份不同版本的文档，真值只认**版本号最高**的那份；
  不比较版本就会答成 7 天或 15 天。

| 模式 | 成功/可评 | 不可评 | 成功率 |
|---|---|---|---|
| A | 8/36 | 0 | **22.2%** |
| B | 16/36 | 0 | **44.4%** |
| C | 27/36 | 0 | **75.0%** |
| D | 25/36 | 0 | **69.4%** |

> 这一组**没有 mock 基线**：mock 后端不会做算术、也不会比版本号，
> 跑出来必然是 0%，拿它当基线只会误导。

按故障类型（D）：

| 故障 | 成功/可评 | 成功率 |
|---|---|---|
| missing_field | 5/6 | 83.3% |
| none | 5/6 | 83.3% |
| premature_termination | 5/6 | 83.3% |
| retrieval_failure | 2/3 | 66.7% |
| silent_corruption | 0/3 | 0.0% |
| stale_data | 0/3 | 0.0% |
| tool_exception | 3/3 | 100.0% |
| tool_timeout | 5/6 | 83.3% |

## 四、模型行为（这才是真实模型评测的重点）

- 单故障：`bad_model_output` 0 次，LLM 错误 {'E_LLM_HTTP': 13}
- 复合故障：`bad_model_output` 0 次，LLM 错误 {'E_LLM_HTTP': 6}
- 平均延迟 581.9 ms ｜ 平均 token 97.8 ｜ 总耗时 474.8 秒

### 无 grammar 约束对照（6 次独立调用）

| 指标 | 值 |
|---|---|
| 严格 JSON 成功率（可评口径） | **0.0%**（0/6） |
| 调用失败（不计入分母） | 0 次  |
| 老口径（成功/总调用） | 0.0% |
| 平均延迟 | 9668.7 ms |

> 这一组是必需的：`format=json` 的 grammar 约束**从模型侧消除了自然格式违规**，
> 所以主实验里 `format_violation` 只能靠注入产生，不能声称测到了模型真实犯错率。

## 五、D 模式仍然失败的用例（不隐藏）

| Agent | 任务 | 故障 | 定性 | 诊断链 | 恢复链 | 答案 |
|---|---|---|---|---|---|---|
| kb | 运费险赔多少 | tool_timeout | failed | tool_timeout | retry | None |
| kb | 运费险赔多少 | tool_exception | failed | tool_exception | retry | None |
| kb | 运费险赔多少 | invalid_json | failed | invalid_json | fallback_tool | None |
| kb | 运费险赔多少 | missing_field | failed | missing_field | fallback_tool | None |
| kb | 运费险赔多少 | retrieval_failure | failed | retrieval_failure | fallback_tool | {'cited': ['证据1'], 'text': '自签收之日起 7 个工作日内可无理由退货，商品需保持完好。'} |
| kb | 运费险赔多少 | context_loss | failed | context_loss | replay_checkpoint | None |
| kb | 运费险赔多少 | process_interruption | failed | process_interruption | replay_checkpoint | None |
| kb | 运费险赔多少 | stale_data | failed | stale_data | human_takeover | {'cited': ['上下文证据'], 'text': '运费险赔付首重15元，续重每公斤8元'} |
| kb | 运费险赔多少 | tool_hallucination | failed | tool_hallucination | cross_verify | {'cited': ['上下文证据', '补充步骤 777'], 'text': '运费险赔付首重19元，续重每公斤12 |
| kb | 运费险赔多少 | premature_termination | failed | premature_termination | replay_checkpoint | {} |
| kb | 积分有效期多久 | stale_data | failed | stale_data | human_takeover | {'cited': ['证据1'], 'text': '购物积分自到账起 15 个月内有效，逾期自动清零。'} |
| kb | 账期多少天 | stale_data | failed | stale_data | human_takeover | {'cited': ['证据1'], 'text': '签约企业客户享 33 天账期'} |
| kb | 发票多久能重开 | tool_timeout | failed | tool_timeout | retry | None |
| kb | 发票多久能重开 | tool_exception | failed | tool_exception | retry | None |
| kb | 发票多久能重开 | invalid_json | failed | invalid_json | fallback_tool | None |
| kb | 发票多久能重开 | missing_field | failed | missing_field | fallback_tool | None |
| kb | 发票多久能重开 | retrieval_failure | failed | retrieval_failure | fallback_tool | {'cited': ['证据1'], 'text': '自签收之日起 7 个工作日内可无理由退货，商品需保持完好。'} |
| kb | 发票多久能重开 | context_loss | failed | context_loss | replay_checkpoint | None |
| kb | 发票多久能重开 | process_interruption | failed | process_interruption | replay_checkpoint | None |
| kb | 发票多久能重开 | stale_data | failed | stale_data | human_takeover | {'cited': ['证据1'], 'text': '电子发票在订单完成后 8 个工作日内开具，不支持修改抬头。 默认 |
| kb | 发票多久能重开 | format_violation | failed | format_violation | reformat | {} |
| kb | 发票多久能重开 | tool_hallucination | failed | tool_hallucination | cross_verify | {'cited': ['证据1', '补充步骤 777'], 'text': '电子发票在订单完成后 12 个工作日内开 |
| kb | 发票多久能重开 | premature_termination | failed | premature_termination | replay_checkpoint | {} |
| kb | 安装要提前多久预约 | stale_data | failed | stale_data | human_takeover | {'cited': ['证据1'], 'text': '大件商品提供免费上门安装，需提前 51 小时预约。'} |
| kb | 价保多久内有效 | tool_timeout | failed | tool_timeout | retry | None |
| kb | 价保多久内有效 | tool_exception | failed | tool_exception | retry | None |
| kb | 价保多久内有效 | invalid_json | failed | invalid_json | fallback_tool | None |
| kb | 价保多久内有效 | missing_field | failed | missing_field | fallback_tool | None |
| kb | 价保多久内有效 | retrieval_failure | failed | retrieval_failure | fallback_tool | {'cited': ['证据1'], 'text': '退货审核通过后 3 个工作日内原路退回，遇节假日顺延。'} |
| kb | 价保多久内有效 | context_loss | failed | context_loss | replay_checkpoint | None |
| kb | 价保多久内有效 | process_interruption | failed | process_interruption | replay_checkpoint | None |
| kb | 价保多久内有效 | stale_data | failed | stale_data | human_takeover | {'cited': ['证据1'], 'text': '自签收之日起 10 个工作日内可无理由退货，商品需保持完好。'} |
| kb | 价保多久内有效 | format_violation | failed | retrieval_failure | fallback_tool | {} |
| kb | 价保多久内有效 | tool_hallucination | failed | retrieval_failure | fallback_tool | {'cited': ['证据1', '补充步骤 777'], 'text': '自签收之日起 14 个工作日内可无理由退 |
| kb | 价保多久内有效 | premature_termination | failed | premature_termination | replay_checkpoint | {} |
| planner | pricing | incorrect_planning | failed | unknown | human_takeover | {'steps': ['发布价目表', '测算毛利', '核对竞品价', '拉取成本价']} |
| planner | migration | incorrect_planning | failed | unknown | human_takeover | {'steps': ['校验一致性', '灰度迁移', '备份数据', '评估影响面']} |
| planner | recall | incorrect_planning | failed | unknown | human_takeover | {'steps': ['出具召回报告', '回收入库', '通知客户', '圈定批次']} |
| planner | sourcing | incorrect_planning | failed | unknown | human_takeover | {'steps': ['签框架协议', '比价定标', '询价', '收集供应商名单']} |
| planner | settlement | incorrect_planning | failed | unknown | human_takeover | {'steps': ['开票寄送', '出具结算单', '确认差异', '核对账单']} |

（仅列前 40 条，共 55 条，完整数据在 metrics.json）

## 六、按故障类型看 D 成功率（可评口径）

| 故障 | 成功/可评 | 不可评 | 成功率 |
|---|---|---|---|
| context_loss | 15/18 | 0 | 83.3% |
| format_violation | 16/18 | 0 | 88.9% |
| incorrect_planning | 0/6 | 0 | 0.0% |
| invalid_json | 15/18 | 0 | 83.3% |
| missing_field | 15/18 | 0 | 83.3% |
| premature_termination | 15/18 | 0 | 83.3% |
| process_interruption | 15/18 | 0 | 83.3% |
| retrieval_failure | 15/18 | 0 | 83.3% |
| silent_corruption | 6/6 | 0 | 100.0% |
| stale_data | 0/6 | 0 | 0.0% |
| tool_exception | 15/18 | 0 | 83.3% |
| tool_hallucination | 15/18 | 0 | 83.3% |
| tool_timeout | 15/18 | 0 | 83.3% |
| wrong_tool | 6/6 | 0 | 100.0% |

## 七、必须讲清楚的取舍与局限

1. **速度换来的代价**：为把 936 个 run 从 5 小时压到可接受时间，
   用了原生 `/api/chat` + `think:false` + `format=json`。实测单次 14s → 0.5s。
   代价是 grammar 约束消除了模型自然产生的格式违规，第四节给了无约束对照数据。
2. **技能库来源**：D 用严格库（独立验证通过才入库）、C 用放宽库（探索成功就入库、不验证），两套库都由 `real_model_train.py` 用同一个真实模型训出来 —— 这才是「验证严不严」这个变量的真正对照。
3. **故障仍是注入的**：不是真实事故里自然共现的错误。
4. **只有一次运行**：没有多次 seed 取均值，模型随机性未量化。
5. `format_violation` 类故障由代码注入，不能当作模型真实犯错率。
6. **「不可评」的边界是人为定的**：只认框架异常和 `E_LLM_*` 导致的空答案。这条规则可以被滥用成洗白（把失败说成服务问题就能剔出分母），所以报告同时给出 `unjudgeable` 绝对数与 `judgeable_rate`，不允许只看成功率；`bad_model_output` 一律计分。

## 模式实际使用的策略与技能

> 本节由 manifest 模块从本次 metrics 生成，不手工维护另一套数字。

| 模式 | 恢复策略来源 | 技能库 |
|---|---|---|
| A | 无恢复 | 无 |
| B | 诊断 + 固定规则表（一律 retry） | 无 |
| C | 候选 + 已启用技能，仅按 agent 匹配 | library_c.db（127 条） |
| D | 已启用技能，agent + fault_type 严格匹配 | library_d.db（11 条） |

配置：backend=ollama，model=qwen3:4b，协议=?，配置指纹=8fe5db473fd7b42821440b19d18fd12ef43d926ebecef489c26d1728df22f6da

分母自洽检查：通过（无异常）
