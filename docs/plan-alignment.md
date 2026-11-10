# 计划文档对齐表（对应 `EvoGuardLab_下一阶段详细推进计划_2026-11-08.docx`）

> **注记（2026-11-10）**：这份对齐表只对 **11-08 旧计划**有效。
> 当前执行依据已换成 `EvoGuardLab_安全防护与下一阶段推进计划_2026-11-09.docx`，
> 实际完成情况见 [docs/progress.md](progress.md)。本文件保留作历史对照，不改写。

Word 原件不改。这份文档只做一件事：标出计划里**已经过时**的地方，
以及每一条对应的现状、证据和还差什么。

对齐时间：2026-11-08　｜　对齐时代码 HEAD：见 `git log -1`
（P0 01–06 全部落地 + 第 9 章退出码与覆盖率补口，全量测试 183 个）

---

## 分叉一：第 3 / 15 / 17 章的交接指针停在 P0 01/02

计划正文是在 P0 01/02 尚未提交时写的，所以第 3 章「现状」、第 15 章「下一阶段起点」、
第 17 章「工作记录」都把成对评估与空集判定列为待办。实际状态已经往后推了四项。

| 计划里的说法 | 现在的状态 | 证据 |
|---|---|---|
| P0 01 成对负迁移指标未做 | 已完成（`paired-v2`，逐 case/seed 成对，负迁移率 = 前成功且后失败 / 前成功，空分母为 `null`） | `ai-service/evoguard/skills.py::evaluate`；`docs/validation-protocol.md`；commit `d601d8c` |
| P0 02 样本充分性 / 晋升门禁未做 | 已完成（门禁 `promotion-gate-v1`：候选正文指纹、验证集指纹、协议、24h 有效期、独立任务数、从 pairs 重算） | commit `382223f`；`experiments/p0_03_20261108/promotion_gate_report.json` |
| P0 03 晋升校验未做 | 已完成，且修掉真实绕过点：`real_model_train.build_library` 以前直接 `UPDATE skills SET status='active'` 伪造「验证通过」，现在必须走 `promote(evaluation_id=...)` | commit `382223f`；`tests/test_skill_validation.py::TestStrictLibraryPipeline` |
| P0 04 完整回滚未做 | 已完成（不可变快照 + `active_version` 指针，回滚还原 strategy/scope/safety/decision_params，`disable` 与 `rollback` 分离） | commit `ba92fa2`；`tests/test_core.py::TestFullRollback` |
| P0 05 经验溯源未做 | 已完成（数据库级核实 source_run_id / step_id / 恢复记录关联；环境元数据从轨迹复制；`fault_scope` 落库） | commit `a97765d`；`experiments/p0_05_20261108/provenance_report.json` |
| P0 06 冻结基线未做 | 已完成（manifest + 库内容指纹 + 防混跑 + 分母自洽 + 报告单一来源） | `ai-service/evoguard/baseline.py`；`experiments/p0_06_20261108/` |

**所以下一阶段的真实起点是 P0 07（若继续编号）**：计划里没有列出的新问题，
而不是回头重做 P0 01/02。

---

## 分叉二：第 9 章把「CLI + JSON/Markdown 导出」写成未来工作

Word 第 9 章「使用流程与发布条件」原文：

> 先做单命令 CLI 和 JSON、Markdown 导出，再增加文件上传页面。CLI 的退出码区分完成、发现超过阈值的问题、输入解析失败；未知格式要明确报错。

这一条**已经交付**，不该再当待办。

| 计划要求 | 现状 |
|---|---|
| 单命令 CLI | `cd ai-service` 后 `python -m evoguard_inspect.cli --trace X.json`（轨迹检查）与 `--agent DIR --split-dir`（静态扫描，每个一级子目录算一个 Agent）共用同一入口 |
| JSON 导出 | `--out dir/` 写 `inspect.json`（轨迹）/ `static.json`（静态） |
| Markdown 导出 | 同目录 `inspect.md` / `static.md`，含分组总览、误报说明、排查顺序 |
| 未知格式明确报错 | `loaders/__init__.py` 认不出格式直接抛错，不返回空结果；CLI 返回 2 |
| 规则可列举 | `--list-rules`（25 条轨迹规则）/ `--list-static-rules`（20 条静态规则） |
| 坏输入不被静默忽略 | `tests/test_inspect.py` 覆盖乱序 span、缺字段、长结果、部分失败文件 |
| 退出码区分「完成 / 超阈值 / 解析失败」 | 已补齐：0 完成、2 解析失败、3 达到 `--fail-on error|warn|info`；`--strict-input` 让部分失败也返回 2 |
| 规则适用性统计（第 9 章「统一报告与覆盖率」） | 已补齐：轨迹侧与静态侧都有「规则适用性」一节（选中 / 可执行 / 跳过与原因、输入统计）；零命中但有跳过时判「覆盖有限」而不是「通过」 |

第 9 章真正没做的只剩一块，仍按计划保留为待办：

**小型人工标注集**（计划要求 ≥30 条可分享轨迹，覆盖正常 / 已恢复 / 未恢复）。
现在只有合成夹具与 9 框架静态扫描，**没有人工标签**，所以只有覆盖率，没有准确率 / 误报率 / 漏报率。

---

## 分叉三：P2 验收链断在「技能库怎么传给评测入口」

原断点：训练入口把产物写在各自输出目录（`experiments/validation_runs/train_*/library_d.db`），
而评测入口 `real_model_bench.py` 只认 `data/library_c.db` / `data/library_d.db`。
结果就是**新训练的库接不进旧评测**，只能手工拷贝文件 —— 一拷贝就说不清报告用的是哪一份库。

已做的收口：

- `real_model_bench.py --library-dir <训练输出目录>`：显式指定评测用哪一份 C/D 库，
  目录不存在直接报错，不静默回落到 `data/`。
- 评测开始时算 `library_hash`（按技能正文内容，不哈希 SQLite 字节），
  写进每条 `runs.jsonl` 记录与 `metrics.json`；结束时不覆盖式地冻结 `manifest.json`。
- 续跑前用 `check_resume` 核对：别的配置 / 别的技能库 → 拒绝（退出码 3）；
  旧版无指纹记录 → 默认也拒绝，`--allow-legacy-resume` 才放行并标 `mixed_baseline`。
- 训练入口本来就把 C/D 库写在 `--output-dir` 里，两个入口现在能直接对接：

  ```bash
  python ai-service/real_model_train.py --output-dir experiments/train_A
  python ai-service/real_model_bench.py --library-dir experiments/train_A
  python ai-service/evoguard/baseline.py check --manifest experiments/real_model/manifest.json
  ```

仍未做的：

- `experiments/real_model/` 的历史报告产生在该机制之前，记录里没有 `config_hash` / `library_hash`，
  只能登记成「历史基线 + 代码漂移」，不能与新冻结产物混算。要拿到可混算的真实模型数字，
  需要重跑一次全量评测（约 936 run）。
- P2 本身的科学目标（让技能表达 calc 规则顺序、xref 版本选择等条件决策）**没有进展**，
  当前技能仍是固定策略词表 + 组合链，难度组加候选后分数没有提高这一点也未改变。

---

## 一句话结论

计划里「P0 01/02 待办」和「CLI/导出待办」两处已经过时，第 9 章的退出码与覆盖率也已补齐，
P2 的断点已经用 `--library-dir` + manifest 接上。真正还没做的只剩：
人工标注集与准确率、静态侧覆盖率口径、重跑一次真实模型全量评测，
以及 P2 让技能表达条件决策。
