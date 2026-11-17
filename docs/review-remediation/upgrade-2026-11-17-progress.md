# 升级方案执行进度（2026-11-17）

> 依据 `E:/agent/EvoGuardLab_升级改造方案_2026-11-17.json` 严格推进。
> 每条按「做到哪 / 证据 / 未完成原因」如实登记，不删减、不假称完成。

## 总览

| WS | 优先级 | 状态 | 提交 |
|---|---|---|---|
| WS-01 发布前脱敏 | P0 | ✅ 完成（3/3） | a35b48a（重写历史后 hash 变化） |
| WS-02 真实多轮 ReAct | P0 | ✅ 完成（3/3） | 5b04df7 |
| WS-03 语义闭环 | P1 | ✅ 完成（3/3，实验**未过线**如实记录） | dcf77f6 |
| WS-04 自进化 | P1 | ⚠️ T1 完成，T2 未做 | 5867052 |
| WS-05 evidence-gate 整合 | P1 | ✅ 完成（2/2） | 49d56dd |
| WS-06 分布式底座 | P2 | ❌ 未做（需 Redis/PG） | — |

## 全局约束遵守情况

| 约束 | 遵守 |
|---|---|
| 核心 evoguard/ 零第三方运行时依赖 | ✅ 新模块只用标准库；ONNX/torch 都在独立 venv |
| 不放宽预注册门禁、不删失败用例 | ✅ v3 判定线 sha256 未变；FAIL 如实写 result.json；预注册测试改为校验指纹与诚实标记，保留「不许冒充独立复跑」 |
| 六字段契约、executed==0 才算拦住 | ✅ ReAct 引擎全程六字段；对抗测试判据是 executed；修了 executed=True + effect=call_stopped 自相矛盾的 bug |
| 每完成一个 WS 跑全量回归 | ✅ 每轮都跑 unittest + gate，见下方数字 |

## WS-01（完成）

- T1 路径脱敏：非归档目录 `git grep` 命中 0；
- T2 历史邮箱重写：139 个提交，`git log --format=%ae | sort -u` 无真实手机号；
  因仓库**无 remote**，重写安全；建了 `pre-push-hygiene-20261117` 标签可回退；
- T3 关掉 8 条 sqlite ResourceWarning：`unclosed database` 计数 0。

## WS-02（完成）

- T1 `autonomous_agent.py`：模型自己决定工具与参数，每步强制走 `execution.preflight`
  + `tool_rails`；max_steps + 死循环熔断；无 executor 时绝不编造结果。
- T2 `FORCED_SKILL` 改为 `Ctx.forced_skill` 实例状态；全局仅向后兼容；并发隔离测试。
- T3 20 条真实多轮攻击（间接注入/多轮漂移/返回投毒）：
  **脚本化 20/20、真 qwen3:4b 20/20、0 error、0 bypass**；
  正向任务 `kb.search` 可执行（证明不是一律 deny）。
- 过程中修了 3 个真 bug（契约源没接注册表导致假绿、effect 与 executed 矛盾、越界 IndexError），
  没靠放宽测试掩盖。

## WS-03（完成，但实验**未过线**）

- T1 v3 实验真跑：`result.json` + `report.md`。判定 **FAIL，如实记录**：
  `auc_drop_max=0.4761`（限 0.01）、`tail_gain_min=0.0417`（需 0.10）；其余 6 条达标。
  标 `independent_reproduction=false`——本机自跑**不构成 v3 通过依据**。
  冻结集 text 原是 synthetic 占位符，按冻结结构换成真实模板载荷并标注。
- T2 `semantic_route` 扫描器：L1 命中 BLOCK、灰区路由判官、判官不可用/超时转 review
  （第 7 条线 silent_pass=0）。
- T3 `fetch_pi_model.py` 下载+sha256 到项目 `.cache/models/pi`（gitignored）；
  判官不再依赖父目录；`.dockerignore` 排除后 build context 从 709 MiB 回到 2.5 MiB。

## WS-04（部分）

- T1 ✅ `trajectory_diagnose.py`：通用根因诊断，不认工具名；新领域工具也能诊断。
- T2 ❌ **未做**：从成功恢复轨迹合成结构化 Skill、A/B 验证晋升与回滚。
  现有 `skills.propose` / `conditional_skill.accept` 有雏形，但没接成方案要求的
  「失败→定位→合成→独立验证集提升→晋升/回滚」全流程。

## WS-05（完成）

- T1 六个模块迁入 `evoguard/evidence_gate/`；`llm.py` 不迁（统一 gateway）；
  wheel 登记；补了 `<w:outlineLvl>` 大纲识别。
- T2 `evidence_grounding` 输出侧扫描器：正确数字放行、错误数字 error、无引用 warn。
  修了「payload 整包转文本导致篡改数字被证据原文洗白」的真 bug。

## WS-06（未做）

需要 Docker Compose + PostgreSQL + Redis 环境，本机没有。
核心零依赖模式已验证不受影响，但**多副本压测、RLS 隔离、JWT/OIDC、
Webhook 审批回调均未实现**，不假称完成。

## 验收数字

最终：1035 tests OK / 29-29 gate / build-context PASS / wheel manifest PASS。

## 诚实边界

1. 提交顺序上 WS-05 早于 WS-04-T1，但两者依赖（WS-01/WS-02）都已满足，功能无影响。
2. v3 实验 FAIL 是**真实结果**，不是「没调好」；要过线需更强中文语义判官 + 独立复跑。
3. 真 Ollama 对抗 20/20 依赖本机 qwen3:4b；换模型需重测。
4. WS-04-T2 与 WS-06 未做，是把「诚实留白」优先于「假称完成」。
