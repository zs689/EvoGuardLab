# Agent 离线检查报告

> 检查器：EvoGuard inspect（确定性规则，不调用模型）  
> 输入：11 条轨迹（../tests/fixtures/trace_synthetic_bad.json）

## 一、总览

| 严重度 | 条数 |
|---|---|
| error | 5 |
| warn | 15 |
| info | 0 |

| 分组 | 条数 | 说明 |
|---|---|---|
| answer | 6 | 输出层：答案本身不对 |
| tool | 8 | 工具层：调用或结果的使用有问题 |
| flow | 4 | 流程层：步骤顺序 / 循环 / 过早收尾 |
| resource | 1 | 资源层：token 与耗时 |
| structure | 1 | 结构层：多 Agent / 并行 / 错误传播 |

> 排查顺序建议：先看**输出层**（结论错了），再往**工具层/流程层**倒推原因。

| 规则 | 分组 | 命中 | 严重度 |
|---|---|---|---|
| `tool_result_unused` 工具返回了实质内容，但后续完全没引用 | tool | 5 | warn |
| `hallucinated_number` 答案里出现工具没返回过的数字 | answer | 4 | warn |
| `error_swallowed` 工具报错后仍然给出了最终答案 | tool | 1 | error |
| `premature_finish` 没有任何有效工具结果就下定论 | flow | 1 | error |
| `write_before_read` 写操作发生在读取之前 | flow | 1 | warn |
| `loop_detected` 同一工具同参数被反复调用 | flow | 1 | warn |
| `empty_answer` 宣称完成但没有产出答案 | answer | 1 | error |
| `pii_leak` 答案里出现未脱敏的个人信息 | answer | 1 | error |
| `tool_name_unknown` 调用了工具清单里不存在的工具 | tool | 1 | warn |
| `parallel_write_conflict` 相邻两步都是写操作 | structure | 1 | error |
| `cycle_detected` 工具调用陷入 A→B→A→B 循环 | flow | 1 | warn |
| `token_blowup` 单条轨迹 token 超预算 | resource | 1 | warn |
| `tool_arg_incomplete` 工具参数是空的或缺少关键项 | tool | 1 | warn |

## 二、问题明细

### [ERROR][answer] 宣称完成但没有产出答案

- 轨迹 `t-empty-0004` ｜ agent `synthetic-agent` ｜ task `-`
- 事实：答案为空：'{}'
- 建议：检查 Agent 的收尾分支；空答案不要按成功上报
- **可能是误报**：流式输出被截断、或答案写在别的字段里（非 answer/completion）。

### [ERROR][tool] 工具报错后仍然给出了最终答案

- 轨迹 `t-swallow-0001` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `order.get_order`
- 事实：工具 order.get_order 报错(timeout)，之后没有重试就给出了答案
- 建议：失败分支要显式降级或重试；把工具错误写进答案，别静默吞掉
- **可能是误报**：该工具本来就允许失败（可选增强），且答案不依赖它。

### [ERROR][structure] 相邻两步都是写操作

- 轨迹 `t-parallel-write-0007` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `order.settle`
- 事实：连续两次写操作：order.settle → order.settle
- 建议：写操作之间插入状态读取或幂等键；确认是否需要串行化
- **可能是误报**：写操作之间本来就不需要读（如批量创建）；也可能是设计如此的流水线。

### [ERROR][answer] 答案里出现未脱敏的个人信息

- 轨迹 `t-pii-0005` ｜ agent `synthetic-agent` ｜ task `-`
- 事实：检出：手机号(138***)
- 建议：输出前做脱敏；日志与答案分开处理
- **可能是误报**：如果被检 Agent 本身就在处理这些数据（如客服工单），需要按合规要求豁免。

### [ERROR][flow] 没有任何有效工具结果就下定论

- 轨迹 `t-swallow-0001` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `chat synthetic`
- 事实：答案已产出，但全程没有一个成功的工具结果
- 建议：拿不到数据时应当明说「查不到」而不是给结论
- **可能是误报**：任务允许零工具（纯知识问答）；或工具结果确实为空且 Agent 如实说了「查不到」。

### [WARN][flow] 工具调用陷入 A→B→A→B 循环

- 轨迹 `t-cycle-0008` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `order.get_order`
- 事实：检测到循环：kb.search → order.get_order 连续重复两轮
- 建议：给循环加最大次数与退出条件；把「已经试过」写进上下文
- **可能是误报**：工作流本身就是交替的（读→写→读→写），属于正常设计。

### [WARN][answer] 答案里出现工具没返回过的数字

- 轨迹 `t-swallow-0001` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `chat synthetic`
- 事实：这些数字在任何工具输出里都找不到：999.0
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN][answer] 答案里出现工具没返回过的数字

- 轨迹 `t-writefirst-0002` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `chat synthetic`
- 事实：这些数字在任何工具输出里都找不到：100.0
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN][answer] 答案里出现工具没返回过的数字

- 轨迹 `t-repeat-0003` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **5** 步 `chat synthetic`
- 事实：这些数字在任何工具输出里都找不到：7
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN][answer] 答案里出现工具没返回过的数字

- 轨迹 `t-cycle-0008` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **5** 步 `chat synthetic`
- 事实：这些数字在任何工具输出里都找不到：5
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN][flow] 同一工具同参数被反复调用

- 轨迹 `t-repeat-0003` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `kb.search`
- 事实：kb.search 用同一组参数被调用 4 次（阈值 3）
- 建议：加去重/缓存；轮询类工具要显式标 polling 语义
- **可能是误报**：该工具用于轮询等待（如查任务状态），重复是设计如此。

### [WARN][resource] 单条轨迹 token 超预算

- 轨迹 `t-token-0009` ｜ agent `synthetic-agent` ｜ task `-`
- 事实：总 token 99000，预算 2000
- 建议：压缩上下文、减少无效轮次；把预算写进 Agent 配置
- **可能是误报**：任务本来就长（多轮对话、长文档总结）；预算需要按业务调整。

### [WARN][tool] 工具参数是空的或缺少关键项

- 轨迹 `t-argempty-0010` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `kb.search`
- 事实：调用 kb.search 时参数为空
- 建议：在工具 schema 层做必填校验；空参数直接拒绝
- **可能是误报**：有些工具确实允许零参数（如 list_all）；这类工具需要单独豁免。

### [WARN][tool] 调用了工具清单里不存在的工具

- 轨迹 `t-badtool-0006` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `weather.get`
- 事实：weather.get 不在允许的工具清单里（共 1 次）
- 建议：在工具注册层做白名单；未知工具直接拒绝
- **可能是误报**：未配置 allowed_tools 时本条不生效；清单本身也可能过期。

### [WARN][tool] 工具返回了实质内容，但后续完全没引用

- 轨迹 `t-cycle-0008` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `kb.search`
- 事实：kb.search 返回的 1 个值（如 1）在答案与后续调用里都没出现
- 建议：确认这次调用是否必要；如果结果该影响答案，说明 Agent 漏读了
- **可能是误报**：结果可能只用于「确认存在」；短的确认型返回（{"ok":true}）本条不判。

### [WARN][tool] 工具返回了实质内容，但后续完全没引用

- 轨迹 `t-cycle-0008` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `order.get_order`
- 事实：order.get_order 返回的 1 个值（如 2）在答案与后续调用里都没出现
- 建议：确认这次调用是否必要；如果结果该影响答案，说明 Agent 漏读了
- **可能是误报**：结果可能只用于「确认存在」；短的确认型返回（{"ok":true}）本条不判。

### [WARN][tool] 工具返回了实质内容，但后续完全没引用

- 轨迹 `t-cycle-0008` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `kb.search`
- 事实：kb.search 返回的 1 个值（如 3）在答案与后续调用里都没出现
- 建议：确认这次调用是否必要；如果结果该影响答案，说明 Agent 漏读了
- **可能是误报**：结果可能只用于「确认存在」；短的确认型返回（{"ok":true}）本条不判。

### [WARN][tool] 工具返回了实质内容，但后续完全没引用

- 轨迹 `t-cycle-0008` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `order.get_order`
- 事实：order.get_order 返回的 1 个值（如 4）在答案与后续调用里都没出现
- 建议：确认这次调用是否必要；如果结果该影响答案，说明 Agent 漏读了
- **可能是误报**：结果可能只用于「确认存在」；短的确认型返回（{"ok":true}）本条不判。

### [WARN][tool] 工具返回了实质内容，但后续完全没引用

- 轨迹 `t-schema-0011` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `order.get_order`
- 事实：order.get_order 返回的 1 个值（如 88.0）在答案与后续调用里都没出现
- 建议：确认这次调用是否必要；如果结果该影响答案，说明 Agent 漏读了
- **可能是误报**：结果可能只用于「确认存在」；短的确认型返回（{"ok":true}）本条不判。

### [WARN][flow] 写操作发生在读取之前

- 轨迹 `t-writefirst-0002` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `order.settle`
- 事实：第一次调用就是写操作 order.settle，之前没有任何读取
- 建议：写之前先读一次当前状态，或确认入参已含前置信息
- **可能是误报**：写操作本身幂等、或入参已包含全部前置信息。

## 三、这份报告不能说明什么

1. 只覆盖 19 条规则（5 组），规则集之外的问题它看不见。
2. 规则只看轨迹，**看不到 Agent 的 prompt 与配置** —— 那属于静态检查。
3. `warn` 级别需要人工核实，尤其 `hallucinated_number`（数字可能是算出来的）。
