# 下一轮核心突破执行进度（2026-11-22）

对应方案：`EvoGuardLab_下一轮核心突破方案_2026-11-22.json`

原则：NOT_RUN / 未完成 / 回滚的项一律如实标注，不伪造 DONE。

| Workstream | 状态 | 产物 |
|---|---|---|
| NEXT-01 多轮累积自进化 | **核心验收达标**（3 轮稳定） | `refine_skill`、`latest_snapshot`、`env_bench_next_r1..r3.json` |
| NEXT-02 计数 bug + L2 加速 | **T1 完成；T2 基础设施完成但全量实测未完成** | `test_eval_benchmark.py` 恒等式测试、并发/去重/缓存、`pub_bench_l2_cache.jsonl` |
| NEXT-03 公开越狱基准 | **完成** | `pub_jailbreak_layered.json`（source_url/sha256/recall/fpr/p95） |
| NEXT-04 runtime.py 拆分 | **回滚（行为回归）** | 无；runtime.py 保持 1320 行 |
| NEXT-05 第二模型 / Docker | **未执行（环境阻塞）** | — |

## NEXT-01：多轮累积自进化（P0，核心达标）

新增 `skill_synthesis.refine_skill(store, skill_id, new_traces)` 与
`latest_snapshot()`。关键设计：**必须从 skill_versions 的不可变快照恢复**
——`skills._get_skill()` 只返回表字段，会丢掉 env_invariants，这是多轮
累积原本无法工作的根因。每轮新规约过 evidence 门禁，按 (domain, rule)
去重，写入新的 version 快照，旧版本可回滚。单测 4 条覆盖 3 层嵌套规约
累积、快照不可变与回滚、重复规则拒收、无证据拒收。

三轮留出实测（真实 DeepSeek，训练集 20 / 留出 40，留出集关闭运行时自愈）：

| 轮次 | 总体 no→with | p | pagination no→with | p |
|---|---|---|---|---|
| r1 | 0.00 → 0.70 | <1e-5 | 0.00 → **0.80** | **0.0078** |
| r2 | 0.00 → 0.70 | <1e-5 | 0.00 → **0.80** | **0.0078** |
| r3 | 0.00 → 0.70 | <1e-5 | 0.00 → **0.80** | **0.0078** |

**达标**：pagination 从 0% 突破至 80%（验收线 ≥80%），McNemar p=0.0078
（验收线 <0.01），unit / timezone 各 100%，三轮完全一致无倒退。

**未达标（如实）**：
- 总体 70%，低于验收线 90%（coupling 在三轮中仍 0%）。
- coupling 的 rule 在三轮跑完后才修正为具体表述「REQUIRED 中设为 True
  （默认必填）」，单点验证已通过，但**未再跑完整三轮**，不能声称已达标。

修掉的三个真实 bug（否则多轮累积必然空转）：
1. `pub_bench` 尾页断言把 100 条数据的第 2 页当尾页 —— 纯题库错误；
2. 非法 cursor 抛 TypeError 未被捕获，提前终止收集式断言；
3. 训练集原本开着运行时自愈，模型靠反馈自修后不再暴露下一层规约。

## NEXT-02：计数 bug + L2 加速

**T1 完成**：`L2_unavailable` 样本原先既算 unavailable 又算 miss，
导致 `asr_detector_only=0.129`（正确 0.0645）。现在每个样本恰好落入
detector_hits 或 detector_miss 一个桶，加了恒等式断言与 2 条单测。
另外发现 `gate_miss` 循环二次调用判官（L2 调用翻倍），改为复用第一次结果。

**T2 基础设施完成**：sha256 文本去重缓存 + JSONL 断点续跑 +
`--workers` 线程池；单测 12 条全绿。

**未完成**：真 L2 全量跑三次均卡住（8 并发 19 分钟只落 62 条缓存，
其中 4 条 `L2_unavailable`；2 并发续跑仍卡）。判定为上游 API 限流，
已停止，**没有伪造完整 `pub_bench_layered.json`**。缓存保留可供续跑。

## NEXT-03：公开越狱基准（完成）

`scripts/eval_jailbreak_public.py` 使用本机已有的
`jailbreak_classification`（jackhhao，Apache-2.0，1998 条），100 攻击 +
100 良性，规则层直接越狱落点。结果：
`recall=0.04, fpr=0.0, p95_ms=0.514`，带 `source_url/license/sha256`。
报告明确标注**不是 JailbreakBench 100 Behaviors**（本机无该可复核数据，
按方案"绝不冒充"条款不使用）。

## NEXT-04：runtime.py 拆分（回滚）

机械拆出 Ctx 与诊断块后 runtime.py 降到 416 行，但
`test_core.TestChain.test_composite_needs_chain` 出现**行为回归**
（复合策略链失败）。定位为跨模块共享的可变全局状态语义难以无损迁移。
已 `git checkout` 回滚，`runtime.py` 保持 1320 行。宁可行数不达标，
也不为了让指标好看而破坏功能——不提交这个回归。

## NEXT-05：环境就绪项（未执行）

- 本机无第二家 API key、Ollama 未装，不伪造跨模型 DONE；
- `scripts/eval_cross_model.py` 在本仓库中不存在（方案中列的是期望路径），
  未凭空新建一套未验证的脚本；
- Docker daemon 仍关闭，`live_capability` 保持 10/11（1 SKIP）。

## 验证

1495 tests OK（skipped=11）、gate 32/33 pass 0 fail、build PASS。
