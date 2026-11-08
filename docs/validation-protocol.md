# 技能验证协议 paired-v2

2026-11-08 起，`skills.evaluate` 使用本协议。旧实验及旧数据库记录保留原样；历史记录新增的 `protocol_version` 与 `details` 为 NULL，不能冒充新版验证。

## 成对指标

每个 target 和 guard case 都以相同输入、故障 recipe、seed 分别运行 baseline 和 candidate。两侧接收独立输入副本。保存 case_id、输入、两侧成功状态、run_id 和可提供的成本字段。

| 指标 | 定义 | 零分母 |
|---|---|---|
| target 增益 pp | (candidate 成功数 − baseline 成功数) / target 数 × 100 | null |
| 负迁移率 | guard 中前成功后失败数 / guard 中前成功数 | null |
| 全 guard 退化比例 | guard 中前成功后失败数 / guard 总数 | null |

同时保存 guard 的改善、退化、双成功、双失败计数。成功总数相同也可能有退化，不能用净差替代逐题配对。机器数据中上述比率用 0–1，gain_pp 用百分点。mock 闭环的 four_groups 展示指标沿用百分数 0–100，显式保留分子分母。

阈值使用未舍入值：增益至少 15pp，负迁移至多 5%。协议变化须更换版本号，不直接合并新旧结果。

## 充分性和独立性

- target、guard 以及 baseline 成功的 guard，各至少覆盖 3 个不同 task/family。少于此数转 NEEDS_REVIEW，不自动晋升；已经观察到超过门槛的退化仍优先 REJECT。
- family_id 优先用于独立样本计数，缺少时退回 (agent_type, task_id)。更换 seed、故障类型或 case_id 不能增加独立任务数。
- case_id 在本次 target 与 guard 的并集中必须唯一；相同任务、故障 recipe、故障位置与 seed，即使换 ID 也拒绝。
- 提供 training_cases 时，同时拒绝训练任务或训练 family 与验证集重叠。mock 与模型训练两个入口都传入完整训练划分；单独调用 evaluate 未传入时会记录 training_overlap_checked=false。
- target 和 guard 可以使用同一个验证任务的不同故障。最终 test 不参与候选生成或技能验证。

3 个任务只是工程证据下限，不能证明真实退化率低于 5%。零观察退化不等于零风险；本轮未实现置信区间或正式统计安全门槛。当前数据仍缺乏完整 family 标注，task_id 不相交不能充分证明语义独立。

## 证据和兼容性

`skill_evaluations` 新增 protocol_version、details 两列；details 保存整个结果，包括配对、阈值和判定理由。原列名继续保留。回调可以返回 bool、(bool, run_id)，或 `{success, run_id, cost}`。无法提供的 run_id/cost 为 null；不把结果字典本身的 truthiness 当成功。

训练入口保存 `validation_results.json`，mock 入口在 `metrics.json` 的 evaluations 中保存同一批结果。两者均在运行目录中保留对应 SQLite 轨迹。cost 中 reported_tokens 是现有运行器报告值；mock 计数不能解释为真实模型 token 使用量。

## 隔离运行

在项目根目录执行：

```powershell
python -m unittest tests.test_skill_validation -q
python ai-service/real_model_train.py --backend mock --limit 6
python ai-service/evolution_bench.py
```

两个入口默认在 `experiments/validation_runs/` 下创建新目录。可用 `--output-dir` 指定一个尚不存在的目录；目录已存在则拒绝运行，避免覆盖证据。历史默认 data 库和 experiments 报告不更新，因此旧看板仍展示旧基线。

`real_model_bench.py` 的历史库路径尚未迁移；新训练产物不会自动接到旧评测入口。需要后续完成显式库选择和实验 manifest，不能把旧评测结果当成本轮新结果。

## P0 03 晋升门禁（promotion-gate-v1）

`skills.promote` 不再接受裸调用。它在同一个事务内完成：核对评估 → 追加不可变版本快照 → 更新当前指针 → 标记评估已消费；任一检查失败则整体回滚，不留下半套状态。

### 评估与候选的绑定

`evaluate` 写库时同时记录：

- `candidate_hash`：技能正文指纹，字段为 name / task_type / fault_type / strategy / required_tools / safety / scope。status、version、时间戳刻意排除，否则幂等重放会立刻失效。内存 dict 与 DB 行先把落库的 JSON 字符串还原成结构再哈希，保证两者算出同一个值。
- `validation_hash` 与 `validation_cases`：target / guard / training 的规范化题目指纹。
- `protocol_version=paired-v2` 与 `promotion_protocol=promotion-gate-v1`。
- 完整 `pairs` 成对明细、阈值与判定理由。

### 晋升时逐项核对

skill_id 归属、评估协议、paired-v2 明细存在、判决 PROMOTE、评估有效期（24 小时）、旧版本状态非 rejected、当前版本已有不可变快照、评估未被其他版本消费、候选正文指纹、验证集指纹、成对明细自洽、样本充分性与门槛。

独立任务数、负迁移率、增益都从 `pairs` **重算**再与记录比对，不是只读汇总列：改某一对的成功状态、把 SQL 的 verdict 列手工翻成 PROMOTE、或替换验证集题目，都会被拦下。每项失败有独立诊断码；`promotion_failure_text()` 输出可直接贴进报告的逐项核对与处理建议，`audit_promotion()` 是同一套逻辑的只读版本，不写库。

### 幂等

同一评估 + `consumed_by == 当前 version` + 状态 active → 判定为重放请求，直接返回已启用版本，不新增 `skill_versions` 行。评估过期后的重放也仍返回该版本；一次新的评估可以继续让技能升版本。

### 正式入口

- `evolution_bench.py`：`evaluate` 后显式传 `evaluation_id` 调 `promote`。
- `real_model_train.py`：严格库（D）通过 `build_library(strict=True)` 先调 `promote` 再导出。此前这里直接 `UPDATE skills SET status='active', version=2` 伪造「验证通过」，是门禁的真实绕过点，已修。
- 放宽库（C）是**故意不验证的对照组**，仍以 active/v2 写入且不改写源库证据；它不是晋升链路生效的证据。
- 老库通过增列迁移；历史评估记录的 `protocol_version` / `details` 为 NULL，一律不得冒充新版验证。

### 反例

`tests/test_skill_validation.py` 的 `TestPromotionGate` / `TestStrictLibraryPipeline` 覆盖：无评估直接晋升被拒、候选正文或 scope 改动后旧评估失效、空 guard / 单验证任务即使手工翻 verdict 也不放行、过期评估、历史 NULL 协议、验证集或成对明细被篡改、评估属于别的技能、重复晋升不重复写版本、被拒后不留下任何写库痕迹、严格库必须由真实晋升产生。

## P0 04 完整回滚（不可变快照与 active_version）

`skills` 表是「当前工作副本」，`skill_versions` 是只追加、不修改的不可变快照。`active_version` 指向当前正在执行的那一份；`active_snapshot()` 让审计能回答「现在到底跑的是哪份正文」。

### 三种操作的区别

| 操作 | 正文 | status | active_version |
|---|---|---|---|
| `promote` | 固化当前正文为新版本 | active | 新版本号 |
| `rollback(to_version=...)` | **从目标快照整份还原** | active | 目标版本 |
| `disable` | 不改 | disabled | NULL |

`rollback` 还原 name / task_type / fault_type / strategy / scope / required_tools / safety / decision_params，并把 version 与 active_version 一起指回目标快照；追加 `skill_rolled_back` 事件，不删除任何快照或评估。`disable` 只撤销启用，绝不顺手改正文。

旧实现只读了快照的 version，却只 UPDATE status / version —— 版本号退回去了、实际执行的行为没变，这正是计划 3.4 指出的问题。

### 正文变更与重新验证

`revise()` 改正文后把当前行标为未启用草稿（candidate、active_version=NULL），不写快照、不自动启用。正文一变，`candidate_hash` 立即失配，旧评估不能再放行；必须重新跑 paired-v2 才能晋升，由 `promote` 把新正文固化成新的不可变版本。

### 验收

`tests/test_core.py::TestFullRollback` 用一组会改变行为的版本差异：v2 是单步 / single / 严格审批，v3 是两步 / multi / 宽松审批，且 required_tools、decision_params 都不同。启用 v3 后回滚 v2，检查：

- `lookup` 实际选中的策略链、scope、required_tools、safety、decision_params 全部等于 v2；
- `plan_recovery` 的 strategy_chain 与 skill_version，以及真实 `run_once` 落在 `recoveries` 里的 strategy / chain / skill_version；
- `active_snapshot()` 指向 v2 快照，旧快照、两次评估与 rollback 事件都还在。

已知边界：运行时目前只消费 strategy / scope / version，**尚未读取 `safety` 的审批声明来执行审批**。本轮验收的是「技能层声明的审批约束随正文一起还原」，不是「审批执行层已接通」。

### 导出链路

`real_model_train.build_library` 与历史入口 `real_model_bench` 复制技能时，现在一并带上 scope / active_version / decision_params。此前只复制 10 列，导出库里的技能无法按版本还原行为。

### 指纹演进

`CONTENT_FIELDS` 自 P0 04 起增加 `decision_params`。P0 03 报告 `promotion_gate_report.json` 里的 content_fields 是当时的 7 字段版本，属于冻结历史证据，不回填；其哈希不能与 P0 04 之后的评估横向比较。

## P0 05 经验证据溯源与环境元数据

### 两层准入校验

`validate(exp, store=None)`：

- **结构层**：evidence_refs 非空、根因已知、恢复成功、置信度 ≥ 0.5。任一不过直接 REJECTED。
- **溯源层**（传 store 时）：`source_run_id` 存在；每条 ref 指向真实的 `steps` 行且该行 `run_id` 等于 source_run_id；诊断定位的坏步被 refs 引用；存在成功恢复记录且其策略链与 `recovery_action` 可关联；恢复产生的 replay 步被引用。

**不传 store 最高只能到 PENDING** —— 没核实来源就不能发「可信执行指导」资格。`save(store, exp)` 一律走带库校验。rejected / pending 照样落库保留作分析资料，但 `retrieve` 默认与 `include_pending=True` 都不会返回 rejected。

以前 evidence_refs 只要「非空」就放行：填个假 ID、抄别的运行的 step_id、或把恢复动作换成一个从没执行过的策略，都能拿到 validated。

### 环境元数据从真实运行复制

`extract` 不再把 `environment` 写死 `"mock"`：

| 字段 | 来源 |
|---|---|
| `provider` / `model_id` | model 步骤的 `input.provider` 与 `steps.model_id` |
| `model_fingerprint` | provider + model_id + 配置模型 + base 的 SHA-256 |
| `prompt_fingerprint` | 系统 prompt + 本次轨迹里的 user prompt |
| `dataset_version` | 从实际加载的 `sim` 任务/故障表算出的稳定指纹 |
| `tool_backend` | `AGENTLAB_TOOL_BACKEND`，缺省 `simulated` |
| `experiment_protocol` | run.mode / seed / validation_protocol / promotion_protocol |
| `fault_scope` | 本次运行诊断出的坏点数 |

`environment` 由轨迹 provider 与 tool_backend 共同分类：`mock_simulated_tools`、`local_model_simulated_tools`、`real_toolchain`。

实现中修掉一个真问题：当前进程 `gateway.get_provider()` 的默认常常是 mock，最初会**覆盖轨迹里记录的真实 provider**，把真实模型跑出来的经验误标成 mock。现在轨迹优先，进程配置只在 provider 一致时用于补 base / configured_model。

`fault_scope` 落库并经 `retrieve` 读回，一路传到 `skills.propose` 决定技能作用域。老库通过增列迁移，9 个新列保持 NULL —— 来源不明就是来源不明，不能补一个 `mock` 让老数据看起来像新协议核实过。`env_meta` 保存完整字典含来源说明。

### 已知边界

- 模型指纹是**可观测标识组合**，不是模型权重哈希；不能用来断言两个模型权重相同。
- `tool_backend=real` 目前只是显式声明，本代码库的工具仍全部是 `sim` 的模拟实现；真实工具链尚未接通。
- 系统 prompt 通过 `sys.modules` 软引用读取（避免 experience ↔ runtime 循环导入），未加载时 prompt 指纹只覆盖轨迹里的 user prompt，并在 `prompt_source` 标注。

### 反例

`tests/test_core.py::TestExperienceProvenance` 覆盖：伪造 step_id、跨运行引用、诊断坏步未被引用、恢复动作与成功恢复记录无法关联、删除成功恢复记录、环境字段不硬编码、真实工具链分类、模型指纹随模型变化、fault_scope 与 env_meta 读回、老库新列为 NULL。`TestExperience` 另验证无 store 不得 validated、失败经验保留但不可检索。

## 后续边界

本轮完成 P0 01/02/03/04/05：验证指标、样本充分性、晋升门禁、完整回滚、经验溯源与环境元数据均已落地。冻结基线 manifest、报告统一、旧评测入口的显式技能库选择（P0 06）与计划文档三个分叉点仍未完成。24 小时有效期与 3 个独立任务只是工程下限；运行时尚未消费 safety 审批声明；工具链仍为模拟实现。不得把本轮结果描述为「实验已可完整复现」或「审批执行层已验证」。
