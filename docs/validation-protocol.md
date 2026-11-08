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

## 后续边界

本轮只完成 P0 01/02。`promote` 自身仍可被直接调用，尚未绑定本协议、评估记录与候选内容指纹；回滚、经验引用溯源和统一实验 manifest 也尚未完成。不得把本轮结果描述为晋升链路已经不可绕过。
