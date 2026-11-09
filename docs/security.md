# 安全层：做了什么、没做什么

一句话：**这套东西是「检测 + 降风险」，不是「保证不被攻破」。**
任何地方都不许把它写成「已防住提示注入」。

攻击面不止「用户输入」三条口子：用户输入、工具返回的外部内容（网页/文档/第三方 API）、
以及 Agent 自己吐给浏览器的输出。三层分别对应这三条口子。

## 三层结构

| 层 | 位置 | 做什么 | 默认状态 |
|---|---|---|---|
| 定义层 | 检查器静态规则（20 条） | 看 prompt/工具/配置有没有基本防线：拒答边界、工具结果是数据不是指令、写操作审批、危险 shell、幂等键、预算 | 只读检查 |
| 行为层 | 检查器轨迹规则（security 组 6 条）+ 运行时钩子 | 看**已经发生的执行**里有没有注入、越权、外带、凭据、危险命令、XSS；运行时在 6 个点做扫描与执行控制 | 检测开、改写关（`audit`） |
| 边界层 | `api/app.py` | 输出侧安全头（`nosniff` / `X-Frame-Options: DENY` / CSP）、`Content-Type` 固定、`encode_for()` 按落点转义 | 常开 |

## 运行时钩子（`ai-service/evoguard/runtime.py`）

1. `run_once`：任务输入进 Agent 前扫描；`enforce` 下高危直接判该次运行被拦（`security_blocked:user_input`）。
2. `Ctx.call` 入口：最小权限校验，工具不在该 Agent 白名单内则拒绝（`tool_not_allowed`）。
3. `Ctx.call` 出口：工具返回扫描；**只有** `enforce` + `AGENTLAB_SECURITY_WRAP=1`
   且命中 error 级时，才用 `mark_payload()` 把可疑值包成不可信数据（不改结构）。
   默认只记账 —— `action="blocked"` 在这里**不阻止**结果进入 Agent（见下方 S01 契约）。
4. `Ctx.answer`：模型输出扫描，只记账不改文本 —— 答案还要被解析成 JSON 参与判定，改写会让评测结论失真。
5. `run_once` 入口 / 审批点：高危输入终止运行、恢复期写操作要求审批，两处都是真实执行点。
6. 上述每个执行点都落一条 `security_effects` 记录，与检测日志按 `run_id` 关联。

轨迹里始终保存**原始**工具输出；检测结果落 `security_events` 表，
**真实执行后果**落 `security_effects` 表。这样「评测看真值」「安全看原始内容」
「报告看拦没拦住」三件事互不污染。

## 三种模式

`AGENTLAB_SECURITY_MODE=off | audit | enforce`，默认 **audit**。

- `audit`：只检测、只记账，**不改任何内容** → 历史实验数字仍然可比。
- `enforce`：检测 + 拦截（越权工具直接拒、高危输入直接拦、输出按落点编码）。
  包裹工具结果另由 `AGENTLAB_SECURITY_WRAP=1` 控制，默认关。
- `off`：完全关闭，用于对照。

为什么默认 audit：这个项目的核心资产是可复现的对照实验。默认改写会让
「模型能力」和「安全层行为」混成一个数字，之前的 1080 run 也就失去可比性。

配置必须记全：除了 `AGENTLAB_SECURITY_MODE`，还有 `AGENTLAB_SECURITY_WRAP`、
`AUTO_APPROVE`、`AGENTLAB_ALLOW_AUTO_APPROVE`、工具契约与输出编码落点。
只记录一个 `mode` 字段就宣称「配置完整」是不成立的。

## 检测建议 ≠ 拦截成功（S01 契约）

**复现过的反例**：`enforce` 且未设 `AGENTLAB_SECURITY_WRAP` 时，
`guard_tool_output("kb.search", 含注入的文档)` 返回 `action="blocked"`、
命中 `injection_override`，但 `text` 与输入**逐字相同**，`Ctx.call` 也没停止调用。
旧版 `defense_demo.py` 把这个 `action` 算成「拦截成功」，属于高估。

现在每个 guard 都返回六字段契约：

| 字段 | 含义 |
|---|---|
| `detected` | 有没有命中特征 |
| `decision` | 这一层决定做什么：`deny / allow / encode / wrap / report / none` |
| `attempted` | 有没有尝试执行控制动作 |
| `executed` | 控制动作是否真的执行了 |
| `effect` | 真实后果：`call_stopped / body_wrapped / body_encoded / body_marked / unchanged / reported_only / none` |
| `reason` | 为什么是这个后果，尤其要写清「为什么没生效」 |

`action` 保留仅为兼容旧调用方，**语义降级为检测建议**。
只有真实执行点（`Ctx.call` / `run_once` / 演示的执行点）写下的
`security_effects.effect` 才能作为「拦住了」的判据。
**未改正文、未停止调用时，报告不得统计为拦截成功。**

```bash
python -m unittest tests.test_security_contract -q   # 8 项：反例 + 六字段 + 执行点留痕
```

## 多 Agent 协作

`check_peer(from_agent, allowed_peers)`：未声明的对端消息一律当不可信数据。
多 Agent 系统最容易被忽略的入口是「另一个 Agent 说的话」——它通常被当成系统内部消息直接信任。

**现状要如实说**：本仓库的运行时是单 Agent（`agents.py` 里没有 handoff / 委托路径），
所以这层目前是**库级能力 + 离线规则**，没有在真实多 Agent 链路上跑通。
真正接多 Agent 时，还需要给消息加来源签名与消息级权限，这一条没做。

## 延迟

纯预编译正则 + 前置子串过滤（命中触发字符才跑正则），无第三方依赖、不联网。

| 输入 | 耗时 |
|---|---|
| 1 KB | 0.044 ms |
| 10 KB | 0.31 ms |
| 50 KB | 1.84 ms |

真实运行里的增量：每次 run 约 4 次扫描 ≈ **51 µs**。
对 mock（每次 run 1.15 ms）是 4.4%，看起来高但绝对量可忽略；
对真实模型（每次 run 约 627 ms）是 **0.008%**。
数据在 `experiments/p0_09_20261109/security_bench.json`，测试里有 1.5ms/10KB 的预算断言。

## 已知边界（不粉饰）

1. **特征匹配，改个说法就能绕过。** 换同义词、拆字、base64、多语言混写都能逃过。
2. **只覆盖已知形态**：本文件的检测项来自真实事故与常见攻击剧本，不是形式化证明。
3. **`enforce` 原则上会改行为**（拦截、编码、包裹）。实测：同一数据集、mock 后端下
   `off` / `enforce` / `enforce+wrap` 三种模式指标**完全一致**（单故障 0.5/24.0/89.2/86.3，
   复合 3.3/86.7/86.7，晋升 7；见 `experiments/p0_09_20261109/mode_compare.json`）——
   因为本仓库的 Agent 是规则式、不读被包裹的文本。
   真实模型读 prompt 时包裹会改变输入，这个代价**没有测过**，别默认它没有。
4. **审批已经能真执行，沙箱还没有**：
   - `enforce` 模式下，恢复期重放写操作必须有人工审批（`AGENTLAB_ALLOW_AUTO_APPROVE=1`
     才允许显式覆盖），否则抛 `E_APPROVAL_REQUIRED` 并中断本次恢复。
   - 默认的 `audit` 模式仍自动放行，但每次放行都记一条 `approval_bypassed` 事件 ——
     报告里能看到「审批被绕过多少次」。实测闭环里是 **825 次**。
   - 代价实测：真执行后单故障 D 从 86.3% 降到 59.8%（-26.5pp），复合两步链从 86.7% 降到 46.7%（-40pp），
     `approval_required` 触发 633 次。数据在 `experiments/p0_09_20261109/mode_compare.json`。
   - **沙箱仍然没有**：危险命令现在能在调用前被拦（参数级），但真要有 shell 工具跑起来，
     隔离层不存在。
5. **没有真正的网络边界**：`api/app.py` 加了安全头，但它不是 WAF，不做限流、认证与租户隔离。
6. **多 Agent 只到库级**，见上。

真要提升强度，优先级是：工具权限最小化 → 写操作审批与沙箱 → 输出编码与 CSP → 最后才是特征检测。
前两项里，**工具白名单拒绝**与**恢复期写操作审批**已经在 `enforce` 下真执行（见上第 4 条）；
**通用沙箱 / 网络隔离**和**可暂停续跑的审批工作流**仍未实现（推进计划 S04）。
输出编码与特征检测已具备，但编码是否在真实落点生效要逐点核验，
不能只看 `guard_model_output` 的返回值 —— 运行库内部刻意不改写答案。

旧版防御演示 v1 的 `enforced=4` / `leaked=3` 是已知高估与虚报，冻结原件保留在
`experiments/defense_demo/`；当前报告在 `experiments/defense_demo_v2/`，
按真实执行后果计量（`sink_hits` / `call_blocked` / `body_changed` / `dangerous_executed`）。
