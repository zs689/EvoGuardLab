# 开发进度

## 2026-11-08：P0 01/02 技能验证指标与样本充分性

主项目为 `E:\agent\EvoGuardLab`，本轮依据最新推进计划的开发交接继续，没有修改 API、前端或技术栈。

### 已完成

- `skills.evaluate` 从成功总数净差改为逐 case/seed 配对负迁移，分母为 baseline 成功 guard；空分母保留 null。
- 保存四种 guard 转移计数、每侧 run_id、成本、输入、判定原因和 paired-v2 协议；旧数据库通过增列迁移，历史记录保留 NULL 协议标记。
- 独立任务不足三项转复核，重复 seed 不充当独立任务；提前拒绝重复 ID/recipe 和训练验证任务/family 重叠。
- 两个训练入口使用 dev 内的 train/val 划分，修复 mock 入口用 test 验证技能的问题。新增隔离输出目录，拒绝覆盖已有目录。
- 通过 baseline_manifest.json 固定 31 个已有报告/数据文件的哈希，未修改已有实验或数据库。

### 验证证据

输出目录：`experiments/p0_validation_20261108/`。

- 修改前原有 109 项测试通过。
- 新增的前 14 项反例在旧实现上全部未通过（9 个断言失败、5 个缺少新接口/字段的错误）；“互换成功题”“空 guard”“单验证任务”均可复现错误决策。
- 修复后前 14 项验证测试通过，连同旧测试共 123 项通过；最终验证专项扩为 18 项并全部通过（原有 109 项及新增 18 项均已覆盖）。
- train_smoke：mock 后端，6 个候选均为 NEEDS_REVIEW，严格库 0 条，放宽库 6 条。
- mock_smoke：49 条训练用例、39 个候选、0 个晋升；单故障 A/B/C/D 为 0/26.5/100/94.1%，复合 D 为 0%。最终格式复核另存 mock_final，不覆盖此前冒烟。
- 最终证据检查通过：214 对结果与 SQLite 原始 run 的成功状态、seed、run_id 逐项一致；JSON 和数据库的评估记录一致，train/val/test 的任务 ID 无交集，两个入口都拒绝覆盖已有输出目录。
- 空 guard 冒烟返回 NEEDS_REVIEW，JSON 中负迁移为 null，报告和终端显示 N/A。31 个历史文件的哈希复核全部一致。详见 `experiments/p0_validation_20261108/verification.json`。

这些是机制实验，不是真实模型能力验证。已有验证数据通常每类仅一个任务，触发复核是预期行为；不能为保持历史成功率降低门槛。严格技能库为空后，复合故障无法再靠已晋升技能恢复，该负结果应保留。

### 仍未完成

- 尚未补足按业务 family 独立划分的验证集；三任务下限不是统计安全保证。
- promote 当时仍未校验评估/候选指纹（**已在 P0 03 修复**）；rollback 仍未恢复完整内容，经验引用尚未绑定来源。
- 旧 real_model_bench 和看板仍读取历史路径；本轮不把新旧报告合并。完整协议冻结、真实模型重测属于后续工作。

### 当时的下一项验收（P0 03，已完成，见下节）

P0 03：给 promote 增加有效评估记录、协议、候选内容指纹校验及幂等晋升，覆盖未评估、评估后候选被改动、重复晋升等反例。随后处理完整行为回滚。见 validation-protocol.md 的口径和边界。

## 2026-11-08：P0 03 晋升校验与幂等

依据推进计划第 4 章「任务 P0 03 晋升校验」与第 3.3 节，没有修改 API、前端或技术栈。

### 已完成

- `skills.promote` 从「直接改状态」改为门禁：核对 skill_id、评估协议、候选正文指纹、验证集指纹、24 小时有效期、样本充分性、判决、旧版本状态与评估消费情况；任一不符抛 `PromotionRejected`（带诊断码与逐项核对说明）。
- `evaluate` 记录 `candidate_hash`、`validation_hash`、`validation_cases`、`promotion_protocol`；内存候选与 DB 行先把落库 JSON 字符串归一成结构再哈希，保证两侧算出同一个值。
- 门禁不信任汇总列：独立任务数、负迁移率、增益从 `pairs` 重算后与记录比对。手工翻 verdict、只改某一对的成功状态、或替换验证题都会被拒。
- 事务内追加不可变版本快照、更新 `active_version` / `promoted_from_evaluation`、标记评估已消费；重复请求幂等返回已启用版本，不继续升版本。
- 接通两个正式入口：`evolution_bench.py` 显式传 `evaluation_id`；`real_model_train.py` 严格库改为 `build_library(strict=True)` 先晋升再导出。此前 `build_library` 直接 `UPDATE skills SET status='active', version=2` 伪造「验证通过」，是门禁的真实绕过点。
- 放宽库（C）保留为故意不验证的对照组，不再改写源库证据，训练控制台显式区分两者。
- `audit_promotion()` 只读体检与 `promotion_failure_text()` 可读失败说明，供报告与 CLI 复用。

### 验证证据

- 全量 **145 个测试**通过（24 核心 + 9 评测口径 + 19 难度组 + 34 检查器 + 23 静态 + 36 技能验证）。
- `tests/test_core.py` 三处原本绕过门禁的裸晋升，改为先构造满足 paired-v2 门槛的独立评估；被测行为不变，且**没有**给 `promote` 留任何 `allow_legacy` 后门。
- 新增 18 个 P0 03 反例：无评估被拒、正文 / scope 改动失效、空 guard 与单验证任务翻 verdict 仍被拒、过期评估、历史 NULL 协议、验证集 / 成对明细篡改、跨技能评估、重复晋升幂等、被拒后不留下写库痕迹、严格库必须由真实晋升产生、C 库不污染源库。
- 隔离冒烟：`experiments/p0_03_20261108/train_smoke/`，mock 后端 6 个候选全部 `NEEDS_REVIEW`，严格库 0 条、放宽库 6 条；未覆盖历史产物。
- `docs/validation-protocol.md` 增加 P0 03 门禁章节；README 测试数更新为 145，并按计划 3.6 把单故障 D 的 `98.5%` 修正为 `97.1%`（与表格、报告一致）。

### 仍未完成

- `rollback` 仍只改 status/version，未恢复 strategy / scope / required_tools / safety（P0 04）。
- 经验证据溯源、环境元数据与 `fault_scope` 持久化（P0 05）。
- 冻结基线 manifest、报告统一、旧评测入口的显式技能库选择（P0 06）。
- 24 小时有效期与 3 个独立任务只是工程下限；仍缺按业务 family 划分的完整验证集与置信区间。

### 当时的下一项验收（P0 04，已完成，见下节）

P0 04 完整回滚：v1 是单步策略，v2 是两步策略且 scope 不同。启用 v2 后回滚 v1，检查实际选中的策略、执行轨迹与审批约束均等于 v1；仅检查版本号变化不算通过。

## 2026-11-08：P0 04 完整回滚

依据推进计划第 5 章「任务 P0 04 完整回滚」与第 3.4 节。

### 已完成

- 技能正文采用只追加的不可变快照；`active_version` 指向当前执行版本，`active_snapshot()` 供审计定位。`save` 写入规范化快照，`promote` 在事务内追加新版本快照。
- `rollback(to_version=...)` 从目标快照整份还原 name / task_type / fault_type / strategy / scope / required_tools / safety / decision_params，并把 version 与 active_version 一起指回目标；状态恢复 active，追加 `skill_rolled_back` 事件，不删除任何快照或评估。
- 新增 `disable()`：只撤销启用（disabled + 指针清空），不改正文；与「恢复旧版内容」严格区分。旧测试 `test_rollback` 相应升级为同时验证两种操作。
- 新增 `revise()`：改正文后标记未启用草稿、清空 active_version，正文指纹失配使旧评估失效，必须重新验证才能晋升。
- 导出链路补齐正文列：`real_model_train.build_library` 与历史入口 `real_model_bench` 复制技能时不再丢掉 scope / active_version / decision_params。
- `CONTENT_FIELDS` 增加 `decision_params`；P0 03 报告中的 content_fields 是当时 7 字段版本，作为冻结历史证据不回填。

### 验证证据

- 全量 **149 个测试**通过（28 核心 + 9 评测口径 + 19 难度组 + 34 检查器 + 23 静态 + 36 技能验证）。
- 新增 `TestFullRollback` 4 项：v2 单步 / single / 严格审批 → v3 两步 / multi / 宽松审批 → 回滚 v2，逐项核对 `lookup` 选中的策略链与正文、`plan_recovery` 的 strategy_chain / skill_version、真实 `run_once` 落在 `recoveries` 的 strategy / chain / skill_version、审批约束与 decision_params、不可变快照指针、历史与审计事件保留；另有 revise 作废评估、默认回滚到上一启用版本、未知版本被拒。
- 隔离冒烟 `experiments/p0_04_20261108/train_smoke/`：mock 后端 6 个候选全部 NEEDS_REVIEW，严格库 0 条、放宽库 6 条，导出链路无报错。
- `docs/validation-protocol.md` 增加 P0 04 章节与已知边界；README 测试数更新为 149。

### 仍未完成

- 运行时目前只消费 strategy / scope / version，**尚未读取 safety 的审批声明执行审批**；本轮只证明技能层审批约束随正文还原。
- 经验证据溯源、环境元数据与 `fault_scope` 持久化（P0 05）。
- 冻结基线 manifest、报告统一、旧评测入口的显式技能库选择（P0 06）。
- 计划文档三个分叉点（第 3/15/17 章指针、P1 第 9 章导出状态、P2 验收链）仍待在 `docs/` 落地。

### 下一项验收

P0 05 经验证据溯源与环境元数据：`validate` 拒绝跨运行伪造引用，`extract` 从实际运行复制 provider / model_id / 指纹 / 数据集版本 / tool_backend / 实验协议，`fault_scope` 持久化。
