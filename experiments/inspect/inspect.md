# Agent 离线检查报告

> 检查器：EvoGuard inspect（确定性规则，不调用模型）  
> 输入：5 条轨迹（../tests/fixtures/trace_synthetic_bad.json, ../tests/fixtures/trace_hallucination.json）

## 一、总览

| 严重度 | 条数 |
|---|---|
| error | 2 |
| warn | 6 |
| info | 0 |

| 规则 | 命中 | 严重度 |
|---|---|---|
| `hallucinated_number` 答案里出现工具没返回过的数字 | 4 | warn |
| `error_swallowed` 工具报错后仍然给出了最终答案 | 1 | error |
| `write_before_read` 写操作发生在读取之前 | 1 | warn |
| `repeated_tool_call` 同一工具同参数被反复调用 | 1 | warn |
| `empty_answer` 宣称完成但没有产出答案 | 1 | error |

## 二、问题明细

### [ERROR] 宣称完成但没有产出答案

- 轨迹 `t-empty-0004` ｜ agent `synthetic-agent` ｜ task `-`
- 事实：答案为空：'{}'
- 建议：检查 Agent 的收尾分支；空答案不要按成功上报
- **可能是误报**：流式输出被截断、或答案写在别的字段里（非 answer/completion）。

### [ERROR] 工具报错后仍然给出了最终答案

- 轨迹 `t-swallow-0001` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `order.get_order`
- 事实：工具 order.get_order 报错(timeout)，之后没有重试就给出了答案
- 建议：失败分支要显式降级或重试；把工具错误写进答案，别静默吞掉
- **可能是误报**：该工具本来就允许失败（可选增强），且答案不依赖它。

### [WARN] 答案里出现工具没返回过的数字

- 轨迹 `t-swallow-0001` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `chat synthetic`
- 事实：这些数字在任何工具输出里都找不到：999.0
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN] 答案里出现工具没返回过的数字

- 轨迹 `t-writefirst-0002` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `chat synthetic`
- 事实：这些数字在任何工具输出里都找不到：100.0
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN] 答案里出现工具没返回过的数字

- 轨迹 `t-repeat-0003` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **5** 步 `chat synthetic`
- 事实：这些数字在任何工具输出里都找不到：7
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN] 答案里出现工具没返回过的数字

- 轨迹 `run_5cf841360030` ｜ agent `planner` ｜ task `training`
- 证据位置：第 **5** 步 `chat planner`
- 事实：这些数字在任何工具输出里都找不到：777
- 建议：要求 Agent 只引用工具返回的数值；
- **可能是误报**：**数字可能是算出来的**（如 小计×折扣）或来自 prompt 常量 —— 所以这条只报警不判错，必须人工核实一次。

### [WARN] 同一工具同参数被反复调用

- 轨迹 `t-repeat-0003` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `kb.search`
- 事实：kb.search 用同一组参数被调用 4 次
- 建议：加去重/缓存；轮询类工具要显式标 polling 语义
- **可能是误报**：该工具用于轮询等待（如查任务状态），重复是设计如此。

### [WARN] 写操作发生在读取之前

- 轨迹 `t-writefirst-0002` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `order.settle`
- 事实：第一次调用就是写操作 order.settle，之前没有任何读取
- 建议：写之前先读一次当前状态，或确认入参已含前置信息
- **可能是误报**：写操作本身是幂等的、或入参已包含全部前置信息。

## 三、这份报告不能说明什么

1. 只覆盖 5 条规则，规则集之外的问题它看不见。
2. 规则只看轨迹，**看不到 Agent 的 prompt 与配置** —— 那属于静态检查。
3. `warn` 级别需要人工核实，尤其 `hallucinated_number`（数字可能是算出来的）。
