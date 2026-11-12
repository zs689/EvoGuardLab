# 当前能力表（评审 §9「文档同步」要求）

> 一张表回答：这个能力**默认开不开**、**在哪一行生效**、**靠哪个测试证明**、**边界在哪**。
> 评审原话：「维护一张当前能力表：入口、默认配置、开启条件、真实执行点、测试和能力边界」。
> 规则：只写当前代码的真实状态；改代码必须同步改这张表；历史报告不回头改数字。

## 一、调用前（执行与授权）

| 能力 | 入口 | 默认 | 开启条件 | 真实执行点 | 测试 | 边界 |
|---|---|---|---|---|---|---|
| 工具白名单 | `security.check_tool` | 开 | — | `runtime.Ctx.call` | test_core / test_permission_and_ci | 未登记 Agent 只告警不拦，避免新增 Agent 被全线挡住 |
| 参数级拦截 | `security.guard_tool_args` | 开 | mode≠off | `Ctx.call` | test_security | 只给检测建议，真正 raise 在 `Ctx.call` |
| 结构契约 | `tool_contract.authorize` | 开 | — | `execution.preflight` | test_tool_contract / test_execution_entry | 只覆盖登记过的工具 |
| 严格参数模型 | `tool_contract.validate_args` | 开 | — | 同上 | test_tool_contract | 未知字段拒绝；别名冲突不猜真值 |
| 按工具声明式护栏 | `tool_rails.check_call` | 开 | `AGENTLAB_SECURITY_RAILS=0` | `execution.preflight` | **test_tool_rails（22 项）** | 调用侧只给 shell.exec / http.send 挂检查，避免与契约重复 |
| 意图对齐 | `security.guard_tool_intent` | **关** | `AGENTLAB_SECURITY_INTENT=1` + 本地模型 | `execution.preflight` | test_execution_entry | 需要 user_goal；缺目标不给结论 |
| 审批后端 | `approval.ApprovalGate` | **关** | `Ctx(approval_gate=...)` | `Ctx.call` | test_approval / test_approval_hardening | 单机 SQLite；跨进程原子性未验证 |

## 二、返回侧与用户输入侧（检测）

| 能力 | 入口 | 默认 | 开启条件 | 真实执行点 | 测试 | 边界 |
|---|---|---|---|---|---|---|
| 不可见字符归一化 | `security.strip_invisible` | 开 | — | `security.scan` | test_llamafirewall_merge | tag/零宽/双向控制符；不处理同形字 |
| L1 正则（24 条） | `security.scan` | 开 | — | `scan` | test_security / test_guard_fuzz | 中文实测召回 0.029 |
| L2 字符 n-gram | `deep_scan` | **关** | `AGENTLAB_SECURITY_DEEP=1` | `guard_tool_output` | test_deep_scan | 未见业务域误报 33%，不默认开 |
| L3 语义判定 | `semantic_guard` | **关** | `AGENTLAB_SECURITY_SEMANTIC=1` | `guard_tool_output` | test_llamafirewall_merge 等 | 约 1 s/条；模型不可用 → review，不放行 |
| 动作-职责不符 | `action_scope.check_output_scope` | 开 | `AGENTLAB_SECURITY_SCOPE=0` | `guard_tool_output` | **test_action_scope（22 项）** | 未登记职责的工具只报 info |
| 声明式返回侧护栏 | `tool_rails.check_result` | 开 | `AGENTLAB_SECURITY_RAILS=0` | `guard_tool_output` | test_tool_rails | 检查器抛错 → `rails_checker_error`，不折算成没命中 |
| 内容包裹 | `security.wrap_untrusted` | **关** | `AGENTLAB_SECURITY_WRAP=1` + enforce | `guard_tool_output` | test_untrusted_input | 改了 text 才算生效 |
| 代码安全扫描 | `code_guard.scan_code` | 按需 | 显式调用 | tool_rails 的 code 扫描器 | test_llamafirewall_merge | 纯静态，**不是执行沙箱** |

## 三、审批与幂等

| 能力 | 入口 | 默认 | 开启条件 | 真实执行点 | 测试 | 边界 |
|---|---|---|---|---|---|---|
| 版本绑定 | `approval._version_mismatch` | 开 | 调用方传版本 | `require` / `consume` | test_approval_hardening | 缺值不当通配符 |
| 原子一次性消费 | `approval._consume_atomic` | 开 | — | 同上 | 同上（屏障并发用例） | 单机 SQLite 条件更新 |
| 幂等结算 | `SettlementLedger.settle` | 开 | 显式调用 | real_tasks | test_approval | 同键不同内容报冲突；≠ 业务成功 |
| 摘要语义化 | `approval.canonical` | 开 | — | 所有摘要 | test_approval | 金额定标到分；整数保精度；字符串不 strip |
| 受控执行配置 | `execution.build_config` | **legacy** | `AGENTLAB_EXEC_MODE=controlled` | `Ctx.__init__` | test_execution_entry | controlled 下写工具无审批后端 → 启动期报错 |

## 四、从 NeMo Guardrails 融合过来的方法

| NeMo 的做法 | 本仓库的等价物 | 差异说明 |
|---|---|---|
| `rails.tool_call/tool_result.per_tool` 按工具分发检查 | `tool_rails` 的 `{tool: {call: [...], result: [...]}}` | 一致。同一份配置管两个方向 |
| 配置缺项在加载期报错，点名 flow + tool | `tool_rails.load` 抛 `RailsConfigError` | 一致。未知扫描器/方向/工具/多余的键都报 |
| `@tool_call_validation` 未声明工具先挡 | `check_call` 返回 `not_declared`，**不跑任何检查** | 一致。有测试断言扫描器调用次数为 0 |
| `$argument=` 只送指定字段给判官 | `{"scanner": ..., "fields": [...]}`，仅 call 方向 | 一致。result 方向传 fields 会配置报错 |
| `parse_tool_safety_verdict` 只认 safe/unsafe，其它当坏输出 | `status` 分 `safe/unsafe/error/not_declared` | 一致。检查器抛错 → `error`，绝不折算成 safe |
| LLM 判官做调用前安全检查 | 用**结构契约 + 参数模型**替代 | **刻意不同**：结构检查更严更便宜；意图对齐作为可选补充 |
| 严格配置校验（模型未定义即失败） | `execution.build_config` 启动期校验 | 一致 |
| NeMo 的 role→scanner 流水线 | `scan_pipeline.py`（上一轮已融合） | 一致 |

## 五、评审报告的逐项状态

| 编号 | 要求 | 状态 | 证据 |
|---|---|---|---|
| R01 | 统一执行入口，deny 时工具不执行 | ✅ | test_execution_entry（调用次数=0） |
| R02 | 严格参数模型，别名冲突拒绝 | ✅ | test_tool_contract / test_edge_cases |
| R03 | 摘要按字段语义，保留精度 | ✅ | test_approval::test_canonical_no_false_equivalence |
| R04 | 候选版本/幂等键绑定消费 | ✅ | test_approval_hardening |
| R05 | 原子一次性消费 | ✅ | test_approval_hardening（并发屏障） |
| R06 | 关键字段完整送判；失败不当放行 | ✅ | test_execution_entry::TestIntentInputCoverage |
| R07 | 统一统计口径，披露测试集复用 | ✅ | experiments/action_scope_v2 |
| R08 | 中文语料形状修正，扩到 100+ | ✅ | test_chinese_corpus（16 项） |
| R09 | 指纹覆盖安全模块与开关 | ✅ | test_baseline_fingerprint（9 项） |
| §9 | 能力表（本文件） | ✅ | docs/capability-matrix.md |
| §10 会话7 | P2：API 受控并发与预算 | ❌ **未做** | 仍是单用户回环假设 |
| §6 R06 | 每任务模型调用次数/token/超时预算 | ❌ **未做** | 只有单次调用超时，没有任务级预算 |
| §4 | 任务/租户作用域纳入审批上下文 | ⚠️ **部分** | 有 run_id，没有 tenant/task 作用域 |
| §7 | 真实人工双标注 + 标注者间 kappa | ⚠️ **部分**：真人**单人**标注已完成（60 条 vs 语料），按入口分档注入档 kappa 0.752；第二位标注者的 60 条盲标包与 **200 条加长批**（含首轮 60 条）已生成并发布，匿名编号、不泄露答案；合并脚本支持 `--n 100` / `--map-a`，结果待回收，**仍不能写双人 kappa** | `experiments/human_labels_v1/result.json`、`scripts/make_round2_sheet.py`、`scripts/score_double_labels.py`、`tests/test_human_labels.py`、`tests/test_double_labels.py` |
| B 方向 | 语义注入预注册实验 E1–E4 | ⚠️ **已执行，2 通过 / 2 未通过**：E1 落点约束有实测证据（正常请求 100% 误报）、E4 证明「检测建议 ≠ 实际拦截」（audit 检测率 1.0 但 ASR 0.833）；E2 中文负例剂量不足、E3 结构过滤在 `z/√n` 归一化下反升误报，均未达发布线。**是对照实验结论，不是已发布能力** | `docs/semantic-injection-plan.md`、`experiments/semantic_injection_v1/report.md`、`tests/test_semantic_injection_plan.py` |
| — | 官方 AgentDojo 排行榜 | ❌ **未做** | 需官方模型与 API |

## 六、怎么复核

```bash
python -m unittest discover -s tests -q      # 784 项
evoguard config                              # 本次生效配置
python -c "import sys;sys.path.insert(0,'ai-service/evoguard');import tool_rails as t;[print(r) for r in t.describe(t.load())]"
```
