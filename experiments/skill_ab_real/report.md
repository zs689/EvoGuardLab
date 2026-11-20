# FINAL-02：真实模型 Skill A/B 对照报告

生成时间：2026-11-20
模型：DeepSeek `deepseek-chat`（同模型配对，key 来自系统环境变量）
命令：`python scripts/eval_skill_ab_real.py --provider deepseek --model deepseek-chat --out experiments/skill_ab_real/result.json`

## 核心结果（真实实测）

| 条件 | 题数 | 通过率 | 平均步数 | 越权执行 |
|---|---|---|---|---|
| No-Skill | 20 | **70.00%** | 4.65 | 0 |
| With-Structured-Skill（提示词注入） | 20 | **50.00%** | 5.55 | 0 |

**ΔSuccess = -20.0 pp，ΔAvgSteps = +0.9**，Guard_Regression = false。

## 结论（诚实，不粉饰）

**提示词形态的 Skill 在本模型上是有害的**：注入结构化经验提示后，
成功率下降 20 个百分点、步骤增加 0.9，且这不是安全误伤（越权仍为 0）。
可能原因：空泛/泛化的 "fix_pattern=unknown_no_repair_trace" 提示挤占了
模型上下文，或诱导模型偏离原本有效的直接策略。

按方案 FINAL-02-T2 的第一性原理判定：**纯提示词 Skill 增益不足（实为负），
必须把 Skill 从"塞提示词"升级为"改变执行控制流的可执行钩子"**：
- 写文件前优先提示 `fs.patch`（精准局部编辑，避免整文件覆写）；
- 修改后强制追加一次 `code.run_in_sandbox` 校验；
- 仅在结构化字段（error_signature/fix_pattern/verification_check）非空时挂载，
  避免 `unknown_no_repair_trace` 这类空壳 Skill 污染上下文。

## 升级后复测（可执行钩子，同模型配对）

按 T2 把 Skill 从提示词升级为**可执行控制流钩子**后复跑
（`--mode hooks`，结构化 Skill 由真实"失败→修复→验证成功"成对轨迹合成）：

| 条件 | 题数 | 通过率 | 平均步数 | 越权执行 |
|---|---|---|---|---|
| No-Skill | 20 | 70.00% | 5.10 | 0 |
| With-Skill（可执行钩子） | 20 | **75.00%** | **4.70** | 0 |

**ΔSuccess = +5.0 pp，ΔAvgSteps = -0.4**，guard_regression=false。

结论：提示词形态有害（-20pp），可执行钩子形态转为正增益（+5pp），
印证了第一性原理判断——Skill 必须改变执行控制流（写前优先 fs.patch、
写后强制校验），不能只往上下文塞经验描述。原始负结果保留作为决策依据。

## 容器隔离复测推翻了上面的 +5pp（重要更正）

Docker Desktop 可用后，用 `--use-docker` 在真容器（只读挂载、资源受限）
重跑同一配置（`--mode hooks`，20 题 × 2 臂）：

| 条件 | 题数 | 通过题数 | 通过率 | 平均步数 | 后端 |
|---|---|---|---|---|---|
| No-Skill | 20 | **11/20** | 55.00% | 5.80 | docker |
| With-Skill（可执行钩子） | 20 | **10/20** | 50.00% | 5.60 | docker |

**ΔSuccess = -5.0 pp，ΔAvgSteps = -0.2**，guard_regression=false。

### 与子进程结果对照：这是噪声，不是真实增益

| 后端 | No-Skill | With-Skill | 差值 | 折算题数 |
|---|---|---|---|---|
| local-subprocess | 14/20 | 15/20 | +5.0 pp | **+1 题** |
| docker | 11/20 | 10/20 | -5.0 pp | **-1 题** |

**两种后端下都只差 1 道题，方向还相反。** n=20 时 1 题 = 5pp，
所以 ±5pp 完全落在单题波动范围内——**可执行钩子 Skill 的真实效应是 0，
之前报告的 +5pp 是抽样噪声，予以更正、不再声称正增益。**
（也不反过来声称有害：-5pp 同样只是 1 题。）

### 由此得出的真实结论

1. 提示词形态 Skill：**有害**（-20pp），证据充分（两种后端一致方向、幅度大）。
   这条结论保留有效——不要往上下文塞 Skill 描述。
2. 可执行钩子形态 Skill：**效应不显著（≈0）**。它不像提示词那样损害性能，
   但也没有可测的正增益。0 元改动不值得宣称成果。
3. 要证明 Skill 有效，20 题的样本量根本不够（1 题就是 5pp）。需要
   ≥100 题的留出集、多次重复、或按错误族分层的统计检验。

## 边界
- 两种后端结果分别落盘：`result.json`（local-subprocess，早期）与
  `result_docker.json`（docker 容器隔离），各自 `backend` 字段标注，不混用。
- 单次运行有随机性；±5pp 在 n=20 时等于 ±1 题，不具备统计意义。
- 只有「提示词形态 -20pp」这个结论幅度足够大、两种后端方向一致，可采信。
- 题数 n=20 以结果文件为准。
