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

## 后续边界

本轮完成 P0 01/02/03，晋升链路在 `promote` 与严格库构建两处均不可绕过。但 `rollback` 仍只改 status/version，尚未恢复技能正文（P0 04）；经验引用溯源、环境元数据（P0 05）与冻结基线、报告统一（P0 06）仍未完成。24 小时有效期与 3 个独立任务只是工程下限，不是统计安全保证。不得把本轮结果描述为「回滚可恢复实际行为」或「实验已可完整复现」。
