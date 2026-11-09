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


