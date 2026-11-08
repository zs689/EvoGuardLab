# Agent 离线检查报告

> 检查器：EvoGuard inspect（确定性规则，不调用模型）  
> 输入：19 条轨迹（../tests/fixtures/trace_synthetic_bad.json, ../tests/fixtures/trace_clean.json, ../tests/fixtures/trace_retry_ok.json）

## 一、总览

| 严重度 | 条数 |
|---|---|
| error | 13 |
| warn | 28 |
| info | 0 |

| 分组 | 条数 | 说明 |
|---|---|---|
| answer | 7 | 输出层：答案本身不对 |
| tool | 18 | 工具层：调用或结果的使用有问题 |
| flow | 11 | 流程层：步骤顺序 / 循环 / 过早收尾 |
| resource | 1 | 资源层：token 与耗时 |
| structure | 4 | 结构层：多 Agent / 并行 / 错误传播 |

> 排查顺序建议：先看**输出层**（结论错了），再往**工具层/流程层**倒推原因。

| 规则 | 分组 | 命中 | 严重度 |
|---|---|---|---|
| `tool_result_unused` 工具返回了实质内容，但后续完全没引用 | tool | 6 | warn |
| `tool_name_unknown` 调用了工具清单里不存在的工具 | tool | 5 | warn |
| `hallucinated_number` 答案里出现工具没返回过的数字 | answer | 4 | warn |
| `premature_finish` 没有任何有效工具结果就下定论 | flow | 4 | error |
| `error_swallowed` 工具报错后仍然给出了最终答案 | tool | 3 | error |
| `write_before_read` 写操作发生在读取之前 | flow | 2 | warn |
| `loop_detected` 同一工具同参数被反复调用 | flow | 2 | warn |
| `parallel_write_conflict` 相邻两步都是写操作 | structure | 2 | error |
| `dependency_mismatch` 下游工具的参数不是上游返回的值 | tool | 2 | warn |
| `empty_answer` 宣称完成但没有产出答案 | answer | 1 | error |
| `pii_leak` 答案里出现未脱敏的个人信息 | answer | 1 | error |
| `cycle_detected` 工具调用陷入 A→B→A→B 循环 | flow | 1 | warn |
| `token_blowup` 单条轨迹 token 超预算 | resource | 1 | warn |
| `tool_arg_incomplete` 工具参数是空的或缺少关键项 | tool | 1 | warn |
| `self_report_contradiction` 答案自称成功，但轨迹里的证据不支持 | answer | 1 | error |
| `retry_storm` 同一个失败被连续重试，没有熔断 | flow | 1 | error |
| `silent_stall` 连续多步无有效产出、无报错、不终止 | flow | 1 | warn |
| `write_timeout_ambiguous` 写操作超时后又用同样参数重试 | structure | 1 | warn |
| `retry_on_permanent_error` 把永久性错误当成瞬时故障继续重试 | tool | 1 | warn |
| `hallucination_propagation` 下游步骤用了上游没有出处的值 | structure | 1 | warn |

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

### [ERROR][tool] 工具报错后仍然给出了最终答案

- 轨迹 `t-liereport-0012` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `db.migrate`
- 事实：工具 db.migrate 报错(destructive operation failed)，之后没有重试就给出了答案
- 建议：失败分支要显式降级或重试；把工具错误写进答案，别静默吞掉
- **可能是误报**：该工具本来就允许失败（可选增强），且答案不依赖它。

### [ERROR][tool] 工具报错后仍然给出了最终答案

- 轨迹 `t-retrystorm-0013` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `kb.search`
- 事实：工具 kb.search 报错(upstream timeout)，之后没有重试就给出了答案
- 建议：失败分支要显式降级或重试；把工具错误写进答案，别静默吞掉
- **可能是误报**：该工具本来就允许失败（可选增强），且答案不依赖它。

### [ERROR][structure] 相邻两步都是写操作

- 轨迹 `t-parallel-write-0007` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `order.settle`
- 事实：连续两次写操作：order.settle → order.settle
- 建议：写操作之间插入状态读取或幂等键；确认是否需要串行化
- **可能是误报**：写操作之间本来就不需要读（如批量创建）；也可能是设计如此的流水线。

### [ERROR][structure] 相邻两步都是写操作

- 轨迹 `t-writetimeout-0015` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `order.settle`
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

### [ERROR][flow] 没有任何有效工具结果就下定论

- 轨迹 `t-liereport-0012` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `chat synthetic`
- 事实：答案已产出，但全程没有一个成功的工具结果
- 建议：拿不到数据时应当明说「查不到」而不是给结论
- **可能是误报**：任务允许零工具（纯知识问答）；或工具结果确实为空且 Agent 如实说了「查不到」。

### [ERROR][flow] 没有任何有效工具结果就下定论

- 轨迹 `t-retrystorm-0013` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **5** 步 `chat synthetic`
- 事实：答案已产出，但全程没有一个成功的工具结果
- 建议：拿不到数据时应当明说「查不到」而不是给结论
- **可能是误报**：任务允许零工具（纯知识问答）；或工具结果确实为空且 Agent 如实说了「查不到」。

### [ERROR][flow] 没有任何有效工具结果就下定论

- 轨迹 `t-stall-0014` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `chat synthetic`
- 事实：答案已产出，但全程没有一个成功的工具结果
- 建议：拿不到数据时应当明说「查不到」而不是给结论
- **可能是误报**：任务允许零工具（纯知识问答）；或工具结果确实为空且 Agent 如实说了「查不到」。

### [ERROR][flow] 同一个失败被连续重试，没有熔断

- 轨迹 `t-retrystorm-0013` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `kb.search`
- 事实：连续 4 步报错、中间没有一次成功（阈值 3）
- 建议：加运行时强制的熔断：连续失败计数达到上限就停止付费调用，成功才清零；配合指数退避与单次运行的费用上限
- **可能是误报**：固定次数的重试本身是正确做法；外部服务持续故障也可能造成连续失败。本条只提示「连续失败没有触发停止」，是否已做退避/熔断要看配置和监控。

### [ERROR][answer] 答案自称成功，但轨迹里的证据不支持

- 轨迹 `t-liereport-0012` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `db.migrate`
- 事实：答案自称完成/通过，但轨迹里有 1 个失败步骤且未恢复（最后：db.migrate，destructive operation failed）
- 建议：不要采信 Agent 的自述；用工具返回状态和独立日志核对最终结论，失败步骤必须在答案里如实披露
- **可能是误报**：成功词可能只是客套话；也可能 Agent 确实修复了，但轨迹没记录全。结论要以基础设施日志和状态为准，不能只凭这条规则定性。

### [WARN][flow] 工具调用陷入 A→B→A→B 循环

- 轨迹 `t-cycle-0008` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `order.get_order`
- 事实：检测到循环：kb.search → order.get_order 连续重复两轮
- 建议：给循环加最大次数与退出条件；把「已经试过」写进上下文
- **可能是误报**：工作流本身就是交替的（读→写→读→写），属于正常设计。

### [WARN][tool] 下游工具的参数不是上游返回的值

- 轨迹 `t-propagation-0017` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `order.get_order`
- 事实：order.get_order 的参数 order_id='A1001' 在上游输出里找不到
- 建议：参数要从上游结果里取；构造型参数请在 Agent 里显式记录来源
- **可能是误报**：参数可能是模型构造的（拼接字符串、换算单位），不一定是直接拷贝。

### [WARN][tool] 下游工具的参数不是上游返回的值

- 轨迹 `run_f4e6b07e6903` ｜ agent `xref` ｜ task `发票时限`
- 证据位置：第 **4** 步 `kb.search_all`
- 事实：kb.search_all 的参数 topic='发票时限' 在上游输出里找不到
- 建议：参数要从上游结果里取；构造型参数请在 Agent 里显式记录来源
- **可能是误报**：参数可能是模型构造的（拼接字符串、换算单位），不一定是直接拷贝。

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

### [WARN][structure] 下游步骤用了上游没有出处的值

- 轨迹 `t-propagation-0017` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `order.get_order`
- 事实：order.get_order 的参数引用了 A1001，但前面 1 个工具输出里都没有它
- 建议：跨 Agent 传递的值要带来源标记；关键字段在交接处做一次存在性校验，别把上游文本直接当结构化事实
- **可能是误报**：标识可能来自用户输入但任务字段没记全，也可能是合法常量或按规则生成的编号。只查 ID 形态、不查普通数字，就是为了压低这类误报。

### [WARN][flow] 同一工具同参数被反复调用

- 轨迹 `t-repeat-0003` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `kb.search`
- 事实：kb.search 用同一组参数被调用 4 次（阈值 3）
- 建议：加去重/缓存；轮询类工具要显式标 polling 语义
- **可能是误报**：该工具用于轮询等待（如查任务状态），重复是设计如此。

### [WARN][flow] 同一工具同参数被反复调用

- 轨迹 `t-retrystorm-0013` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **4** 步 `kb.search`
- 事实：kb.search 用同一组参数被调用 4 次（阈值 3）
- 建议：加去重/缓存；轮询类工具要显式标 polling 语义
- **可能是误报**：该工具用于轮询等待（如查任务状态），重复是设计如此。

### [WARN][tool] 把永久性错误当成瞬时故障继续重试

- 轨迹 `t-permanenterr-0016` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `kb.search`
- 事实：第 1 步是永久性错误（401 unauthorized: invalid api key），第 2 步仍重复调用 kb.search
- 建议：在工具层标注 error_type；permanent / input_error 直接停止并把错误回传给模型修正参数，只有 transient 才重试
- **可能是误报**：错误信息措辞可能含糊；也可能重试期间权限被修复、资源被创建出来了。以错误码为准，不要只看文本。

### [WARN][flow] 连续多步无有效产出、无报错、不终止

- 轨迹 `t-stall-0014` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `pipeline.next`
- 事实：连续 3 个工具步骤没有实质返回，也没报错或收尾
- 建议：给循环加最大空转次数与显式超时；空结果要写日志并向上抛，不要让调用链静默挂起
- **可能是误报**：轮询/等待类工具返回空是正常语义；流式响应的中间帧也可能是空。轮询工具应显式标注 polling 语义，本条对它不适用。

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

### [WARN][tool] 调用了工具清单里不存在的工具

- 轨迹 `t-liereport-0012` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `db.migrate`
- 事实：db.migrate 不在允许的工具清单里（共 1 次）
- 建议：在工具注册层做白名单；未知工具直接拒绝
- **可能是误报**：未配置 allowed_tools 时本条不生效；清单本身也可能过期。

### [WARN][tool] 调用了工具清单里不存在的工具

- 轨迹 `t-stall-0014` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `pipeline.next`
- 事实：pipeline.next 不在允许的工具清单里（共 3 次）
- 建议：在工具注册层做白名单；未知工具直接拒绝
- **可能是误报**：未配置 allowed_tools 时本条不生效；清单本身也可能过期。

### [WARN][tool] 调用了工具清单里不存在的工具

- 轨迹 `run_2965e36537d2` ｜ agent `xref` ｜ task `发票时限`
- 证据位置：第 **2** 步 `kb.search_all`
- 事实：kb.search_all 不在允许的工具清单里（共 1 次）
- 建议：在工具注册层做白名单；未知工具直接拒绝
- **可能是误报**：未配置 allowed_tools 时本条不生效；清单本身也可能过期。

### [WARN][tool] 调用了工具清单里不存在的工具

- 轨迹 `run_f4e6b07e6903` ｜ agent `xref` ｜ task `发票时限`
- 证据位置：第 **2** 步 `kb.search_all`
- 事实：kb.search_all 不在允许的工具清单里（共 2 次）
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

### [WARN][tool] 工具返回了实质内容，但后续完全没引用

- 轨迹 `t-propagation-0017` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **3** 步 `order.get_order`
- 事实：order.get_order 返回的 2 个值（如 10.0）在答案与后续调用里都没出现
- 建议：确认这次调用是否必要；如果结果该影响答案，说明 Agent 漏读了
- **可能是误报**：结果可能只用于「确认存在」；短的确认型返回（{"ok":true}）本条不判。

### [WARN][flow] 写操作发生在读取之前

- 轨迹 `t-writefirst-0002` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `order.settle`
- 事实：第一次调用就是写操作 order.settle，之前没有任何读取
- 建议：写之前先读一次当前状态，或确认入参已含前置信息
- **可能是误报**：写操作本身幂等、或入参已包含全部前置信息。

### [WARN][flow] 写操作发生在读取之前

- 轨迹 `t-writetimeout-0015` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **1** 步 `order.settle`
- 事实：第一次调用就是写操作 order.settle，之前没有任何读取
- 建议：写之前先读一次当前状态，或确认入参已含前置信息
- **可能是误报**：写操作本身幂等、或入参已包含全部前置信息。

### [WARN][structure] 写操作超时后又用同样参数重试

- 轨迹 `t-writetimeout-0015` ｜ agent `synthetic-agent` ｜ task `-`
- 证据位置：第 **2** 步 `order.settle`
- 事实：order.settle 第 1 步超时后，第 2 步用完全相同的参数再次调用
- 建议：写工具带幂等键（idempotency_key / request_id）；超时后先查状态再决定是否重试，不要盲重试
- **可能是误报**：调用方可能带了幂等键，或第一次确实没执行成功（这时重试是对的）。本条是提示「需要确认」，不是判定「一定重复了」。

## 三、这份报告不能说明什么

1. 只覆盖 25 条规则（5 组），规则集之外的问题它看不见。
2. 规则只看轨迹，**看不到 Agent 的 prompt 与配置** —— 那属于静态检查。
3. `warn` 级别需要人工核实，尤其 `hallucinated_number`（数字可能是算出来的）。
