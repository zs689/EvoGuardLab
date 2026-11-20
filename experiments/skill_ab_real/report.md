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

## 边界
- backend 为 local-subprocess（Docker daemon 不可用），每条结果 `backend` 字段如实标注。
- 单次运行有随机性；-20pp 是本次实测，需多次重复确认稳定性。
- 题数 n=20 以结果文件为准。
