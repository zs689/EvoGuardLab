# EvoGuard Lab

**自进化 Agent 可靠性与自主恢复实验平台**
Self-Evolving Agent Reliability & Autonomous Recovery Laboratory

> 一句话：不只让 Agent 从错误里"学到经验"，还要**用独立测试证明这条经验真的有用、且没有把原本能做的事弄坏**。

---

## 现在能跑什么（真实状态，不是路线图）

```bash
cd EvoGuardLab
python ai-service/evolution_bench.py     # 完整闭环：失败 → 诊断 → 恢复 → 提炼经验 → 生成技能 → 独立验证 → 晋升 → 四组对照
python -m unittest tests.test_core -v    # 13 个测试
python scripts/check_model.py            # 检查本地模型是否可用（可选）
```

不需要 pip 安装任何东西，Python 3.9+ 标准库即可跑（接本地模型时才需要 Ollama）。

### 最近一次实测（mock 模型，2026-11-07，数据集 192 例 / 测试集 144 例）

| 组 | 含义 | 任务成功率 |
|---|---|---|
| A | 无恢复 | 0.0% |
| B | 诊断+规则恢复，无经验学习 | 37.5% |
| C | 用经验但**不严格验证** | 58.3% |
| D | 完整闭环（验证+晋升） | **95.8%** |
| — | 负迁移率（B 成功但 D 失败） | 0.0% |

训练集 48 例（每类 Agent 前 2 个任务），测试集 144 例（其余 6 个任务），**任务模板不跨集合**。

三个可以解释的现象：
- D 剩下的 4.2%（6/144）是 `incorrect_planning`：**根因诊断不出来 → 学不到技能 → 也救不回来**。学习的天花板由诊断能力决定。
- **C 比 B 高 20.8 个百分点**：C 用了未严格验证的候选技能，恰好那批策略有效；**D 比 C 再高 37.5 个百分点，主要来自"严格验证挡住了坏经验"**，而不是"学到了很多新技能"（只晋升了 1 个）。
- 晋升只有 1 个：规则表已覆盖的故障，学一条同样的技能增益为 0，按门槛被正确拒绝。

> 百分比与用例数无关（144 例和 48 例给出同样的 37.5/58.3/95.8），因为行为由**故障类型**驱动、不由任务驱动 —— 这本身说明规则表缺乏任务级泛化。

---

## 它解决的到底是什么问题

普通 Agent 踩坑后会「总结一条经验写进记忆」。问题是这条经验本身可能是错的：
一次工具超时之后，它可能得出"这类工具全都不能用"的结论，然后**永久变笨**。

EvoGuard 的答案是三道闸：

1. **经验准入**：必须带 `evidence_refs`（指向具体 step_id）+ 恢复必须成功 + 根因不能是 unknown，否则直接拒绝入库
2. **记忆治理**：未验证的经验默认检索不到；被拒的经验**永远**不出现在可信检索里
3. **晋升门禁**：候选技能要在**独立任务集**上测，同时必须检查「有没有把原本能成功的任务弄坏」——负迁移超过 5% 一票否决

---

## 目录

```
EvoGuardLab/
├── ai-service/
│   ├── evoguard/
│   │   ├── core.py          数据表 / 状态机 / 错误码
│   │   ├── sim.py           模拟业务 + 工具 + 12 类故障注入 + 真值
│   │   ├── agents.py        三个 Target Agent（订单 / 检索 / 规划）
│   │   ├── gateway.py       模型网关（mock / 本地 OpenAI 兼容）
│   │   ├── runtime.py       执行 / 检查点 / 诊断 / 恢复 / 审批
│   │   ├── invariants.py    业务不变量（独立于诊断规则的第二层）
│   │   ├── experience.py    经验提取 + 准入校验 + 记忆治理
│   │   ├── skills.py        候选技能 / 独立验证 / 晋升 / 回滚 / 负迁移
│   │   └── evaluate.py      A/B/C/D 四组对照 + 指标
│   └── evolution_bench.py   闭环演示入口
├── tests/test_core.py       13 个测试
├── scripts/check_model.py   本地模型就绪探针
├── docs/stage0-research.md  10 个参考仓库的技术分析（阶段 0 交付）
├── experiments/             实验报告输出
├── datasets/ infra/ frontend/ backend/   # 预留，尚未实现
└── data/                    SQLite（运行时生成，不入库）
```

---

## 12 类故障（前 8 类是基线，后 2 类专门用来测"学习"）

| # | 类型 | 能否被规则诊断 | 备注 |
|---|---|---|---|
| 1-8 | timeout / exception / invalid_json / missing_field / **silent_corruption** / retrieval_failure / context_loss / interruption | 能 | silent_corruption 只能靠交叉验算 |
| 9 | stale_data（返回旧版本） | 能（靠版本号可观测） | 规则表里**没有**恢复策略 → 只能靠学 |
| 10 | incorrect_planning（顺序颠倒） | **不能** | 已知边界，直接转人工 |

第 9 类是整个实验的关键：**只有"诊断得出但规则救不了"的故障，才能测出学习的增量价值。**

---

## 接本地模型

```bash
# 1. 装 Ollama（装到非 C 盘：OllamaSetup.exe /DIR="F:\ollama"）
# 2. 模型目录指到 F 盘（用户环境变量，新终端生效）
setx OLLAMA_MODELS "F:\Qwen3"
# 3. 拉模型
ollama pull qwen3:4b
# 4. 探针：确认它真能按格式输出 JSON
python scripts/check_model.py --base http://127.0.0.1:11434/v1
# 5. 切到真实模型重跑
set AGENTLAB_LLM=local
set AGENTLAB_LLM_BASE=http://127.0.0.1:11434/v1
python ai-service/evolution_bench.py
```

**为什么要先跑探针**：3B~4B 的小模型经常输出"好的，以下是 JSON："这种前缀，接进评测只会得到一堆 `bad_model_output`。探针在你白跑一轮之前把这件事查出来。

---

## 已知限制（别当成能力）

1. **模型默认是 mock**，默认数字衡量的是**机制自洽**，不是模型能力。切真实模型后 B/C/D 都会掉，那才是真实水平。
2. **只有 48~72 个用例**，训练/测试按 task_id 切，样本量不足以做统计结论。
3. **incorrect_planning 无法诊断**，属于已知边界。
4. **技能是声明式策略映射**，不生成可执行代码（所以也没有沙箱执行风险）。
5. **FastAPI / PostgreSQL / Vue / Java 一律未做**——它们不影响"能不能证明进化有效"这个核心问题。

---

## 下一步

- [ ] 接真实模型重跑四组，看 B/C/D 的真实差距（预期全部下降）
- [ ] 用例扩到 200+，加 dev/test 划分与数据版本记录
- [ ] 补 `incorrect_planning` 的诊断（需要业务顺序约束，不能拿真值当规则）
- [ ] RAG 记忆（当前检索是 SQL 过滤，还没有向量）
- [ ] FastAPI + Web 轨迹回放
- [ ] 跨 Agent 迁移实验（研究假设 H3：迁移距离越远收益越低，可能被证伪）
