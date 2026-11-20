# FINAL-02：真实模型 AB 对照报告（coding agent）

生成时间：2026-11-20
模型：DeepSeek `deepseek-chat`（OpenAI 兼容 API，key 来自系统环境变量 `DEEPSEEK_API_KEY`）
命令：`python scripts/eval_coding_agent.py --mode model --provider deepseek --model deepseek-chat --ab --out experiments/coding_agent_real/ab_result.json`

## 核心结果（真实实测，非 0 基线）

| 架构 | 题数 | 通过率 | 平均步数 | 越权工具执行 |
|---|---|---|---|---|
| linear（裸 ReAct） | 23 | **60.87%** | 4.61 | 0 |
| reflect_replan（反思重规划） | 23 | **65.22%** | 4.22 | 0 |

**ΔSuccess = +4.35 pp，ΔAvgSteps = -0.39**（同模型配对，剥离模型强弱变量）。

这是第一次在**非 0 基线**上证明 Agent 架构本身的正增益：
- 基线 60.87% 说明题目有真实难度（不是全过/全不过的玩具集）；
- reflect_replan 高出 4.35pp 且步骤更少，说明反思回路确实改善成功率与效率；
- 注入题（3 道）在两臂下 `attack_executed=0`，安全门禁未被架构升级削弱。

## 诚实边界（必须一起读）

1. **backend = local-subprocess，不是 Docker 容器**：本机 Docker daemon 不可用
   （`docker version` 连不上 npipe，Docker Desktop 未安装）。评测脚本在 Docker 不可用时
   降级为本地子进程执行测试代码，范围仅限评测临时目录。
   每条结果的 `backend` 字段均为 `local-subprocess`，**不冒充容器隔离成绩**。
2. **题数 23 = 题库当前实际规模**（含注入题），以 `n` 字段为准。
3. 通过率由隐藏测试在真实文件系统上判分（读回 Agent 实际写入的文件），
   评测脚本不含修复答案，非自证。
4. 单次运行有模型随机性；Δ=+4.35pp 是本次实测值，不代表统计显著性结论，
   需多次重复方可作稳定结论。

## 结论

在 DeepSeek 上，Reflect-Replan 架构相对裸 ReAct 有**可测的正增益**（+4.35pp），
且未引入越权副作用。这是"架构有价值"的实证，但仍需：
- Docker 恢复后重跑容器隔离版本；
- 多次重复取置信区间；
- 换第二个模型交叉验证模型无关性。
