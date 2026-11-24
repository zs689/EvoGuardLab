# 可恢复审批与真实副作用口径（会话 5/6）

对应推进计划 §6 S04 / S05。代码：`ai-service/evoguard/approval.py`、
`ai-service/evoguard/real_tasks.py`、`runtime.Ctx.approval_gate`。
演示：`scripts/real_settlement_demo.py` → `experiments/real_settlement_v1/`。
验收：`tests/test_approval.py`（15 项）。

## 一、审批状态机

`approval_requests` 每条绑定：

| 字段 | 作用 |
|---|---|
| `run_id` / `tool` | 哪次运行的哪个写操作 |
| `args_digest` / `args_canonical` | 规范化参数摘要（`504` 与 `504.0`、键值顺序、字符串首尾空白都归一） |
| `resource_scope` | 从参数摘出的订单号 / 路径 / 资源标识 |
| `candidate_version` / `idempotency_key` | 候选版本与幂等键 |
| `policy_digest` | 白名单 + 工具契约指纹，权限一变旧审批立即失效 |
| `status` | `pending → approved → consumed`；或 `rejected` / `expired` |
| `expires_at` | 默认 24 小时（沿用 P0 03 口径） |
| `approved_by` / `decided_at` / `consumed_at` | 批准人、决策时间、一次性消费时间 |
| `checkpoint_id` | 暂停时的检查点，批准后从原检查点续跑 |
| `reason` | 每一步都写清为什么 |

**一次性消费**：`approved` 被 `require()` 或 `consume()` 消费后立刻置 `consumed`，
再用同一 `request_id` 会抛 `E_APPROVAL_INVALID`。

复用前全部校验（任一不满足即拒绝，且留原因）：参数摘要、工具、run_id、权限指纹、
是否过期、状态是否 `approved`、是否已被消费。

状态在 SQLite 里，不在内存：关掉 `Store` 再重开，`resume(run_id)` 能读回检查点、
pending 与 approved；**恢复只还原状态，不自动继续写操作**。

**待批票不重复开**：同一 `run_id` + 工具 + 参数摘要重试时，复用已有的 `pending`
票并返回同一个 `request_id`；若这期间权限/契约指纹变了，旧票标 `expired`
（原因写「权限/工具契约已变更，待批票作废」）再开新票。理由很实际：
不这么做，Agent 每重试一次就多一张同一操作的票，人工面对一堆票不知道该批哪张。

## 一（补）、结算账本的幂等键

`settlement_ledger` 以 `idempotency_key` 为主键，同键同内容只落一笔。
**同键不同内容不再静默通过**：金额（浮点按 `1e-6` 比）或订单号不一致时抛
`E_LEDGER_CONFLICT`（`LedgerConflict`），并写明两次的订单与金额。
原因：`INSERT OR IGNORE` 会把「同一个键、金额被改过」当成「已结过」返回，
金额类书证最怕这种静默覆盖。

## 二、统一执行门禁

`runtime.Ctx` 新增可选 `approval_gate`。挂上之后，`Ctx.call` 对
`WRITE_TOOLS` 的写入统一走 `ApprovalGate.require()`：

- **首次写入**（`recovering=False`）也要审批 —— 补上旧实现只拦恢复期重放的缺口；
- **恢复期重放写入**走同一个门禁，不再有两套逻辑；
- 暂停时写一条 `security_effects`（`effect=call_stopped`，含 request_id），
  放行时写 `approval_consumed`；工具在暂停期间**没有执行**。

默认 `approval_gate=None`，历史实验行为与冻结数字完全不变（309 项回归里有专门
一条测这个）。真实副作用任务显式挂上才启用。

## 三、真实副作用链路

`real_tasks.py` 的 `run_settlement()`：

1. `LocalOrderService`：`127.0.0.1` 随机端口的 `ThreadingHTTPServer`，
   `urllib` 显式绕过系统代理读 `/order/<id>`；
2. `load_rules()`：只读 `tests/fixtures/settlement_rules.json`，路径越出项目沙箱直接拒绝；
3. `compute_total()`：按 `rules-v1` 算小计 / 折扣 / 总额，留 `rule_version` 与计算明细；
4. `ApprovalGate.require()`：唯一写操作门禁；
5. `SettlementLedger.settle()`：`idempotency_key` 主键 + `INSERT OR IGNORE` 写专用表。

**提交成功但响应丢失**：`lose_response=True` 在账本写入后抛 `ResponseLost`，
异常对象带已提交结果（含幂等键）。调用方拿同一个键查询或重试，
`settle()` 返回 `created=False`，`settlement_ledger` 仍只有一笔。

## 四、5 个验收情形（全部通过）

| 情形 | 通过标准 | 证据 |
|---|---|---|
| 首次写入没有审批 | 没有落库副作用，状态可定位 | pending + request_id + checkpoint_id + `count=0` |
| 审批前后参数变化 | 摘要不匹配，原审批不复用 | `E_APPROVAL_INVALID`，旧审批仍 `approved` 且未消费 |
| 提交成功但响应丢失 | 同一幂等键重试仍只有一笔 | `created=False`，`count=1` |
| 拒绝与过期 | 任务不自动继续，事件有原因 | `rejected` / `expired` + `reason`，工具未执行 |
| 暂停后进程重启 | 恢复检查点与批准状态，不重复写入 | `resume()` 读回，首次写入成功、二次 `created=False` |

额外守住：权限变更、重复消费、`504` vs `504.0` 规范化、沙箱路径越界、
首次写入与重放写入同一门禁、默认 runtime 行为不变。

## 五、不能怎么用

- ❌ 不是生产级审批工作流：没有身份认证、多租户、审计访问控制、限流与 SLA。
- ❌ 不是「防住越权」的证明：白名单与权限契约仍由调用方传入，本模块只校验摘要没变。
- ❌ 不是真实金融结算：订单是回环测试服务、规则是沙箱测试文件、账本是专用 SQLite 表。
- ❌ 不能与历史 1080 run 评测比大小：数据集、配置、后端都不同。
- ✅ 可以写：在这 5 个受控情形下，暂停 / 批准 / 拒绝 / 过期 / 重启 / 响应丢失
  的行为都有可核对证据。

## 六、仍未完成

- 会话 7：独立授权策略（从事先冻结的策略取白名单，不从轨迹反推）与外部轨迹小批量。
- 计划 §6「按技能 safety 声明决定审批」：目前 `safety` 声明没有参与门禁判定。
- 首次写入审批在**默认** runtime 里仍未启用（只在显式挂 gate 的真实任务链上生效）；
  要在全部运行上启用，需重跑实验并重算历史数字，不能悄悄改。
- 2×2 消融、30+30 场景扩样、1080 run 重跑与公平消融仍未做。

## 七、复核命令

```bash
cd <repo-root>
python -m unittest tests.test_approval -q
python scripts/real_settlement_demo.py
```
