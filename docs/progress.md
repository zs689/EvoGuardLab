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

## 2026-11-08：检查器阈值退出码与规则适用性统计

依据推进计划第 9 章「使用流程与发布条件」。这一节之前被写成未来工作，实际只做了一半 —— 现在把缺的那半补上。

### 已完成

- **退出码三态**：0 = 跑完且未达阈值；2 = 输入解析失败；3 = 达到 `--fail-on error|warn|info` 阈值。之前只有 0 和 2，CI 里没法用它卡质量。
- **`--strict-input`**：默认可解析的部分照常出结论、只在终端提示失败数；加了该参数则任一份输入解析失败就返回 2。
- **`inspect_files_safe`**：逐文件加载，坏输入不再让整批白跑；空文件与格式不兼容都算「解析失败」并计数，不静默当零命中。
- **`coverage.py` 规则适用性统计**：可选规则数、选中数、可执行数、跳过数与原因；数据集层面统计有多少条轨迹提供了答案 / 工具输出 / ≥2 种工具 / token 用量 / 时间戳；报告写入 Markdown 与 JSON，JSON 增加顶层 `verdict`。
- **判定口径**：全部规则可执行且零命中 = `通过`；零命中但有规则因输入不足没跑 = `覆盖有限`（明确写「不能读成检查通过」）；有命中 = `有发现`；无轨迹 = `没有可检查的轨迹`。
- **条件表自洽保护**：规则命中了却被条件表判为不可执行时，以实际行为为准并输出 `table_warnings` —— 表写错了要暴露，不能悄悄把命中算成跳过。
- **修掉一条真误报**：`dependency_mismatch` 只在上游工具输出里找参数来源，把「用户问题里的词直接当搜索词」判成了编造参数。现在合法来源扩展为工具输出 + 用户问题 + 模型输入，`trace_retry_ok.json` 从 1 条 warn 降为 0。
- **静态侧覆盖率对齐**：`analyze_static` 用 `confidence`、prompt、工具、写操作工具、配置重试五项条件判断 20 条静态规则能不能跑；源码启发式提取（`confidence=low`）上缺失型规则一律跳过并写明原因 —— 「源码里没读到」不等于「运行时没配」。`static.md` 增加「规则适用性」一节，`static.json` 增加 `coverage` 与顶层 `verdict`。
- **修掉一处静默丢输入**：`--trace a --trace b` 与 `--agent a --agent b` 之前因 `nargs="*"` 重复覆盖，只处理最后一个，其余文件被无声丢弃。改成 append 语义后两种写法都保留全部输入，并加回归测试守住。

### 验证证据

- 全量 **188 个测试**通过（55 核心 + 9 评测口径 + 19 难度组 + 42 检查器 + 27 静态 + 36 技能验证）。
- 新增 `TestCoverageAndExitCodes` 8 项与 `TestStaticCoverage` 4 项：条件表键必须是已注册规则（两侧）、零命中但有跳过不得报通过、补全输入后判定通过、命中强制可执行并报条件表偏差、空输入判定、部分解析失败不静默、CLI 五种退出码组合、重复参数不丢输入、低置信度定义不得判通过、静态报告带 `coverage` 与 `verdict`。
- 实测：`--fail-on error` 对合成坏轨迹返回 3；坏路径返回 2；好轨迹返回 0；`--agent bad --agent good` 分析 2 个定义并报出 16 个问题（之前只分析 1 个）。
- `experiments/p0_07_20261108/inspect_demo/`：17 条合成轨迹、25 条规则中 24 条可执行（1 条因配置没声明 `expect_keys` 跳过），报告含「规则适用性」一节与部分覆盖提示（例如 `token_blowup` 只在 1/17 条轨迹上可查）。
- `experiments/p0_07_20261108/static_demo/`：坏定义 + 好定义一起扫，20/20 条静态规则可执行，16 个发现；低置信度仓库扫描（`mini_repo`）只 4 条可执行、跳过 16 条并逐条写明原因。

### 仍未完成

- 人工标注集：仍是合成夹具 + 9 框架静态扫描，**没有人工标签**，所以只有覆盖率，没有准确率 / 误报率 / 漏报率。
- 规则适用性统计只覆盖候选集与实际行为，不覆盖「规则本身该不该存在」—— 需要外部标注才能判断。



## 2026-11-09：门禁后真实模型全量重跑（1080 run）

### 做了什么

- 用真实模型（Qwen3 4B / Ollama）在隔离目录重跑训练与评测，验证 P0 01–06 在真实链路里是否成立：
  - 训练：`experiments/real_model_20261108/train/`，dev 49 例、404 run、449 秒、41 个候选；
    **严格晋升 0 条**，放宽库 41 条。
  - 评测：`experiments/real_model_20261108/bench/`，270 例 × 4 模式 = **1080 run**、1036 次模型调用；
    `baseline.py check` 通过（产物差异 0、代码漂移 0、分母自洽）。
- 评测入口现在强制显式选库与显式输出目录：`--library-dir` / `--output-dir`，
  产物里带 `config_hash` 与 `library_hash`（C 41 条、D 0 条）。

### 结果（不加修饰）

| 测试集 | A | B | C（不验证） | D（严格） |
|---|---|---|---|---|
| 单故障 204 | 0.0% | 26.5% | 100.0% | 94.1% |
| 复合 30 | 0.0% | 0.0% | 0.0% | 0.0% |
| 难度组 36 | 22.2% | 44.4% | 69.4% | 69.4% |

**D 库是空的**，所以 D 的 94.1% 全部来自「查不到技能就退回按根因的固定策略表」。
证据：`data/real_model.db` 里 D 模式的 `recoveries` 记录**没有 `skill_id`**（C 的有）。
复合故障 D 0% 同因：链技能一条都没晋升。

### 为什么一条都晋升不了

`skills.evaluate` 给出的判决理由是：

```
['target has fewer than 3 independent tasks/families',
 'guard has fewer than 3 independent tasks/families',
 'guard has fewer than 3 baseline-success tasks/families']
```

`target_n = 1`、`guard_n = 2`（独立任务族 1）。数据集里每个 (agent, fault) 只有 1 个 dev 任务，
而门槛要求 target / guard / guard 基线成功各 ≥ 3 个独立任务。**门槛是对的，数据不够。**

后果要说清楚：历史那份「单故障 D 97.1% / 复合 100%」是**门禁前**直写库（38 条、不校验评估）
跑出来的，与本次不是同一实验条件，不能混算；本次 C 100% > D 94.1% 也不能读成
「不验证更好」—— 那是「有技能 vs 没技能」，不是「严 vs 松」。

### 过程中修掉的两个真 bug

1. **库指纹退化成空**：`--library-dir` 记录的是 `skillset["d_path"]`，而那是**文件名**不是完整路径，
   `os.path.exists` 判假 → 库集合为空 → `library_hash` = `sha256("{}")`，所有库看起来一样，
   指纹等于没做（第一轮结果就是这样，已删除重跑）。修法：改用 `LIB_C` / `LIB_D` 完整路径；
   库文件缺失时直接拒绝出报告（退出码 5）；`baseline.freeze` 收到空库集合时抛错；
   加回归测试 `test_freeze_rejects_empty_library_set`。
2. **续跑守卫过严**：`--output-dir` 只要目录非空就拒绝，把「断点续跑」也挡了。
   改成只拒绝**不含 `runs.jsonl`** 的非空目录；是否同一基线交给 `check_resume`。

### 仍未完成

- **数据集扩充**：每个 (agent, fault) 至少 3 个独立任务，才可能通过样本充分性门槛。
  这是让 D 有意义的前置条件，不是可选项。
- 复合故障的链式技能同样需要 ≥3 个独立任务才能验证。
- 不要用下调 `MIN_VALIDATION_TASKS` 的方式让数字变好看。

## 2026-11-09：数据集扩充到「每个 (agent,fault) ≥3 独立任务」

### 为什么做

上一轮门禁后重跑，41 个候选全部 `NEEDS_REVIEW`，理由是
`target has fewer than 3 independent tasks/families`（`target_n=1`、`guard_n=2`）。
门槛没错，是数据只有 1 个验证任务 —— 所以改数据，不动门槛。

### 改了什么

- 每个 agent 的任务从 8 个扩到 **15 个**（订单 A1001–A1015 / 知识库 KB-01–KB-15 对应 15 个问题 / 15 条流程）。
- dev 从 2 个扩到 **9 个**，并按角色切三组、任务不重叠：
  `dev_train_tasks`（前 3，探索用）/ `dev_target_tasks`（中 3，量增益）/ `dev_guard_tasks`（后 3，量负迁移）。
- test 仍是 6 个任务 → 单故障测试集**仍是 204 例**，与历史数字可比；复合测试 30 例不变。
- 难度组训练任务从 2 个扩到 6 个（3 target + 3 guard），calc / xref 的技能才有机会过门禁。
- `--guard-n` 默认 2 → 3（门禁要求 ≥3 个独立 guard 任务族）。
- 复合故障按**任务角色**分 train/val，不再按列表奇偶交替 —— 交替会把同一任务的复合用例放进
  val 而它的单故障在 train，`evaluate` 直接抛 `validation overlaps training task/family`（实测踩到）。

### 验证

- 全量 **189 个测试**通过。
- mock 全量训练（`experiments/p0_08_20261109/mock_full/`）：157 个探索用例 → **116 个候选**
  → 判决分布 `PROMOTE 11 / REJECT 66 / NEEDS_REVIEW 39` → **严格库 11 条**（非空），
  放宽库 116 条。首次出现「严格库非空」，说明划分对了。
- 仍被 `NEEDS_REVIEW` 的 39 条，理由统一是
  `guard has fewer than 3 baseline-success tasks/families`（`guard_tasks=3` 但基线只救回 2 个）——
  这是诚实的证据不足：基线本来就失败的任务，测不出负迁移。
- `REJECT` 66 条是增益为 0（规则表本来就能救），门禁拒得对。
- 扩集后新的闭环基线（mock，`experiments/p0_09_20261109/audit_smoke/`）：
  单故障 **A 0.5 / B 24.0 / C 89.2 / D 86.3**，复合 **3.3 / 86.7 / 86.7**，晋升 7 条。
  与历史的 0 / 26.5 / 100 / 100 **不可比** —— test 集换了题目，旧数字只能当旧数据集的结果。

### 仍未完成

- 真实模型全量重训 + 评测（本轮只做了 mock 验证，未跑 Qwen）。
- 39 条「guard 基线成功不足」需要换 guard 故障类型或补更多 dev 任务，而不是放宽门槛。

## 2026-11-09：安全层（输入/输出校验、防火墙检测、最小权限、多 Agent 互信）

### 做了什么

- 新增 `ai-service/evoguard/security.py`：24 个检测器（注入 / 外带 / 凭据 / PII /
  危险命令 / XSS）+ 输出编码（html/json/text）+ 不可信内容包裹 + 最小权限白名单 +
  对端校验 + 事件落库（`security_events` 表）。
- 运行时 4 个钩子：`run_once`（输入）、`Ctx.call` 入口（最小权限）、`Ctx.call` 出口（工具返回扫描）、
  `Ctx.answer`（模型输出扫描）。**三种模式** off / audit / enforce，默认 audit：
  只检测只记账，不改任何内容，历史实验数字仍然可比。
- 检查器新增 `security` 分组 6 条轨迹规则：`injection_in_tool_output`、`tool_not_allowed`、
  `data_exfiltration`、`secret_in_tool_args`、`dangerous_shell_command`、`xss_in_answer`。
  **复用运行时同一套检测器**，不另抄正则 —— 离线报告和运行时拦截必须看同一组特征。
- API 边界加安全头：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`、CSP。
- 多 Agent：`check_peer` + `guard_peer_message`（未声明对端消息一律当不可信数据）。
  **如实说明**：本仓库运行时是单 Agent，没有 handoff 路径，这层目前是库级能力 + 离线规则。

### 验证

- 全量 **216 个测试**通过（56 核心 + 9 + 19 + 44 检查器 + 27 静态 + 36 技能 + 25 安全）。
- `tests/test_security.py` 25 项：14 类攻击逐一命中、5 条正常业务文本零误报、
  每条发现都带误报说明、前置过滤表不许漏、编码/包裹/结构保持、最小权限与对端校验、
  三模式行为差异、事件落库、运行时 audit 不改结果 / enforce 拒绝越权工具。
- `tests/test_inspect.py` 新增：攻击夹具六条安全规则全部命中；正常轨迹（含 17 条合成坏轨迹）
  零安全误报。
- 延迟：1 KB 0.044ms / 10 KB 0.31ms / 50 KB 1.84ms；真实运行约 4 次扫描 ≈ **51µs/run**，
  对真实模型（627ms/run）是 **0.008%**。预算断言 10KB < 1.5ms。
  数据 `experiments/p0_09_20261109/security_bench.json`。
- 攻击报告 `experiments/p0_09_20261109/attack_report/`：11 个发现（5 error），
  security 组 7 条。

### 仍未完成 / 不能宣称的

- **不能说「已防住注入」**：特征匹配，换说法、拆字、base64、多语言混写都能绕过。
- 审批与沙箱仍是**声明**：静态规则只检查「有没有写」，运行时不强制执行。
- `api/app.py` 加的是安全头，不是 WAF：没有限流、认证、租户隔离。
- 多 Agent 只到库级：没有消息签名与消息级权限。
- 实测结论要如实写：同一数据集下 `off` / `enforce` / `enforce+wrap` 的闭环指标**完全一致**
  （单故障 0.5/24.0/89.2/86.3，复合 3.3/86.7/86.7，晋升 7），因为 Agent 是规则式、
  不读被包裹的文本。**真实模型读 prompt 时包裹的代价没测过**，不能假定为零。
- 更要紧的一条：这里的数字与历史 0/26.5/100/100 **不可比** —— 数据集从每 agent 8 个任务
  扩到 15 个，test 集虽然还是 204 例，但装的题目换了。历史数字只能当「旧数据集上的结果」。

## 2026-11-09：保障视图（可靠 / 防护 / 可运行三层判定）

### 定位

这个项目不是「模型评测工具」，是 **AI 保障系统**：
既要保证 Agent 可靠可运行，又要防护被其他 AI 或人攻击。在此之前可靠与防护是两个平行模块，
没有任何一处的结论是「所以这个 Agent 能不能放心用」。这一节补的是那层粘合。

### 做了什么

- 新增 `ai-service/evoguard_inspect/assurance.py`：把轨迹规则按维度分组（可靠 25 条 / 防护 6 条），
  再从轨迹里算可运行指标（模型与工具报错率、预算触发、工具耗时 p95、报错后仍有答案的条数），
  给出三维判定（通过 / 有风险 / 不通过 / 覆盖有限 / 无法判定），总判定取最差。
- **刻意不给 0–100 分**：分数看着专业，但没人知道 82 分能不能上线，而且会让人优化分数。
  每维固定写「测了什么 / 没测什么」，缺口不许省略。
- **零命中 ≠ 通过**：有规则因输入不足没跑时判「覆盖有限」，并在报告里列出跳过的规则名。
- **数据画像**：报告头部标注多少条轨迹带注入故障，避免把注入数据的失败率当成线上失败率。
- CLI 新增 `--assurance`，退出码 0 通过/风险、3 不通过、2 解析失败。
- `loaders` 支持直接把**运行库**当输入（按 SQLite 文件头识别，不看后缀），并去掉原来
  `LIMIT 200` 的静默截断 —— 截断会让人以为「全查过了」。
- `Step` 增加 `duration_ms`，OTel loader 用 `endTimeUnixNano - startTimeUnixNano` 计算，
  本项目 loader 读库里已有的耗时；没有耗时记 0 并显示 `N/A`，不假装很快。
- 运行时：模型报错后仍出答案时，在 `run.reason` 追加 `degraded:<code>`，
  让「看起来成功、其实是规则兜底」这种情况在记录里可见。

### 验证

- 全量 **224 个测试**通过（新增 `test_assurance.py` 8 项：攻击轨迹判不通过、干净轨迹覆盖有限不算通过、
  空输入无法判定、报错后仍有答案判风险、高报错率判不通过、健康运行通过、报告含全部维度与边界、不给分数）。
- 真实产物 `experiments/p0_09_20261109/assurance/`：3205 条轨迹（全部带注入故障），
  判定「可靠 不通过（2446 发现）/ 防护 覆盖有限（未配 allowed_tools，越权那条测不了）/
  可运行 有风险（6% 模型报错、165 条报错后仍出答案）」。
  这份结果本身就说明了设计意图：防护维度没配白名单时**明确说测不了**，而不是报「无问题」。

### 仍未完成

- 判定阈值（报错率 20% 不通过 / 10% 风险）是经验值，没有线上数据校准。
- 可运行只看轨迹：没有 SLA、并发、成本曲线。
- 人工标注集仍缺，所以三个维度都只有覆盖率，没有准确率。

## 2026-11-09：审批与参数拦截从「声明」变成「真执行」

### 为什么做

之前的安全层有一句自己写着、也必须承认的话：**审批和沙箱只是声明**。
静态规则检查「有没有写审批」，但运行时 `AGENTLAB_AUTO_APPROVE=1` 默认就放行，
写操作照样执行 —— 等于没有审批。这一轮把两件事变成真的。

### 做了什么

- 新增 `security.guard_tool_args`：**调用前**扫参数（凭据 / 危险命令 / 外带），
  `enforce` 下命中高危直接拒绝执行，抛 `security_blocked`。
  为什么必须在执行前：危险命令和外带一旦发出去，事后只能看到结果。
- 写操作审批：`enforce` 下 `AUTO_APPROVE` 不再能默默放行 —— 恢复期重放写操作
  必须显式 `AGENTLAB_ALLOW_AUTO_APPROVE=1`，否则抛 `E_APPROVAL_REQUIRED` 并中断恢复。
- 默认 `audit` 模式保持自动放行，但每次放行记一条 `approval_bypassed`，
  报告里能看到「审批被绕过多少次」。
- 修掉一个真实漏检：`rm -rf /` 的检测正则要求 `/` 后跟空白或行尾，
  而工具参数是 JSON（`{"cmd": "rm -rf /"}`，`/` 后是引号）→ **完全漏检**。
  改成后瞻断言后覆盖 JSON 场景，同时确认 `rm -rf /tmp` 不误报。

### 验证

- 全量 **229 个测试**通过（新增 5 项：enforce 拦截危险参数、audit 只记录仍执行、
  enforce 下写操作要审批、显式覆盖可放行、audit 记录 approval_bypassed）。
- 三模式对照（`experiments/p0_09_20261109/mode_compare.json`，同数据集、mock 后端）：

  | 模式 | 单故障 A/B/C/D | 复合 单策略/链/D | 晋升 | 安全事件 |
  |---|---|---|---|---|
  | 默认（只检测） | 0.5 / 24.0 / 89.2 / 86.3 | 3.3 / 86.7 / 86.7 | 7 | approval_bypassed 825 |
  | enforce（真拦） | 0.5 / 15.2 / 62.7 / 59.8 | 3.3 / 46.7 / 46.7 | 5 | approval_required 633 |
  | enforce + 包裹 | 同 enforce | 同 enforce | 5 | 同 enforce |

  **-26.5pp / -40pp 就是「审批真执行」的价格**，也是它和「只声明审批」的区别。
  包裹本身不改变指标（Agent 是规则式，不读被包裹文本）。

### 仍未完成

- **沙箱没有**：能拦危险命令参数，但没有执行隔离层。
- 审批只有「拦住 / 放行」两态，没有真正的审批工作流（谁批、批了什么、超时怎么办）。
- enforce 模式的数字与默认模式不可混算，报告里必须标模式。

## 2026-11-09：真实模型全量重训 + 评测（数据集与审批都更新后）

### 训练（Qwen3 4B / Ollama，`experiments/real_model_20261109/train/`）

- 探索 147 例 → 127 个候选（其中组合链 15）；1421 秒、1845 次运行。
- 判决：**PROMOTE 11 / REJECT 64 / NEEDS_REVIEW 52**；严格库 **11 条**、放宽库 127 条。
- 11 条严格晋升分别是：`planner/incorrect_planning` ×2、`order/missing_field+silent_corruption` ×3、
  `order/wrong_tool+format_violation` ×3、`planner/incorrect_planning+premature_termination` ×3。
  这四条都是**规则表里没有对应策略**的故障 —— 进化第一次在真实模型上产出有效且验得过的技能。

### 评测（`experiments/real_model_20261109/bench/`）

- 270 例 × 4 模式 = 1080 run、1058 次模型调用、680 秒；manifest 校验通过（C 127 条 / D 11 条）。
- 单故障：A 0.0 / B 22.1 / C 82.8 / **D 79.9**
- 复合：A 0.0 / B 0.0 / C 0.0 / **D 53.3**
- 难度组：A 22.2 / B 44.4 / C 75.0 / **D 69.4**

### 三条读法

1. **复合故障上 D 第一次真正领先**（53.3% vs C 0%）。C 的 127 条技能里单步技能把两步链挤到轮不到的位置，
   D 的 11 条含链才救得回来。查库核实：D 恢复记录 **54/280 次带 `skill_id`**（其余退回规则表），C 是 262/262。
2. **单故障 C 82.8 > D 79.9**：宽松库覆盖广、代价是复合故障归零。不是「不验证更好」，
   而是「广撒网 vs 精确」的取舍。
3. **与历史的 97.1% / 100% 不可比**：那是门禁前直写库 + 旧数据集跑出来的。

### 发现的缺口（没修，先说清楚）

- `calc` / `xref` 两个难度组 Agent 的 13 个候选 `gain=None`，理由
  `target has fewer than 3 independent tasks/families`：难度组只有 **6 个训练任务**（3 训练 + 3 验证），
  没有第三组做 guard，所以它们的技能永远验不过 —— 难度组的 D 69.4% 是**规则表**的成绩，不是学到的技能。
  修法是把难度组训练任务补到 9 个（3 训练 / 3 target / 3 guard），补完要重训+重跑评测。
- kb 的候选仍是 `NEEDS_REVIEW`（guard 基线成功任务不足 3 个）：基线本来就救不回的 guard 任务测不出负迁移。

## 2026-11-09：难度组补到 9 个训练任务 —— 修好了划分，露出真正的瓶颈

### 改了什么

难度组（`calc` / `xref`）的训练任务从 6 个补到 **9 个**（3 训练 / 3 target / 3 guard，
后 6 个任务不重叠），`_task_roles` 与 `split_cases` 同步改成 `tt[:3] / tt[3:6] / tt[6:9]`。
上一轮它们 13 个候选 `gain=None`（`target has fewer than 3 independent tasks/families`），
就是 target 与 guard 抢同一批任务导致的。

### 结果（`experiments/real_model_20261109b/train/`，Qwen3 4B，1438 秒 / 1923 run）

- `gain=None` 从 **13 条降到 0 条** —— 划分修对了。
- 硬组 13 个候选判决：`calc` 9 个全 `REJECT`（理由 `no target gain`），`xref` 4 个全 `NEEDS_REVIEW`。
  **一个都没晋升**，严格库仍是 11 条（`order` ×6、`planner` ×5）。
- 检查严格库内容：与上一轮**同构**（同样的 task_type / fault / strategy / scope），
  所以评测结论不变（D 单故障 79.9%、复合 53.3%），不重复跑 11 分钟评测。

### 这暴露了什么（比数字重要）

硬组的 13 个候选 **增益全是 0**：`retry` / `fallback_tool` / `replay_checkpoint` 这些策略
在规则表里本来就有，技能给不出增量。也就是说：

> **难度组失败的原因不是「缺策略」，而是「模型不会」** ——
> 4B 模型不做多步算术（先满减后折扣）、也不比文档版本号。
> 技能层是「遇到 X 用 Y 策略」的映射，映射不出「会算数」。

所以难度组 A 22.2% → 崩、D 只能回到 69.4%（规则表 + 重放），这不是治理没做好，
而是**这类任务的瓶颈不在恢复层**。要把这条线做上去，得换更强的模型或改任务设计，
不是继续加技能。

### 仍未完成

- kb 的 39 个候选仍是 `NEEDS_REVIEW`（guard 里基线成功的独立任务不足 3 个）。
- 硬组若要真正验证「技能能不能救」，需要挑规则表**没有**对应策略的硬组故障类型。

## 2026-11-09：标注集与准确率口径（补上最缺的一块）

### 做了什么

- 新增 `scripts/build_labeled_set.py`：用**执行时的注入状态**当 ground truth，生成 37 条标注轨迹
  （`clean` 12 / `failed` 17 / `recovered` 8，backend=mock），导出成 OTLP，
  检查器走的代码路径与检查别人时完全一样。
- 新增 `ai-service/evoguard_inspect/labels.py` + `scripts/eval_labeled.py`：
  **两档口径**统计（确诊 = error 级；提示 = warn 及以上），分母为 0 时给 `None` 不给 0，
  `unjudgeable`（没有 error 级规则可跑）从分母剔除。
- 新增 `docs/labeling-protocol.md`：标签定义、两档口径、不能怎么用、要变成真准确率还差什么。
- 新增 `tests/test_labels.py` 15 项：指标算法、空壳答案归一、中文改写、恢复重放。

### 顺带修掉三处真实误报（都由这个标注集暴露）

1. **空壳答案**（`{"parsed": null, "text": ""}`）被当成真答案 → 32/32 条正常轨迹被判「没有答案」。
   修法：`otel.unwrap_answer` 与 loader 侧同时归一，并且只认**真的存在** `parsed` 键的字典
   （第一版把 `{"order_id": ..., "total": ...}` 也当成空壳丢了，反而更糟）。
2. **中文改写**被判成参数编造：用户问「退货要几天内」，检索词「退货时限」→ `dependency_mismatch` 误报 10/20。
   修法：含中文的值放宽到 2-gram 重叠；纯 ASCII 的 ID 仍按子串判。
3. **恢复重放**被当成循环：`cycle_detected` 把 `kind=replay` 的步也算进 A→B→A→B，D 模式全被误报。
   修法：重放步不参与循环检测。

### 当前数字（37 条构造集，mock）

| 口径 | 检出 | recall | 误报 | precision |
|---|---|---|---|---|
| 确诊（error 级） | 7/17 | 0.412 | 0/20 | 1.000 |
| 提示（warn 及以上） | 7/17 | 0.412 | 2/20 | 0.778 |

### recall 只有 41% 的原因（不是调参能解决的）

漏掉的 10 条全是「内部自洽但结论错」：`silent_corruption`（A 模式）、`stale_data`、
`incorrect_planning`。这些需要**外部真值**（政策库 / 版本元数据 / 流程定义）才判得出，
轨迹内部一致性推不出来。这条写进了 `docs/labeling-protocol.md` 第四节。

### 仍未完成

- 37 条全部来自本仓库自己跑的数据，**没有外部真实业务轨迹**；样本量也低于计划的 30 条
  （按「可判定条数」算是 37，但分布不真实）。
- 标注者仍是规则作者本人 —— 换成外部标注者前，这些数字只能当回归护栏。

---

## 2026-11-09（第二阶段）：阶段 A「防护证据可信」完成（S01 / S02 / S03）

按 `EvoGuardLab_安全防护与下一阶段推进计划_2026-11-09.docx` 第 13 节推进：
先做 S01、S02（会话 1、2），再做 S03 清理旧描述（会话 3）。
起点已复核：HEAD `74d4312`、工作区干净、248 项测试全绿。

### 完成事项

**S01 让检测与执行结果分开记录**
- 复现反例：`enforce` + 未设 `AGENTLAB_SECURITY_WRAP` 时，`guard_tool_output` 返回
  `action="blocked"`、命中 `injection_override`，但 `text` 与输入逐字相同、
  `Ctx.call` 也没停止调用 —— `blocked` 只是检测建议。
- 四个 guard 统一返回六字段契约 `detected / decision / attempted / executed / effect / reason`；
  `action` 保留仅作兼容，语义降级为检测建议。
- 新增 `security_effects` 表 + `record_effect()` / `list_effects()`；`Ctx.call`、
  `Ctx.answer`、`run_once`、审批点共 6 个真实执行点逐点落痕，可按 `run_id` 与
  `security_events` 对上。未改正文、未停止调用时一律写 `executed=False / unchanged`。
- 验收：反例测试在修复前 **2 failures + 6 errors**，修复后 8 项全绿。

**S02 重做四类演示的判据**
- `scripts/defense_demo.py` 重写为 v2：四档（off / audit / enforce / enforce+wrap）
  × 四类攻击 + 各自的合法业务对照；只按真实后果计量（未授权调用是否执行、
  `FakeSink` 实际命中、危险操作是否执行、输出是否仍可被当代码、任务是否完成）。
- 修掉 v1 虚报：危险命令 / XSS 分支不再记 `leaked`；`dangerous_executed` 恒 False
  并注明「演示本身拒绝执行命令」。`sink_hits` 必须回指真实放行的 `http.send`。
- 间接注入加**后续动作链**：真的采用 guard 返回的 `text` 回灌给决策器，再尝试
  `http.send`。默认决策器是确定性最坏情况观测器（**不是真实模型**），
  报告把真实模型复核标 N/A；`--followup real` 可接本地模型，不可用时降级为 N/A。
- 关键结论：`enforce`（未开 wrap）下间接注入正文**没变**，攻击没达成靠的是后续
  `http.send` 被白名单拒，不是「认出了注入」；只有 `enforce+wrap` 才真的改了回灌文本。
- 支持 `--out`，默认 `experiments/defense_demo_v2/`；v1 冻结原件不动。

**S03 清理会改变理解的旧描述**
- `security.py` 顶部 docstring、`docs/security.md`、`docs/assurance.md`、
  `evoguard_inspect/assurance.py` 里「权限与审批只有声明 / 运行时不强制」
  「人工标注集缺失」「前两项只是文档与规则」等旧描述全部按现状改写。
- `scripts/eval_labeled.py`：去掉硬编码「49 条」，条数从 `n_total` 动态取；
  支持 `--out`（默认 `experiments/labeled_eval_v2/`）；修章节编号；
  补上 Q02 限制（白名单由轨迹反推，不能用来判越权）。
- `ai-service/real_model_train.py`：docstring 与报告表格改成
  `dev_train_tasks / dev_target_tasks / dev_guard_tasks` 三组 3/3/3；
  顺带修掉「dev 每 agent 只有 2 个 task」这条更严重的旧描述。
- 新增当前版本报告：`experiments/labeled_eval_v2/`、`experiments/assurance_v2/`；
  冻结历史原件（`labeled_eval/`、`p0_09_20261109/assurance/`、各 `train_report.md`、
  `defense_demo/`）**保留旧数字不改写**，并由 `tests/test_docs_consistency.py` 守住。

### 证据与数字

- 全量回归 **280 项全绿**（248 + 8 契约 + 12 演示 + 12 文档一致性）。
- 演示四档（`experiments/defense_demo_v2/demo.json`）：

| 模式 | 检测 | 控制动作真执行 | 接收方命中 | 攻击目标达成 | 正常误阻断 |
|---|---|---|---|---|---|
| off | 0/4 | 0 | 2 | 3 | 0.0 |
| audit | 4/4 | 0 | 2 | 3 | 0.0 |
| enforce | 4/4 | 4 | 0 | 0 | 0.0 |
| enforce+wrap | 4/4 | 4 | 0 | 0 | 0.0 |

- `baseline.py verify` 冻结 manifest：**产物差异 0 处**，代码漂移 3 个文件
  （`runtime.py` / `sim.py` / `real_model_train.py`）。它证明历史证据未被改写，
  **不证明当前代码重新产生相同结果**。
- 标注集当前口径仍是 error 级 7/17、误报 0/20；warn 级误报 2/20，未变。

### 仍不成立的主张（不能说）

- ❌「防住提示注入 / 生产级 WAF」：检测是特征匹配，改写就能绕。
- ❌「4/4 拦截」「leaked=3」：v1 的高估与虚报，已在 v2 更正。
- ❌「准确率 41%」「误报率 0」：37 条构造集只是回归护栏。
- ❌「所有收益来自验证门禁」：C/D 还差匹配、容量、状态过滤，未做公平消融。
- ❌「多 Agent 已安全协作」「存在完整沙箱」「审批可暂停续跑」：都还没实现。

### 本轮没做（如实标注）

- 没重训模型、没重跑 1080 run 完整评测；本轮只改本地代码与文档。
- 间接注入的**真实模型复核未执行**（默认观测器不是真实模型），该项标 N/A。
- 阶段 B 的审批状态机、真实 HTTP/SQLite 副作用、独立授权策略与外部轨迹都没动。

### 下一项验收（阶段 B，对应计划会话 4–6）

1. 会话 4：冻结全部安全开关（mode / wrap / auto_approve / 工具契约 / 输出落点）
   并比较四模式，产出逐 case 结果、分母与配置指纹；`--out` 隔离，不覆盖冻结证据。
2. 会话 5：`approval_request` 状态 + 规范化参数摘要，覆盖批准 / 拒绝 / 过期 /
   参数篡改 / 重复消费 / 进程重启恢复，首次写入与重放写入走同一个执行门禁。
3. 会话 6：本地 HTTP 测试订单 + 沙箱规则文件 + 专用 SQLite 结算表，
   验证「提交成功但响应丢失仍只写一次」；`sim` 保留作回归。
4. 前置依赖：Q02 从事先冻结的策略取白名单、Q03 外部轨迹与独立标注。

---

## 2026-11-09（第三阶段）：会话 4「冻结安全配置并比较四模式」

### 完成事项

- **先坐实反例**（计划要求「一次会话先确定一个可观察反例」）：同一个
  `mode=enforce`、`AGENTLAB_AUTO_APPROVE=1`，仅 `AGENTLAB_ALLOW_AUTO_APPROVE`
  0→1，恢复期 `order.settle` 就从 `E_APPROVAL_REQUIRED` 变成
  `{'settled': True}`（真的落库）。只记 `mode` 无法解释这个差异。
  用**真实 runtime 子进程**复现（`AUTO_APPROVE` 是 import 时冻结的常量，
  运行时改环境变量不生效，所以必须换进程）。
- 新增 `scripts/freeze_mode_compare.py`：把 10 类有效开关写成规范化配置并算
  sha256 指纹；复用 S02 演示引擎跑 off / audit / enforce / enforce+wrap 四档，
  产出 **32 条逐 case 结果**（4 模式 × 8 场景）、明确分母、零分母 `null`、
  缺失成本记 N/A，以及 `manifest.json`（威胁模型版本、攻击集/正常集内容指纹、
  独立授权策略、安全模式、包裹、自动审批、工具契约、输出落点、4 个文件的代码指纹）。
- 新增 `docs/mode-compare-protocol.md`：威胁模型、指标定义与分母、配置清单、
  不能怎么用、数量不足如实标注。
- 新增 `tests/test_mode_compare.py` 14 项：真实 runtime 反例、指纹确定性与可区分性、
  汇总与逐 case 一致、零分母 null、N/A 不写 0、冻结历史不被覆盖。

### 证据与数字

| 模式 | 业务成功 | 攻击目标达成 | 真实阻断率 | 正常误阻断率 | 配置指纹(前 12) |
|---|---|---|---|---|---|
| off | 8/8 | 3 | 0.0 | 0.0 | `83054074407b` |
| audit | 8/8 | 3 | 0.0 | 0.0 | `7526dafbfe1c` |
| enforce | 6/8 | 0 | 1.0 | 0.0 | `be2d2ce9838d` |
| enforce+wrap | 6/8 | 0 | 1.0 | 0.0 | `f70caf5921e5` |

- **别把 8/8→6/8 读成防护损失**：下降的两条全部是攻击场景的工具调用被拒，
  正常任务误阻断率仍为 0.0（测试里逐条核过这两条的 `kind=attack` 与 `call_blocked`）。
- `enforce` 的间接注入仍是 `effect=unchanged`，只有 `enforce+wrap` 才 `body_wrapped`。
- 旧 `experiments/p0_09_20261109/mode_compare.json` 保留不动（只有聚合百分比、
  无逐 case、无配置），新产物在 `experiments/mode_compare_v2/`。
- 全量回归 **294 项全绿**（280 + 14）。

### 仍不成立的主张（不能说）

- ❌ 与历史 1080 run 真实模型评测比大小：数据集、配置、后端都不同，
  且历史 manifest 未登记完整安全开关，不能事后补猜成某种模式。
- ❌ 把审批开关的影响算进本表：演示引擎不含写操作，其真实影响由子实验单独验证。
- ❌ 准确率 / 真实业务效果 / 真实模型复核：8 个构造场景、受控观测器、N/A。
- ❌「防住提示注入 / 生产级 WAF / 完整沙箱 / 多 Agent 已验证」：同上一轮，仍不成立。

### 本轮没做（如实标注）

- 场景数 **8 个**，低于计划 §5 建议的 12 个工程冒烟起点，更没到 30 攻击 + 30 正常；
  没靠改措辞硬凑独立样本。
- 2×2 消融（固定审批策略比内容隔离、固定隔离方式比审批策略）未跑。
- 计划 §5「先改权限结构再补特征」未做：本轮只验证既有白名单与参数拦截，没加正则。
- 未重训模型、未重跑 1080 run、未动冻结 manifest。

### 下一项验收（会话 5，阶段 B）

`approval_request` 状态 + 规范化参数摘要：绑定 `run_id` / 工具 / 参数摘要 /
资源范围 / 候选版本 / 过期时间 / 批准人 / 一次性消费；暂停时存检查点、
批准后续跑；覆盖批准、拒绝、过期、参数篡改、重复消费、进程重启恢复。
首次写入与重放写入必须走同一个执行门禁。之后才是会话 6 的本地 HTTP +
SQLite 幂等闭环。

---

## 2026-11-09（第四阶段）：会话 5 + 6「审批状态机与真实副作用闭环」

### 完成事项

- 新增 `ai-service/evoguard/approval.py`：`approval_request` 绑定 run_id / 工具 /
  规范化参数摘要 / 资源范围 / 候选版本 / 24h 有效期 / 批准人 / 一次性消费状态 /
  权限指纹 / 检查点；状态 `pending → approved → consumed`，或 `rejected` / `expired`。
  参数变化、权限变更、过期、拒绝、重复消费一律拒绝并留原因。
  规范化把 `504` 与 `504.0`、键值顺序、字符串空白归一（实测踩过 int/float 不一致）。
- `runtime.Ctx` 新增可选 `approval_gate`：**首次写入与恢复期重放写入走同一个执行门禁**，
  补上旧实现只拦恢复期重放的缺口；暂停写 `effect=call_stopped`（含 request_id），
  工具未执行。默认 `None`，历史行为与冻结数字不变（有专门一条回归守着）。
- 新增 `ai-service/evoguard/real_tasks.py`：127.0.0.1 本地 HTTP 读测试订单 →
  项目沙箱内 `tests/fixtures/settlement_rules.json` → 按规则算总额 →
  过统一门禁 → 专用 SQLite 结算表（`idempotency_key` + `INSERT OR IGNORE`）。
  不碰外部账号、真实支付、任意 shell 执行；`sim` 保留作快速回归。
- 新增 `scripts/real_settlement_demo.py` 与 `docs/approval-protocol.md`；
  `tests/test_approval.py` 15 项。产物 `experiments/real_settlement_v1/`。

### 证据：计划「必须测试的情形」5/5 通过

| 情形 | 结果 | 关键证据 |
|---|---|---|
| 首次写入没有审批 | 通过 | pending + request_id + checkpoint_id，`ledger.count=0` |
| 审批前后参数变化 | 通过 | `E_APPROVAL_INVALID`，旧审批仍 `approved` 且未消费 |
| 提交成功但响应丢失 | 通过 | `ResponseLost` 带幂等键，重试 `created=False`，`count=1` |
| 拒绝与过期 | 通过 | `rejected` / `expired` + reason，工具未执行 |
| 暂停后进程重启 | 通过 | 重开 Store 后 `resume()` 读回检查点与 approved，不重复写入 |

额外：权限变更、重复消费、参数规范化、沙箱路径越界、
首次/重放同一门禁、默认 runtime 行为不变，共 15 项回归全绿。

- 全量回归 **309 项全绿**（294 + 15）。
- 冻结 manifest 复核仍是「产物差异 0 处」，代码漂移 3 文件 →
  历史证据未被改写，不能声称当前代码重跑出相同结果。

### 仍不成立的主张（不能说）

- ❌ 生产级审批工作流：无鉴权 / 多租户 / 审计访问控制 / 限流 / SLA。
- ❌ 防住越权：白名单与权限契约由调用方传入，本模块只校验摘要没变。
- ❌ 真实金融结算：回环测试服务 + 沙箱规则 + 专用 SQLite 表。
- ❌ 与历史 1080 run 比大小；❌ 防住提示注入 / 完整沙箱 / 多 Agent 已验证。

### 本轮没做（如实标注）

- 会话 7（独立授权策略 + 外部轨迹小批量）未开始；Q02/Q03 仍未关闭。
- 计划 §6「按技能 `safety` 声明决定审批」未做：`safety` 没参与门禁判定。
- **首次写入审批在默认 runtime 里仍未启用**，只在显式挂 gate 的真实任务链上生效；
  要在全部运行上启用需重跑实验并重算历史数字，不能悄悄改。
- 2×2 消融、30+30 扩样、1080 run 重跑与公平消融未做。

### 下一项验收（会话 7，阶段 C 前置）

从事先冻结的 Agent 策略取白名单（不从已执行轨迹反推，Q02），
补至少 30 条外部可授权轨迹并按任务约束独立标注（Q03）；
按规则 / 严重级别 / 入口 / 任务家族报 TP/FP/FN 与适用率，零分母继续 null。
之后再进入条件技能实验（阶段 D）。

---

## 2026-11-09（第五阶段）：会话 7 + Q01–Q05（评测质量与 CI 策略）

本轮连续完成会话 7 与阶段 C 的五项质量任务。

### 完成事项

**Q02 独立授权策略**（`scripts/eval_permission.py`、`tests/fixtures/labeled/policies.json`）
- 白名单只从事先冻结的手写策略取，与 `security.AGENT_TOOLS` 一致（测试断言）。
- 没有独立策略的样本标 `unjudgeable_permission`，**不计入分母也不静默通过**。
- 实测反例：`trace_attack.json` 执行了白名单外工具，反推白名单检出越权 **0**，
  冻结策略检出 **2**，差异 2 条 —— 不分离口径越权永远测不出来。
- 适用率 1.0（38/38 可评，含 1 条受控反例）。

**Q01 三种真值 + Q03 独立标注协议**（`independent_labels.py`、
`scripts/build_external_label_registry.py`、`docs/external-labeling-protocol.md`）
- 分开存 `execution_state` / `observable_fault` / `business_outcome`。
- 待复核清单：自生成 37、**外部可授权 0 / 独立人工标注 0 / kappa `null`**，
  全部 `pending_independent_review`，不冒充人工标签。
- 外部轨迹必须带 `source` + `authorization` 才计入；分歧标 `needs_discussion`，
  先讨论再定不取多数；`cohen_kappa` 零分母给 `None`。

**Q04 按维度覆盖**（`assurance.py`）
- 不再拿整份规则的一个 `cov_ok` 压所有维度；reliability / security 各自算规则覆盖，
  operability 按观测字段算，每维报适用/实检/跳过/原因/观测数。
- 安全缺白名单只压 security，不牵连可靠性；补白名单只修好 security。
- 原 `tests/test_assurance.py` 8 项保持全绿。

**Q05 CI 发布策略**（`ci_policy.py`、`cli.py --policy`、
`tests/fixtures/ci_policy.json`、`docs/ci-policy.md`）
- `assurance.json` 存报告判定，`ci_decision.json` 存 `ci_action` /
  `block_pipeline` / `exit_code` / `reasons` / 策略指纹，两者分开。
- `review` 退出码 0 但明确需人工处置；`block` 退出码 3。
- 补掉一个真实漏洞：`coverage_ok=False` 却列不出缺失项时，旧逻辑会因
  「空列表全在允许清单」放行复核，现在必须阻断。

### 证据与数字

- 全量回归 **330 项全绿**（309 + 21 条 Q01–Q05 测试）。
- Q05 CLI 演示：报告 `FAIL` → 流水线 `block` → 退出码 3；
  理由含「报告判定不通过」与「关键安全规则不可评：配置未声明 allowed_tools」。
- 标注回归护栏数字未变：error 级 7/17、误报 0/20；warn 级误报 2/20。
- 冻结 manifest 复核仍是「产物差异 0 处」，代码漂移 3 文件。

### 仍不成立的主张（不能说）

- ❌ 37 条自生成样本是外部真实业务轨迹 / 人工标注 / 真实准确率。
- ❌ 退出码 0 等于全面安全；`review` 等于通过。
- ❌ 权限专项证明了「防住越权」：它只证明口径分离后越权**测得出**。
- ❌ 阈值已校准：10% / 20% 仍是工程经验值，未登记版本与开发集分布。

### 本轮没做（如实标注）

- 外部轨迹 ≥30 条与独立标注者（Q03 的实质部分）**未到位**，只建了协议与清单。
- CI 策略还没直接读 `security_effects` 的执行后果证据来阻断
  「未授权执行 / 未批准写入」，只按报告判定与覆盖决策。
- 按规则 / 严重级别 / 入口 / 任务家族的 TP/FP/FN 分报表未生成。
- 阶段 D 条件技能实验（E01/E02/E03）、公平消融、1080 run 重跑、2×2 消融、
  30+30 扩样都未开始。

### 下一项验收

阶段 D：一个声明式条件决策技能（适用条件 / 必需证据 / 允许工具 / 确定性参数 /
停止条件 / 失败回退），在独立任务家族上做前后配对验证与公平消融；
无稳定收益允许拒绝晋升并发布负结果。

---

## 2026-11-09（第六阶段）：阶段 D / E01 声明式条件技能骨架

### 完成事项

- 新增 `ai-service/evoguard/conditional_skill.py`：技能只有八项声明式字段
  （id/name/applies_when/required_evidence/allowed_tools/decision_params/
  stop_when/fallback），`schema_version=conditional-skill-v1`。
- 外部执行器 `accept()` 强制边界：禁止字段（含嵌套）`code` / `exec` /
  `evaluation_criteria` / `approval_status` / `system_policy` /
  `set_allowed_tools` 等；禁止代码特征字符串（`import ` / `def ` / `exec(` /
  `__` / `subprocess` …）；`decision_params` 只允许四个登记键；
  `allowed_tools` 必须是冻结策略子集且**不得声明写工具**。
- `execute()` 执行前**二次校验**，只做三种确定性选择（`max_version` 按文档版本、
  `declared_order` 按声明业务顺序、`first`），不调用工具、不执行代码；
  缺必需证据 / 无证据 / 超 `max_steps` 一律走 `fallback`。
- `paired_check()`：同 task/seed 前后配对算 gain 与负迁移率；
  **业务收益不能抵消未授权操作**（after 出现 `unauthorized`/`unsafe` 直接 REJECT，
  哪怕 gain=1.0）；无稳定收益允许 REJECT 并发布负结果；无可配对用例给
  `NEEDS_REVIEW` 且零分母 `None`。
- 新增 `docs/conditional-skill-protocol.md`、`tests/test_conditional_skill.py`（21 项）。

### 证据与数字

- 全量回归 **351 项全绿**（330 + 21）；旧 `skills.py` 未改，330 项旧测试不受影响。
- 21 项覆盖：合法技能接受、缺字段拒绝、禁止字段/嵌套拒绝、代码特征拒绝、
  未登记参数拒绝、未知选择器拒绝、越权声明拒绝、写工具拒绝、指纹稳定、
  三种选择器、缺证据/超步数回退、策略收紧后执行被拦、
  配对验证的 PROMOTE / 越权覆盖收益 / 负迁移 / 负结果 / 零分母。

### 仍不成立的主张（不能说）

- ❌ 条件技能已经在独立任务家族上证明增量：只做了 `paired_check` 骨架，
  没有 train/dev/validation/test 四组家族与真实数据集。
- ❌ 存在公平消融的因果结论：C/D 还差匹配、容量、状态过滤，未消融。
- ❌ 准确率 / 真实业务效果 / 防住注入 / 生产级：同前几轮，仍不成立。

### 本轮没做（如实标注）

- E02 的四组任务家族、真实 guard 与正常 guard、完整公平消融与回滚证据。
- E03 经验适用范围登记（工具契约 / 数据版本 / 权限 / 有效期 / 撤销）、
  结构过滤 + BM25、embedding 评估。
- 云端 LoRA、完整多 Agent、向量数据库、双后端迁移按计划暂缓。
- 未重跑 1080 run、未重训模型、未改冻结 manifest。

### 下一项验收

E02 在**独立任务家族**上证明增量：建 train / dev / validation / test 四组，
用相同任务与 seed 做前后配对，同时检查正常 guard 与安全 guard；
业务收益不能抵消未授权操作；没有稳定收益就拒绝候选、保留失败样本与解释，
并发布负结果。之后才是 E03 与公平消融。

---

## 2026-11-09（第七阶段）：Word 计划剩余任务（§5 / §10 / E02 / E03）

按 `EvoGuardLab_安全防护与下一阶段推进计划_2026-11-09.docx` 的 §5 / §9 / §10 / §12，
把剩余可本地完成的任务一次做完。

### 完成事项

**§5 先改权限结构 再补特征**（`ai-service/evoguard/tool_contract.py`、
`scripts/tool_contract_demo.py`、`docs/tool-contract.md`）
- 域名 / 资源 ID 格式 / 可写路径 / 最大金额 / 方法 / 副作用类型 / 审批请求 id
  由冻结契约结构化校验；未知身份 `enforce` 下拒绝；`security.scan()` 的命中
  只写进 `regex_signals`，**不改变 decision**。
- 两个反例：`rm -rf /` 字样但契约合规 → `allow`；普通回环 URL 无任何正则特征
  但域名/身份越界 → `deny`。同一组合约加不加危险字样，decision 不变。
- 独立模块，不改 `security.guard_tool_args` 默认行为，历史回归可比。

**§5 12 个工程冒烟场景**（`scripts/defense_demo.py`）
- 从 4 场景 / 8 case 扩到 **6 场景 / 12 case / 6 个独立攻击向量组**；
  新增「参数里的假凭据」「参数里的外带 URL」两个**新入口**，不是改措辞。
- 四模式对照产物已按 12 重算：off/audit 业务 12/12、攻击达成 5；
  enforce/enforce+wrap 业务 8/12、攻击达成 0、真实阻断 1.0、正常误阻断 0.0。
  下降的 4 条全部是攻击被拒，不是防护损失业务。
- `docs/mode-compare-protocol.md` 保留「会话 4 当时 8 条」的历史状态，
  另加「后续状态更新」避免文档互相矛盾。

**E02 独立家族增量**（`scripts/conditional_skill_eval.py`、`experiments/e02_v1/`）
- train / dev / validation / test 四组，task_id 与 seed 全程不重叠；
  同 task/seed 前后配对；正常 guard 零误阻断，安全 guard 拦越权候选。
- validation gain 0.167、test gain 0.25（增益只出现在规则表原本选错的任务上，
  不是全量提升）；`first` 选择器无收益 → REJECT 并发布负结果与失败样本；
  越权候选即使有业务收益也 REJECT。

**E03 经验适用范围**（`ai-service/evoguard/experience_scope.py`、
`scripts/experience_scope_demo.py`、`experiments/e03_v1/`）
- 记录工具契约版本 / 数据版本 / 权限范围 / 有效期 / 撤销状态 / 来源 run；
  **结构过滤先于 BM25**（纯标准库，中文 2-gram + ASCII 词，k1=1.5 / b=0.75）。
- 反例：文本与查询逐字一致、BM25 最高分但**已撤销**的记录不进候选；
  若忽略撤销标记它会排第一。相似度不能替代可信性。未加 embedding。

**§10 公平消融与统一登记**（`scripts/ablation_v1.py`、
`experiments/ablation_v1/`、`docs/ablation-protocol.md`）
- 六组消融 / 13 配置，每组由同一冻结基线加**一个** override（脚本有断言），
  净增益模型让验证门禁 / 匹配 / 容量三组都产生可观察差异。
- 八项业务与防护指标同时报告，逐 case 与分母保留，零分母 `null`；
  待审批不算业务完成；攻击样本的 business_success 表示「动作是否执行」不是业务正确。
- 内容隔离消融在固定 enforce 下攻击达成率不变，唯一差异是
  `content_isolated_cases`；条件技能消融只影响独立任务族。
- manifest 统一登记威胁模型、攻击/正常集指纹、独立授权策略、安全模式、包裹、
  自动审批、工具契约、输出落点、配置与代码指纹。

### 证据与数字

- 全量回归 **392 项全绿**（351 + 12 工具契约 + 17 E02/E03 + 11 消融 + 1 场景扩展）。
- 冻结 manifest 复核仍是「产物差异 0 处」，代码漂移 3 文件。
- 新产物：`experiments/tool_contract_v1/`、`experiments/e02_v1/`、
  `experiments/e03_v1/`、`experiments/ablation_v1/`，以及重算的
  `experiments/defense_demo_v2/`、`experiments/mode_compare_v2/`。

### 仍不成立的主张（不能说）

- ❌ 验证门禁的因果收益已证明：消融是确定性构造集，不是 1080 run 重跑。
- ❌ E02 增益代表真实模型/线上效果；不给置信区间，不靠重复调用扩样本。
- ❌ 12 个场景 = 置信保证；仍未扩到 30 攻击 + 30 正常。
- ❌ 准确率 / 防住注入 / 生产级 WAF / 完整沙箱 / 多 Agent 已验证。

### 本轮没做（如实标注）

- 真实模型上的 E02 四组家族验证与不确定性范围、embedding 评估、
  严格 2×2 消融、回滚证据、1080 run 重跑、30+30 扩样。
- 外部真实业务轨迹与独立标注者（Q03 实质）仍未到位。
- CI 策略直接读 `security_effects` 阻断未授权执行、阈值版本登记未做。
- 未重训模型、未改冻结 manifest。

### Word 计划的完成度

会话 1–7、S01–S05、Q01–Q05、E01–E03 骨架、§5 权限结构与 12 场景、
§10 六组消融与统一登记均已有代码与测试。**剩余的是需要外部数据或真实模型
重跑的实验类任务**（外部轨迹 ≥30、独立标注、1080 run 重跑、公平消融的
真实模型版本、阈值校准），这些在离线本地环境里不能靠改代码「做完」，
已逐项标注为未完成并写明前置条件。

---

## 2026-11-10：补上第二轮真实模型评测 + 发布准备

### 重跑 1080 run（两套库对照）

用当前代码（配置指纹 `8fe5db473fd7`，与 11-09 冻结**完全相同**）跑了两遍：

| 运行 | 技能库 C/D | 库指纹 | 单故障 D | 复合 D | 难度组 C |
|---|---|---|---|---|---|
| 11-09 冻结 | 127/11 | `d6dea8be…` | 79.9% | 53.3% | 75.0% |
| 11-10 第二轮库 | 127/11 | `5b6b8495…` | **79.9%** | **53.3%** | **75.0%** |
| 11-10 data 旧库 | 41/6 | `afac4ecf…` | 81.4% | 73.3% | 69.4% |

- 第二轮库与第一轮**指纹不同、结果逐项相同**：两套库在这批任务上行为等价，
  但不能说它们是同一份库。
- `data/` 旧库（41/6）结果明显不同：**库变了指标就变**，manifest 必须登记库指纹。
- 配置指纹三轮一致：本轮代码改动不在这条评测路径上；不能推广成「任何改动都不影响」。
- 产物：`experiments/real_model_20261110_r2/`（正式）、
  `experiments/real_model_20261110/`（旧库对照）。任务 #22 的「第二轮重测」已补上。

### 发布准备

- 加 **MIT LICENSE**、**GitHub Actions**（3.11 / 3.13 跑 393 项测试）。
- README 顶部加「这是什么 / 不是什么」；文末加「相关工作与引用」
  （AgentDojo / InjecAgent / ASB / WASP / OWASP / NIST / MITRE ATLAS / garak / PyRIT）。
- 新增 `docs/attack-datasets.md`：6 个公开基准的覆盖度、接入方式、许可（未核实的标「待核实」）、
  禁止事项与待办。**尚未接入**。
- 隐私清理：`docs/resume-bullets.md` 移出版本控制（保留本地）；
  `progress.md` 去掉「用户要求」类协作痕迹；`stage0-research.md` 去掉个人署名。
- 补**严格 2×2 消融**（审批策略 × 内容隔离，17 个配置），
  固定审批看隔离列、固定隔离看审批列，两个维度独立不抵消。

### 状态

- 全量回归 **393 项全绿**（392 + 2×2 新增 1 项）。
- 冻结 manifest（11-09）未被改写；11-10 的两份 manifest 是新增证据。

### 仍不成立

- ❌「当前代码逐字复现 11-09」：库指纹不同，只能说结果一致。
- ❌「外部攻击已覆盖」：只做了清单与接入方案，一个外部样本都没接。
- ❌「可发布即生产可用」：README 已写明不是 WAF / 沙箱 / 真实准确率。

## 2026-11-10 边界测试与入口硬化（第四轮）

正常路径的 393 项测试全绿，不代表边界分支没洞。这一轮专门打异常路径：
新增 6 个测试文件（88 项），过程中查出并修掉 6 个真问题。

### 新增测试

| 文件 | 项数 | 打的是什么 |
|---|---|---|
| `tests/test_edge_cases.py` | 34 | 非法状态迁移、空/缺字段、off 档不判定、账本与票据边界 |
| `tests/test_guard_fuzz.py` | 8 | 固定 seed 模糊（危险串 / 畸形串 / 嵌套 / 代理字符 / NaN），四档全跑 |
| `tests/test_untrusted_input.py` | 14 | 技能候选、CI 策略、契约参数这些「来源不可信」的输入 |
| `tests/test_reproducibility.py` | 8 | 同配置重跑逐字节一致；汇总数必须等于逐条之和 |
| `tests/test_safe_extract.py` | 11 | 第三方 zip 的路径穿越 / 盘符 / 反斜杠 / 类型 / 体积上限 |
| `tests/test_api_smoke.py` | 14 | 真起 HTTP 服务：接口可用、静态穿越挡住、安全响应头齐全、参数校验 |

模糊测试守的是三条铁律：六字段执行契约必须齐全、`decision` 只能是登记取值、
`executed=True` 必须对应真的改了正文的 effect（不许空口说拦住了）。

### 查出并修掉的问题

1. **`guard_peer_message` 不认 off 档** —— 其余四个守卫都先查 `enabled()`，只有
   它在 off 下照样查对端、照样扫消息。四档对比里 A2A 路径的「对照档」其实还在检测，
   等于少关了一半。已补齐，与其它守卫一致。
2. **待批票会越堆越多** —— 同一 run/工具/参数重试一次就新开一张 pending 票，
   人工面对 N 张同一个操作的票。改成复用同参数的待批票；权限/契约漂移时把旧票
   标 `expired`（写明原因）再重开，不复用。
3. **结算账本幂等键冲突被静默吞掉** —— `INSERT OR IGNORE` 让「同键不同金额/不同订单」
   的第二次提交直接返回「已结过」。金额类书证不能被静默覆盖：现抛
   `LedgerConflict`，并在同键同内容（浮点按 1e-6 比）时照旧只落一笔。
4. **`tool_contract` 的 off 档仍在判定** —— off 下结构化检查照跑、失败给 `report`，
   对照档不是真对照。改为 off 直接 `allow` + `detection=False`，并在 note 里写明
   「allow 只表示没拦，不代表合规」。
5. **金额上限能被 `-inf` 绕过** —— `float('-inf') <= 10000` 为真，负无穷金额过检。
   改为要求有限非负数（NaN / ±inf / 负数一律不合规），detail 文案同步。
6. **不可信输入的崩溃点** —— 技能候选不是对象时 `accept` 抛 `AttributeError`
   （`None.get`）而不是 `SkillRejected`；CI 策略里 `allow_verdicts: null` 抛
   `TypeError`；`dimension_coverage` 为 null 时 `evaluate` 崩溃。现在：
   技能非对象直接 `SkillRejected`；策略字段类型不对按更严一侧取值并**阻断**
   （新增 `policy_problems` 字段），缺维度覆盖信息也阻断而不是当没问题。
   另外技能声明写工具时，除策略 `write_tools` 外再核一遍冻结契约的副作用声明
   —— 漏配一个字段不该成为拿到写/外发权限的缺口。

### 顺带补的一条（接外部数据集前的必需项）

`scripts/safe_extract.py` 原来一条测试都没有，而它专门吃**别人给的** zip。
补测试时顺手修了跨平台漏洞：只按 `/` 切分判断 `..`，`..\evil.py` 这种
Windows 写法在 POSIX 上会被当成普通文件名；盘符路径 `C:/evil.py` 在 POSIX 上
同样会落成一个怪名字的目录。现在统一把反斜杠当分隔符、拒绝盘符，
判定逻辑单独抽成 `_suspicious()` 并直接测它。
（还验证了「声明大小可以撒谎」的实际行为：`zipfile` 以声明大小为读取上限，
所以不会因为头里写小就写出巨量内容。）

### 看板服务补冒烟测试

`ai-service/api/app.py` 是 README 让人 `python ai-service/api/app.py` 直接开的门面，
此前**一条测试都没有**。补了 14 项：在 127.0.0.1 随机端口真起服务（DB 与报告路径
指向临时目录，不碰仓库 `data/`），逐个打接口，并专门验两件事：

- `_static()` 的路径穿越：`/../ai-service/api/app.py`、`/..%2f..%2fREADME.md`、
  `/%2e%2e/%2e%2e/README.md`、`/../.git/config` 全部 404，且响应体里没有仓库内容。
- 每个响应都带 `nosniff` / `X-Frame-Options: DENY` / CSP / `no-store`，
  JSON 错误不会被浏览器当 HTML 解析。

顺带把 POST `/api/run` 的校验分支钉住：空体走默认值（前端「点一下就跑」的便利），
但非法 `agent_type` / `mode` / 故障类型、以及畸形 JSON 必须 400。

### 顺带修的可复现性

- `real_settlement_v1` 的产物里混着随机审批 id 与内核分配的回环端口，两次运行
  不可能一样。报告与 json 统一脱敏为 `appr_<masked>` / `127.0.0.1:<port>`，
  并在报告里写明「脱敏不影响任何结论」。现在这条链也纳入了「逐字节重跑一致」的测试。

### 冻结产物变化

- `experiments/mode_compare_v2/`：只有 `security.py` 的 code_fingerprint 变了，**指标逐项不变**。
- `experiments/tool_contract_v1/demo.json`：只有一条 detail 文案（金额规则说明）变了，判定不变。
- `experiments/real_settlement_v1/`：id/端口脱敏，5 个情形仍 5/5 通过。
- `experiments/defense_demo_v2/`、`experiments/ablation_v1/`：与提交版**逐字节相同**。

### 状态

- 全量回归 **482 项全绿**。
- 仍不成立：外部攻击样本一个都没接；CI 策略还没读 `security_effects` 表做独立核验。

## 2026-11-10 让检测能力自己长：外部语料 + 第二级模型 + 两段门禁

### 一、外部语料接进来了

`scripts/import_external_attacks.py` 按 **git blob sha** 取 InjecAgent 四个文件
（分支会动、sha 不会），逐文件写 sha256 与条数进清单；语料放 `data/external/`
（已忽略），仓库只留下载器、清单与报告。共 **2108 条**端到端用例
（data_stealing 1020 / direct_harm 1088）。

`scripts/eval_external_attacks.py` 把四个数分开报，不许混成一个「准确率」：

| 指标 | 值 |
|---|---|
| 注入特征命中（只算 `injection_*`） | 0.500 |
| 任意信号命中（含 PII/密钥/外带） | 0.846 |
| 干净对照误报 | 0.294（注入类误报 0.000，全是 `pii_email`/`pii_phone`） |
| 只扫攻击句子的命中 | **0.000** |
| base / enhanced 变体 | **0.000 / 1.000** |

结论：正则只认**字面**注入话术，对自然语言注入完全无感。

### 二、方法论上踩的三个坑（都写进了代码注释与测试）

1. **对照组用模板** → 正负样本差一层外层引号，字符模型学格式当捷径。
2. **只替换 attacker_instruction** → enhanced 变体真正注入的是更长一段，
   剩下的「ignore previous instructions」让对照组自己就命中规则（误报 50% 是这么来的）。
   现改成按模板前缀/后缀夹出**整段注入区间**替换；1984 条能构造严格对照，
   124 条锚点对不上整条剔除（剔除依据是「能不能构造」，与模型表现无关）。
3. **填充语只有一句** → 模型只要认那句话就能判干净。改成 40 句池子，
   训练用前 30 句、**测试用没见过的后 10 句**。

另外修了两个自己的 bug：计数器被布尔值覆盖（`d_fp += int(d_fp)`）、
全局按 hash 切三段导致 val/test 各自只剩一个类别（改成层内按比例分配）。

### 三、结果（`experiments/detector_evolution_v1*`）

| 划分 | test 仅正则 | test 正则+第二级 | test 误报 | 门禁 |
|---|---|---|---|---|
| `--by payload`（62 组） | 0.500 | **0.961** | 0.060 | 晋升通过、**发布暂缓**（>5%） |
| `--by tool`（16 组） | 0.500 | 1.000 | **0.333** | 晋升通过、**发布暂缓** |

耗时：正则 p50 0.04 ms / p95 0.08 ms；第二级 p50 1.02 ms / p95 1.55 ms，
且只在一级没命中时才跑。

**跨分布探针**：16 条正常用户请求，模型判为注入 **16 条**——它学的是
「像不像指令」，不是「是不是攻击者塞的」。所以 `deep_scan.applicable()`
只允许 `tool_output` / `peer_message` 两个落点，装到 `user_input` 上会把所有请求拦掉。

### 四、落地方式

`security.py` 加 `AGENTLAB_SECURITY_DEEP` + `AGENTLAB_DEEP_MODEL` 两个开关，
**默认关**；命中时加一条 `injection_learned`（warn，带分数与证据 n-gram）。
一级已命中就不叠第二级。默认行为完全不变（541 项回归守着）。

### 五、状态

- 全量回归 **541 项全绿**（新增 13 外部语料 + 10 CI 后果证据 + 36 第二级模型）。
- CI 也接上了 `security_effects`：`decision=deny/encode/wrap` 而 `executed=1` 一律阻断。
- 仍不成立：模型**未发布**（误报 6%/33%）；中文注入没样本；AgentDojo 未接。

## 2026-11-10 晚：启动本地模型、找到 HF 镜像、接 AgentDojo

### 本地 Ollama

- `ollama` 在 `F://ollama//ollama.exe`，模型在 `F://Qwen3`（`OLLAMA_MODELS` 已指向）。
  `ollama serve` 启动后 `qwen3:4b` 可用。
- 之前 11434 的 502 是 **HTTP 代理拦截**：`NO_PROXY=127.0.0.1,localhost` 绕过即可，
  不是服务没起。`check_model.py` 与模型输出都验证通过。
- `--followup real` 修了一个真 bug：provider 开了 JSON grammar，旧 prompt 却要
  自由文本，模型只能把输入复述成 JSON、启发式全读成 KEEP，还有 3 次 token repeat 500。
  改为显式 JSON + 结构化解析，解析不了标 parse_error 记 N/A。重跑 **8/8 observed，全 KEEP**。
  报告如实写「只有工具返回注入那组向量触发模型、共 8 条 case」。
- 产物：`experiments/defense_demo_real_20261110/`；`tests/test_real_followup.py` 11 项。

### HuggingFace 镜像与直接越狱语料

- `huggingface.co` 不可达，**`hf-mirror.com` 可达**。Gandalf 在 HF 上没有官方公开仓库，
  改用 `jackhhao/jailbreak-classification`（**Apache-2.0**）1998 条（666 越狱 / 1332 正常）；
  `TrustAIRLab/in-the-wild-jailbreak-prompts`（MIT）列为候选（parquet、内容高度重叠）。
- 入口 `guard_input`，召回/误报分开报：**越狱召回 0.041**、正常误报 0.004 —
  真实用户写的语义型越狱正则几乎抓不到，是重要负结果。
- 二级模型明确不适用于 `user_input`（16/16 正常请求被判注入）。
- 产物：`experiments/jailbreak_eval_v1/`；`tests/test_jailbreak_prompts.py` 10 项。

### AgentDojo 模板适配器

- 按 git blob sha 取 4 个攻击源码文件，**AST 静态解析**（不 import、不执行），
  抽出 10 个模板 × 12 个 InjecAgent 目标 = 120 条组合样本。
- 一级正则整体召回 **0.100**（只有带字面话术的 `injecagent` 模板被认出）；
  二级模型召回 1.000，但正常文本误报 **0.500**（干净工具输出 0.053 /
  正常用户请求 1.000）→ 召回 1.0 不等于泛化好，两数必须并列。
- 明确：不是 AgentDojo 官方端到端成绩；组合样本不算独立攻击向量。
- 产物：`experiments/agentdojo_eval_v1/`；`tests/test_agentdojo_adapter.py` 12 项。

### 状态

- 全量回归 **574 项全绿**。
- 仍未做：官方 AgentDojo 4 环境 benchmark、语义型攻击检测方案、中文注入样本，
  以及用户指定「最后做」的 30+30 扩样 / 每格≥3 任务 / 阈值版本登记 / 跨 Agent 迁移 / 多 seed。

## 2026-11-10 收尾：用户指定「最后搞」的五项，全部落地

### 1. 30+30 独立向量扩样（`scripts/expanded_vectors_v1.py`）

- 30 个攻击向量 + 30 条结构相似、意图合法的对照。独立性靠三条硬校验：
  `vector_id` 唯一、`(入口 × 手法)` 唯一、payload 两两字符 3-gram
  Jaccard < 0.85（实测最大 0.691，v11/v30）。重复 payload 直接判不独立。
- 检测层结果：**内容召回 0.467、内容误报 0.000、声称执行 0 次**。
  另有 14 条合法对照命中**配置类信号**（`tool_policy_missing` /
  `peer_not_allowed`）——按 Q01 三真值口径单列，不混进内容误报
  （第一版把它们混进去，误报虚高到 46.7%，已修）。
- 结论要如实说：30 个向量是工程冒烟的覆盖面，不是「所有攻击手段」；
  语义型攻击仍然靠不住（外部越狱语料召回 0.041）。

### 2. 每格 ≥3 条独立任务

- 102 个 `(agent × fault × difficulty)` 格子，每格 3 个不同 `task_id`，
  违规 0。任务全部取自既有 `sim.TASKS`，不改模板、不动历史 204 例口径：
  这是**任务矩阵定义的扩充**，不是把新数字混进冻结评测集。

### 3. 阈值版本登记

- `config/thresholds_v1.json`：版本、适用维度、20%/0% 阈值、开发集分布
  （从 `tests/fixtures/trace_*.json` 真实统计）、理由与「不在测试集上反复调阈值」声明。
- `assurance.load_thresholds()` / `thresholds_fingerprint()`；
  `build()` 输出新增 `thresholds.version / fingerprint / source`。
  配置缺失或非法时回退内置值并标 `source=builtin`，不静默换口径。
- **阈值本身仍是经验值，未标定** —— 登记的是版本与适用场景，不是「已标定」。

### 4. 跨 Agent 迁移

- 架构现状：`runtime._decide` 要求 `skill.task_type == agent_type`，
  order 的技能直接用到 kb/planner **不会被采用**（实测 `skill_applied` 全 False）。
- 模拟迁移（把 `task_type` 改成目标 agent）：成对用例增益为负/零、
  判定 **REJECT**。业务收益不能抵消负迁移。模拟不等于架构已支持。

### 5. 多 seed

- 5 个 seed（7/11/23/41/59），27 条 case/seed，成功率标准差 **0.0**、稳定用例 27/27。
- 如实写：仿真器的故障注入是确定性的（seed 只登记在 manifest、不改落点），
  方差为 0 是设计结果，**不能据此宣称真实模型也稳定**。

### 状态

- 全量回归 **591 项全绿**（新增 `tests/test_final_expansion.py` 17 项）。
- 冻结产物未改写；README 的「已完成 / 未完成」清单已按实情重排。
- 仍未做：真实模型上的 E02 / 多 seed、语义型攻击检测方案、阈值标定、
  官方 AgentDojo 4 环境 benchmark、独立标注者与 kappa、中文注入样本。

## 2026-11-10 最后一轮：语义检测 / 真实模型 / 跨语言 / 标注 / 标定 / 官方 harness

详见 `docs/final-round.md`，这里只留结论。

- **语义型检测换设计**：本地 qwen3:4b 第三级判定器（`semantic_guard.py`），
  218 条、216 次调用、3 条 N/A、p50 804.5ms。外部越狱召回 0.05 → **0.575**、误报 0；
  30 向量合并召回 0.8（合并误报 0.333）。**默认链建议一级+三级**，二级保持实验性。
  运行时开关 `AGENTLAB_SECURITY_SEMANTIC=1`，默认关；一级命中不叠第三级。
- **真实模型 E02 + 多 seed**：4 家族 × 3 任务 × 3 seed × 2 条件。
  dev 增益 -0.667（负迁移），validation/test +0.333 → **整体门禁 REJECT**；
  多 seed 标准差 0、12/12 稳定、N/A 3。严格 schema 修掉「复述输入 JSON 当作答」。
- **阈值标定**：120 开发快照 + 60 留出集，网格最优 0.15，但留出集上候选与默认
  0.20 的 F1 都是 1.0 → 合成集区分度不足，候选 `thresholds_v2_calibrated.json`
  **未启用**，默认不改写。
- **kappa**：A=规则 / B=语义（都不是真人），218 条 → kappa **0.325**、分歧 58。
- **中文样本**：30 条模型翻译生成（非真实语料），一级正则召回 **0.000**、
  三级语义 0.000–0.167 → 跨语言明确盲区。
- **官方 AgentDojo**：`--model LOCAL` + `LOCAL_LLM_PORT=11434` 接通 4 个 suite，
  各 1 任务 / `direct` 攻击；utility 0/0/0/100%、security 0/0/100/0%。
  **不是榜单成绩**（官方 97+629 任务、闭源前沿模型），价值在链路接通与产物可核对。

### 状态

- 全量回归 **615 项全绿**（新增 `tests/test_final_round.py` 24 项）。
- 仍未做：真人双标、真实中文攻击语料、自然语言间接注入检测、阈值用真实业务集标定、
  官方榜单全量（需官方模型 API）。

## 2026-11-10 融合 LlamaFirewall（读源码 → 改代码 → 验证）

来源 `E:/agent/learn/LlamaFirewall`（meta-llama/PurpleLlama，MIT，固定提交 f64eebd…）。
那份是轻量源码学习版：没装依赖、没下模型权重、没跑过示例，所以结论来自**读代码**，
不是复现上游效果。详见 `docs/llamafirewall-merge.md`。

### 学到的三个方法

- 按 Role 挂不同扫描器（Configuration）；
- register/create_scanner 可插拔 + scan_replay 逐条带历史扫描、BLOCK/人工复核即停；
- HiddenASCIIScanner（不可见 tag 字符）与 AlignmentCheck（用户目标 vs 当前动作）。

### 融进来的三块

1. `security.py:340-460`：补上一个**真实绕过**——关键词插一个不可见字符时，
   `_PRE` 前置子串和正则一起失效（改前 `plain -> injection_override`、
   `tag-char -> []`、`zero-width -> []`）。现在用「剥离视图 + tag 解码视图」双扫，
   并产出 hidden_tag_chars / hidden_zero_width / hidden_bidi_control 三条可复核发现。
   比上游多覆盖零宽与 bidi，而且是**先归一化再检测**（只告警不归一化等于绕过还在）。
   C 层正则快速路径让 10KB 扫描从 ~1ms 回到 **0.37ms**（预算 1.5ms）。
2. 新 `scan_pipeline.py`：Role / 三档决策 / 注册 / scan_replay / build_trace /
   `to_ci_action`（→ CI 的 pass/review/block）。**不照抄**上游「命中邮箱也 BLOCK」：
   按 severity 分级，info 不拦、warn 转人工复核、扫描器异常不静默放行。
3. `semantic_guard.py:173-215`：AlignmentCheck 本地版 `judge_alignment`，
   输出 observation/thought/reason，命中转人工复核而非 BLOCK；模型不可用时记 N/A
   并同样升级人工处置。`AlignmentScanner` 默认不挂（约 1s/次）。

### 上游缺点（写在文档里，没照抄）

默认配置根本没挂 HIDDEN_ASCII；RegexScanner 命中邮箱/电话就 BLOCK；
`scan()` 与 `scan_async()` 的多扫描器聚合逻辑不一致，sync 每条还新建事件循环。
PromptGuard / CodeShield / PIICheck 因依赖外部模型或引擎，不融合。

### 状态

- 全量回归 **638 项全绿**（新增 `tests/test_llamafirewall_merge.py` 23 项）。
- 历史指标不变：expanded_vectors_v1 / external_attacks_v1 / agentdojo_eval_v1
  重跑逐字节一致；jailbreak_eval_v1 召回/误报逐项一致。
- 顺手修了 `eval_jailbreak_prompts.py` 把 p50/p95 耗时写进产物导致不可复现的问题，
  并把该链路纳入 `test_reproducibility.py` 的逐字节比对。

