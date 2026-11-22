# 自主学习深耕与安全闭环 执行进度（2026-11-22 晚，第二轮）

对应方案：`EvoGuardLab_自主学习深耕与安全闭环方案_2026-11-22.json`

| Workstream | 状态 | 产物 |
|---|---|---|
| EVO-DEEP-01 规约冲突 + 微工具合成 | **完成** | `_slot_key`/`active_invariants`、`tool_synthesis.py`、2 个测试文件 |
| EVO-DEEP-02 在线学习闭环 | **完成** | `ReActAgent(auto_learn=)`、`tests/test_online_learning_loop.py` |
| DET-DEEP-01 越狱召回 + L2 限速 | **完成（两项验收均达成）** | `pub_jailbreak_layered.json`、`pub_bench_layered.json` |
| ARC-DEEP-01 状态封装后拆分 | **部分（拆了诊断，Ctx 回滚）** | `legacy_diagnose.py`、`runtime_common.py`；`runtime.py` 1320→1013 |

## DET-DEEP-01-T1：越狱召回 0.04 → 0.93（达标）

先发现**数据泄漏**：`train_light_classifier.py` 的训练集就包含
`jailbreak_classification/prompts.jsonl`。改为复用训练脚本自身的
`stratified_split`，只取 holdout（`train_overlap_excluded: true`）。

然后挖出**四个叠加的真 bug**，逐个修掉：

1. **训练/推理特征函数不一致**：推理侧 `_feats` 会额外加 `pinyin_tokens`，
   训练侧 `feats` 没有 —— 运行时多出一类"权重从未训练过"的特征，分数被
   系统性推高。已让训练侧同样加，并用 60 条样本逐条比对确认
   `predict == classify`（60/60 完全一致）。
2. **拼音短码子串误匹配**：`PINYIN_TO_CLUSTER` 里的 `sc`/`rg`/`qb` 用
   `in compact` 匹配，英文里 `score`/`description` 全都命中；改成短码按
   词边界匹配。这是英文误报的主因（正常英文文本 0.14 → 0.87）。
3. **模长分母漏项**：`_score_one` 只对 `w != 0` 的特征累加模长，而训练侧
   `vec()` 对全部词表累加 —— 分母偏小、分数虚高。改为 idf 覆盖全词表。
4. **切分参数没落盘**：权重文件只记 `split_seed`，不记 train/calib 比例，
   下游评测用默认比例会得到**另一套 holdout**（评测集落在训练集里，
   fpr 从 0.045 虚报成 0.46）。已在权重里写入 `split` 元数据并由评测读取。

最终：

| 口径 | recall | fpr |
|---|---|---|
| L1_only | 0.01 | 0.00 |
| **L1 + light_classifier** | **0.93** | **0.02** |

验收要求「recall 显著高于 0.04 且 fpr ≤ 0.05」——两项均达成。
（阈值在训练脚本的 calib 上校准，holdout 只用于报告，未参与调参。）

## DET-DEEP-01-T2：L2 全量跑通（达标）

**真正的卡点不在攻击集，在 utility 集**：`_utility()` 逐条串行调判官，
既无去重缓存也无并发——2108 行良性请求把评测拖到十几分钟不结束。
已重构为复用 `_eval_set` 的去重缓存 + 并发 + 令牌桶。

同时修了四处：
- 令牌桶 `--rps`（1.5~2 次/秒）+ 不可用时指数退避重试；
- 流式落盘（旧 `ex.map` 按序取结果，一条卡住全批不落盘）；
- `_load_cache` 不再永久缓存 `L2_unavailable`（一次限流被固化成了 136 条）；
- 判官「不确定」（`report`）与「服务不可用」分开计为 `L2_undecided` /
  `L2_unavailable` —— 混记会让"服务挂了"的指标永远不清零。

结果（全量，1 分钟级完成，此前 >30 分钟不结束）：

`injecagent_public n=2108 / uniq=62`，`asr_detector_only=0.0645`，
`L2_unavailable=0`，`L2_undecided=136`，utility pass_rate=1.0。

`L2_semantic=0` 是**真实结果**：规则漏网的 4 条唯一文本是「关闭两步验证」
「退出频道」这类正常业务请求，语义判官判"不确定"是正确的，不是 bug。

## ARC-DEEP-01：部分完成（1013 行，未达 550）

诊断块（`diagnose` / `_diagnose_impl` / `diagnose_by_invariants` /
`STRATEGY_BY_CAUSE` / `_record_skill_id`）已拆到 `legacy_diagnose.py`，
共用常量与异常收敛到 `runtime_common.py`。`runtime.py` 1320 → **1013 行**，
`test_core` 56 条全过。

**Ctx 拆分再次回滚**：拆出后 `test_composite_needs_chain` 回归——第二轮
恢复时 `cross_verify` 不再生效（第 14 步算出的 total 没被重算覆盖）。
对比轨迹定位到根因：诊断把累积的多轮重放判成 `resource_abuse`
（`_last_attempt` 的"只取最后一轮"语义在跨模块后失效），于是
`round_root_cause` 不在 `REPAIR_COVERAGE` 里，策略被"路过"而不修复。

结论：**行数不是拆分的理由**。要拆 Ctx，必须先把 `_last_attempt` 的
轮次语义做成显式状态（而不是靠逻辑步从 1 重新计数），这是一次独立重构，
不应夹在指标型任务里做。已如实记录，未提交回归。
