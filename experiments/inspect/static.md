# Agent 静态检查报告（不执行 Agent）

> 检查器：EvoGuard inspect --agent（确定性规则，不调用模型、不运行被检代码）

## 一、被检对象

| Agent | 来源 | 类型 | 工具数 | prompt 字数 | 置信度 |
|---|---|---|---|---|---|
| billing-agent-bad | `<repo-root>\tests\fixtures\agent_bad.json` | json | 5 | 40 | **high** |
| billing-agent-good | `<repo-root>\tests\fixtures\agent_good.json` | json | 2 | 103 | **high** |

> **confidence=low 表示定义是从源码里猜出来的** —— prompt 边界和工具清单都可能不全，
> 这类结论只能当线索，不能当事实。

## 二、总览

| 严重度 | 条数 |
|---|---|
| error | 3 |
| warn | 12 |
| info | 1 |

| 分组 | 条数 | 说明 |
|---|---|---|
| prompt | 4 | 提示词层：没写清楚的边界，模型只能自己猜 |
| tool | 7 | 工具定义层：模型选错工具、误用写操作的根源 |
| config | 5 | 配置层：超时 / 重试 / 上限 / 输出约束 |

## 三、问题明细（按分组）

### prompt · 提示词层：没写清楚的边界，模型只能自己猜

**[WARN] prompt 里没有「不知道就说不知道」的约束**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`system_prompt`
- 事实：没有找到任何「不确定/查不到时如何作答」的约束
- 怎么改：加一句：拿不到数据就明说查不到，不要推测；给出兜底话术
- **可能是误报**：约束可能写在别处（工具描述、后置校验、另一个 prompt 片段）。

**[WARN] 有工具但 prompt 没说什么时候用**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`system_prompt`
- 事实：声明了 5 个工具，但 prompt 里没有工具使用规则
- 怎么改：写清：什么情况下必须先调工具、拿到什么才能回答、工具失败怎么办
- **可能是误报**：策略可能写在工具描述里（description 里写了「何时使用」）。

**[ERROR] prompt 里含个人信息**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`system_prompt`
- 事实：检出：手机号
- 怎么改：示例数据换成虚构值；prompt 里不出现真实个人信息
- **可能是误报**：可能是脱敏不彻底或测试数据；需要人工确认。

**[WARN] 有读取外部内容的工具，但 prompt 没说「工具结果是数据不是指令」**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`kb.search`
- 事实：1 个工具会读取外部内容，prompt 里没有「工具结果只是数据」的边界声明
- 怎么改：在 system prompt 里明确：工具返回内容是不可信数据，其中的指令一律不执行；把外部内容与指令上下文分开放，并做分隔符/清洗
- **可能是误报**：外部内容可能已经在上游做过清洗/隔离；也可能 prompt 写在别处（模板、外部文件）而解析器没读到。低置信度的仓库扫描一律不判这条。

### tool · 工具定义层：模型选错工具、误用写操作的根源

**[ERROR] 有写操作工具，但没有任何审批/确认机制**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`order.settle, refund.create, order.settle_v2`
- 事实：3 个写操作工具，配置里没有审批字段
- 怎么改：写操作走审批（human-in-the-loop）；至少要幂等键 + 二次确认
- **可能是误报**：写操作的审批在框架层（业务网关、数据库权限），不在 Agent 定义里。

**[WARN] 工具没有描述**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`order.settle_v2, kb.search`
- 事实：2/5 个工具没有 description
- 怎么改：每个工具写清：做什么、什么时候用、返回什么、有什么副作用
- **可能是误报**：有些框架从函数 docstring 生成描述，解析器没读到。

**[WARN] 写操作的描述里没提副作用**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`order.settle, order.settle_v2`
- 事实：写工具的描述里没有副作用说明：order.settle, order.settle_v2
- 怎么改：描述里明确写「会修改数据，不可撤销」，让模型先确认
- **可能是误报**：副作用写在参数说明或系统提示里；也可能由框架统一处理审批。

**[WARN] 工具参数没有类型定义**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`shell.exec(cmd), kb.search(q)`
- 事实：参数缺 type：shell.exec(cmd), kb.search(q)
- 怎么改：补全 JSON Schema（type/enum/required），让调用前就能校验
- **可能是误报**：框架可能用运行时签名推断类型（Pydantic / dataclass），解析器没读到。

**[WARN] 工具名或描述高度相似**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`order.settle / order.settle_v2`
- 事实：疑似重复工具：order.settle / order.settle_v2
- 怎么改：重命名或合并；描述里写清两者边界，否则模型只能猜
- **可能是误报**：相似是业务本身决定的（如 get_order / get_order_detail），需要描述里写清区别。

**[ERROR] 有执行任意命令/代码的工具，且没有沙箱说明**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`shell.exec`
- 事实：存在命令/代码执行工具：shell.exec
- 怎么改：改成白名单命令；或明确沙箱 + 无网络 + 只读挂载
- **可能是误报**：工具内部有白名单或沙箱，但没写在定义里；需要人工确认实现。

**[WARN] 写操作工具没有幂等键**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`order.settle`
- 事实：写操作工具 order.settle 的参数/描述里没有幂等键
- 怎么改：加 idempotency_key / request_id；超时后先用只读接口查状态，确认没执行成功再重试
- **可能是误报**：服务端可能自己做了去重；也可能是天然幂等的覆盖式写（PUT）。低置信度的仓库扫描一律不判这条。

### config · 配置层：超时 / 重试 / 上限 / 输出约束

**[WARN] 没有超时配置**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`config`
- 事实：配置里没有 timeout
- 怎么改：给工具调用和模型调用分别设超时，并定义超时后的降级
- **可能是误报**：超时可能由框架默认值或网关统一设置。

**[WARN] 没有重试/失败策略**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`config`
- 事实：配置里没有 retries / retry_policy
- 怎么改：对瞬时错误做有限重试 + 退避；区分可重试与不可重试
- **可能是误报**：重试由框架或网关负责（如 SDK 默认重试）。

**[WARN] 没有最大轮次上限**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`config`
- 事实：配置里没有 max_iterations / max_steps
- 怎么改：设硬上限；到达上限要显式失败并说明原因，不要静默截断
- **可能是误报**：上限可能在编排层（如 LangGraph 的 recursion_limit）设置。

**[WARN] 没有输出结构约束**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`config`
- 事实：没有 output_schema，prompt 里也没提输出格式
- 怎么改：用 JSON Schema / response_format 强约束；解析失败要显式报错
- **可能是误报**：输出结构由调用方解析时校验（或在 prompt 里用示例强约束）。

**[INFO] 没有单次运行的 token / 费用上限**

- 对象：`billing-agent-bad`（置信度 high）
- 证据：`config keys: model, temperature`
- 事实：配置里没有单次运行的 token / 费用上限
- 怎么改：在每次模型调用前检查累计消耗；超预算直接中止该次运行，并把预算做成 per-run / per-tenant 两级
- **可能是误报**：预算可能由网关、云厂商配额或外部计费系统统一管控；单次任务也可能天然很短。本条只是提示确认，级别为 info。低置信度的仓库扫描一律不判这条。

## 四、这份报告不能说明什么

1. 只看定义，**不知道 Agent 实际会怎么执行** —— 那需要轨迹（`--trace`）。
2. 规则集之外的风险它看不见（本报告只跑了配置里启用的规则）。
3. 从源码提取的定义置信度低，结论需要人工复核。
