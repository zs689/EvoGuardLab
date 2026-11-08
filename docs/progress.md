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

### 当时的下一项验收（P0 05，已完成，见下节）

P0 05 经验证据溯源与环境元数据：`validate` 拒绝跨运行伪造引用，`extract` 从实际运行复制 provider / model_id / 指纹 / 数据集版本 / tool_backend / 实验协议，`fault_scope` 持久化。

## 2026-11-08：P0 05 经验证据溯源与环境元数据

依据推进计划第 5 章「任务 P0 05 经验证据与来源」。

### 已完成

- `validate(exp, store=None)` 分两层：原结构条件 + 数据库级溯源（source_run_id 存在、每条 ref 是真实步骤且属于该 run、诊断坏步被引用、成功恢复记录与 recovery_action 可关联、replay 步被引用）。**不传 store 最高 PENDING**，`save` 一律带库校验。
- rejected / pending 经验仍落库保留作分析资料，`retrieve` 默认与 `include_pending=True` 都不返回 rejected。
- `extract` 不再把 environment 写死 `"mock"`：从 model 步骤复制 provider / model_id，算模型与 prompt 指纹，从实际加载的 sim 数据算 dataset_version，读 tool_backend 与实验协议，按 provider + tool_backend 分类为 `mock_simulated_tools` / `local_model_simulated_tools` / `real_toolchain`。
- 修掉实测 bug：当前进程 gateway 默认 provider 常为 mock，最初会覆盖轨迹里记录的真实 provider；改为轨迹优先，进程配置只在一致时补 base / configured_model。
- `fault_scope` 与 8 个环境字段落库，`env_meta` 保存完整字典与来源说明；`retrieve` 读回后 `fault_scope` 能一路传到 `skills.propose` 的作用域决策。老库增列保持 NULL，不冒充新协议。

### 验证证据

- 全量 **162 个测试**通过（41 核心 + 9 评测口径 + 19 难度组 + 34 检查器 + 23 静态 + 36 技能验证）。
- 新增 `TestExperienceProvenance` 12 项反例；`TestExperience` 升级为真实轨迹构造（假 step_id 不再能拿到 validated），并新增「无 store 不得 validated」「失败经验保留但不可检索」。
- 真实训练入口 `experiments/p0_05_20261108/train_smoke/`：6 条经验全部 validated，环境分类 `mock_simulated_tools`，provider/model_id/各类指纹/tool_backend/fault_scope 均非空；`env_meta` 含 `prompt_source=system_prompt+steps`。
- mock 闭环 `experiments/p0_05_20261108/evolution_smoke/`：49 用例提炼 **39 条经验全部 validated**，四组对照 0 / 26.5 / 100 / 94.1%、复合两步链 100%，与 P0 01/02 的历史基线一致 —— 溯源没有误拒、没有改变既有结论。
- `docs/validation-protocol.md` 增加 P0 05 章节与已知边界。

### 仍未完成

- `tool_backend=real` 只是显式声明，工具链仍全部是模拟实现；模型指纹不是权重哈希。
- 冻结基线 manifest、报告统一、旧评测入口的显式技能库选择（P0 06）。
- 计划文档三个分叉点（第 3/15/17 章指针、P1 第 9 章导出状态、P2 验收链）仍待在 `docs/` 落地。

### 下一项验收

P0 06 冻结可复现基线：把报告 / metrics.json / runs.jsonl / C 与 D 技能库哈希及关键代码指纹写进同一 manifest，重跑用新目录不覆盖历史，按 case_id / seed / mode / 配置哈希 / 技能库哈希恢复且指纹不一致拒绝混跑；报告自动生成同一组数字。

## 2026-11-08：P0 06 冻结可复现基线与数字统一

依据推进计划第 4 章「任务 P0 06」与第 3.6 点的报告分叉。

### 已完成

- 新增 `ai-service/evoguard/baseline.py`（协议 `evoguard-baseline-v1`）：`freeze / verify / check` 三个动作 + `run_key / assert_no_mixing / check_resume / check_denominators / summarize / render_mode_section`。
- manifest 一次登记：运行目录里 metrics.json / runs.jsonl / report.md / *.db / 其它 json 的哈希，11 个关键代码文件哈希，配置（含 paired-v2 与晋升门禁门槛、检查器配置）及其哈希，C/D 技能库**内容**哈希与条数，git HEAD，最后是覆盖前三者的总指纹。
- **技能库指纹按内容算，不哈希 SQLite 字节**：同一库关掉重开后页与 journal 会变，字节哈希会飘；改为按 id 排序哈希正文（含 status / version / active_version，因为「候选」和「已晋升」是两种实验条件）。回归测试 `test_library_hash_is_stable_across_reopen` 守住这条。
- **产物篡改与代码漂移分开报**：产物哈希变了 → `ok=False`（证据被改）；代码在冻结之后改了 → 只进 `code_drift`，并明说「这份报告属于历史版本，不能声称由当前代码产生」。混在一起报，用户分不清是数据坏了还是代码往前走了一步。
- **防混跑接进真实入口**：`real_model_bench` 每条 run 记录写入 protocol / config_hash / library_hash；续跑前用 `check_resume` 核对，别的配置或技能库直接拒绝（退出码 3），旧版无指纹记录默认也拒绝，`--allow-legacy-resume` 才放行并在 metrics 标 `mixed_baseline`。
- **冻结时对账**：传入运行前算好的技能库指纹时，`freeze` 会重算一遍并比对；运行期间库内容变了（中途晋升技能）直接抛 `BaselineMismatch`，不允许把「跑前快照」当成「跑后事实」。
- **报告数字单一来源**：`evolution_bench` 与 `real_model_bench` 都在写完后追加「模式实际使用的策略与技能」一节，内容全部由 metrics 生成（哪个库、多少条、协议、配置指纹、分母自洽结果），不再手抄第二套数字。
- 重跑守卫：输出目录已存在直接 `FileExistsError`；manifest 已存在默认拒绝覆盖（`test_freeze_refuses_to_overwrite_history`）。

### 验证证据

- 全量 **176 个测试**通过（55 核心 + 9 评测口径 + 19 难度组 + 34 检查器 + 23 静态 + 36 技能验证）。
- 新增 `TestBaseline` 14 项：冻结/校验往返、改报告被发现、代码改动归类为漂移、技能库内容变化被发现、指纹跨重开稳定、拒绝覆盖历史、恢复键随五个维度变化、混跑拒绝外来与未标记记录、`check_resume` 三分支、four_groups 的 rates 与 counts 对账、冻结前库已变的拒绝。
- `experiments/p0_06_20261108/evolution_smoke/`：mock 闭环重跑，**产物差异 0 处、代码漂移 0 个、分母自洽通过**；四组数字仍为 0 / 26.5 / 100 / 94.1%，39 条经验全部 validated。
- `experiments/p0_06_20261108/baseline_manifest.json`：冻结历史 `experiments/real_model/` 产物，现在校验结果为「产物差异 0 处 + 代码漂移 3 个文件」，即历史报告未被改动、但由更早的代码产出 —— 这正是该有的结论。
- README 残留的 `单故障 D 98.5%` 已改回 `97.1%`（与表格、报告一致）。

### 仍未完成

- 真实模型报告（`experiments/real_model/`）是在本模块之前跑的：结构里没有 `config_hash` / `library_hash`，只能以「历史基线 + 代码漂移」的形式登记，不能与新的冻结产物混算。
- 运行时仍不消费 `safety` 审批声明；工具链仍全部为模拟实现。
- 计划文档三个分叉点（第 3/15/17 章指针、P1 第 9 章导出状态、P2 验收链）仍待在 `docs/` 落地。

### 下一项验收

计划文档分叉点收口：把第 3/15/17 章的交接指针从 P0 01/02 挪到 P0 03 之后再挪到 P0 06 之后、把第 9 章已交付的 CLI/JSON/Markdown 导出标为已完成、补 P2 里「历史评测入口技能库怎么选」的断点。随后再评估是否值得重跑一次真实模型全量评测。

## 2026-11-08：计划文档分叉点收口

依据推进计划第 3 / 9 / 15 / 17 章与第 3.6 点。Word 原件未改，结论落在 `docs/plan-alignment.md`。

### 已完成

- 逐条对照计划原文与当前代码，标出两处**已经过时**的表述：第 3/15/17 章仍把 P0 01/02 列为待办（实际已推进到 P0 06）；第 9 章把「单命令 CLI + JSON/Markdown 导出」写成未来工作（实际已交付，含导出文件名 `inspect.json` / `inspect.md` / `static.json` / `static.md`）。
- 补出第 9 章**真正没做**的三块：阈值退出码（现在只有 0 / 2，没有 `--fail-on`）、≥30 条人工标注轨迹与准确率、规则适用性统计（零命中但多数规则未执行时必须写「覆盖有限」）。
- P2 断点收口：`real_model_bench.py --library-dir` 显式选择评测用哪一份 C/D 库，训练产物目录可直接接评测入口；库哈希进 runs.jsonl 与 metrics，结束冻结 manifest，续跑用 `check_resume` 防混跑。
- 如实记录仍未做的：`experiments/real_model/` 的历史报告产生在指纹机制之前，只能登记为「历史基线 + 代码漂移」；P2 让技能表达条件决策没有任何进展。

### 验证证据

- 全量 **176 个测试**通过；`ai-service/real_model_bench.py` 可编译，`--library-dir` 指向不存在目录时直接报错。
- 实测 CLI 静态分支：`cd ai-service && python -m evoguard_inspect.cli --agent ../tests/fixtures/mini_repo --split-dir --out ...` 输出 `static.md` + `static.json`，20/20 条静态规则生效。

### 下一项验收

剩余真实缺口，按价值排序：
1. 重跑一次真实模型全量评测（约 936 run），让 `experiments/real_model/` 拥有可混算的指纹，而不是只登记历史基线；
2. 检查器阈值退出码 + 规则适用性统计（第 9 章硬要求，缺它无法进 CI）；
3. ≥30 条人工标注轨迹集，才有资格谈误报率与漏报率。

