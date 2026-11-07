# EvoGuard Lab

**自进化 Agent 可靠性与自主恢复实验平台**
Self-Evolving Agent Reliability & Autonomous Recovery Laboratory

> 一句话：不只让 Agent 从错误里「学到经验」，还要用**独立测试**证明这条经验真的有用、且没有把原本能做的事弄坏。

---

## 现在能跑什么（真实状态，不是路线图）

```bash
cd EvoGuardLab

# 1) 完整闭环：故障 → 诊断 → 策略链恢复 → 提炼经验 → 独立验证 → 晋升 → 对照实验
python ai-service/evolution_bench.py

# 2) 回归测试（21 个）
python -m unittest tests.test_core -v

# 3) 启动 Web 看板（BoardUI 风格，零依赖）
python ai-service/api/app.py           # http://127.0.0.1:8770

# 4) 可选：检查本地模型是否就绪
python scripts/check_model.py --base http://127.0.0.1:11434/v1
```

不需要 pip 安装任何东西，Python 3.9+ 标准库即可跑（接本地模型才需要 Ollama）。
Web 看板是**原生 HTML/CSS/JS 单文件**，不引 CDN、不需要 Node 构建。

### 最近一次实测（mock 模型，2026-11-07）

**单故障测试集 204 例**

| 组 | 含义 | 成功率 |
|---|---|---|
| A | 无恢复 | 0.0% |
| B | 固定规则恢复（不诊断） | 26.5% |
| C | 用经验但**不严格验证** | 100.0% |
| D | 完整闭环（诊断 + 验证 + 晋升） | **100.0%** |
| — | 负迁移率（B 成功但 D 失败） | 0.0% |

**复合故障测试集 30 例**（一次运行注入两个落在不同逻辑步的持久故障）

| 组 | 含义 | 成功率 |
|---|---|---|
| A | 无恢复 | 0.0% |
| SINGLE | 从 5 个单策略里挑最优 | **0.0%** |
| CHAIN | 两步策略链 `fallback_tool → cross_verify` | **100.0%** |
| D | 完整闭环，自动查已晋升技能 | **100.0%** |

闭环产出：78 个训练用例 → 77 条经验 → 77 个候选技能 → 独立验证后晋升 7 个，
其中 **10 条经验是组合策略链**（不是单策略换个名字）。

### 三个必须讲清楚的解释

- **C = D = 100% 不代表「严格验证没用」**。规则表已覆盖全部单故障，技能在那上面增益为 0，
  所以 C/D 打平。治理价值靠 `tests/test_core.py` 的**对抗性投毒测试**正面证明：
  插入一个有害候选技能后 C 会失败、D 仍然成功。四组对照里看不出来的东西，用主动投毒来测。
- **策略链的增量不是调参调出来的**，是物理限制：诊断每轮只返回一个坏点，
  处理完重放第二个才暴露。修复动作受「本轮诊断根因」授权，
  否则单策略会顺手把沿途所有认识的故障全修了，实验结论会是假的。
- **`incorrect_planning` 从「诊断不出」变成「能诊断」**，靠的是新增的流程不变量
  `INV-PLAN-ORDER`（首步不得是收尾动作、末步不得是启动动作）。
  它只看通用动作词语义，不读任何任务的标准答案；纯执行型流程仍是已知盲区。

---

## 它解决的到底是什么问题

普通 Agent 踩坑后会「总结一条经验写进记忆」。问题是这条经验本身可能是错的：
一次工具超时之后，它可能得出「这类工具全都不能用」的结论，然后**永久变笨**。

EvoGuard 的答案是三道闸：

1. **经验准入**：必须带 `evidence_refs`（指向具体 step_id）+ 恢复成功 + 根因非 unknown，否则拒绝入库
2. **记忆治理**：未验证经验默认检索不到；被拒经验**永远**不出现在可信检索里
3. **晋升门禁**：候选技能要在**独立任务集**上测，同时检查「有没有把原本能成功的任务弄坏」
   —— 增益 ≥ 15pp 且负迁移 ≤ 5%，门槛固定在代码里，技能改不了

策略链把「查表」升级成「规划」：技能声明的是 `["fallback_tool","cross_verify"]`
这样的**有序多步**，恢复时每轮重新诊断、按新根因换策略，每步单独写审计记录。

---

## 目录

```
EvoGuardLab/
├── ai-service/
│   ├── evoguard/
│   │   ├── core.py          数据表 / 状态机 / 错误码 / 策略链归一化
│   │   ├── sim.py           模拟业务 + 工具 + 15 类故障 + 复合故障 + 真值
│   │   ├── agents.py        三个 Target Agent（订单 / 检索 / 规划）
│   │   ├── gateway.py       模型网关（mock / 本地 OpenAI 兼容）
│   │   ├── runtime.py       执行 / 检查点 / 分层诊断 / 策略链恢复 / 审批
│   │   ├── invariants.py    业务不变量（含流程首尾阶段约束）
│   │   ├── experience.py    经验提取 + 准入校验 + 记忆治理
│   │   ├── skills.py        候选技能 / 独立验证 / 晋升 / 回滚 / 负迁移
│   │   └── evaluate.py      A/B/C/D 四组对照 + 指标
│   ├── evolution_bench.py   闭环入口（产出 report.md + metrics.json）
│   └── api/app.py           REST API（标准库 http.server）
├── frontend/index.html      BoardUI 风格看板（双主题，零依赖）
├── tests/test_core.py       21 个测试（含对抗性投毒 / 策略链 / 不变量）
├── scripts/check_model.py   本地模型就绪探针
├── docs/
│   ├── stage0-research.md   10 个参考仓库的技术分析
│   └── taxonomy.md          15 类故障与 MAST/TRAIL 分类法映射
├── experiments/             evolution_report.md + metrics.json（自动生成）
└── data/                    SQLite（运行时生成，不入库）
```

### Web 看板的接口

| 接口 | 内容 |
|---|---|
| `GET /api/health` | 服务 + 数据库状态 |
| `GET /api/meta` | Agent / 故障 / 任务模板 / 策略覆盖表 |
| `GET /api/metrics` | 实验指标（前端唯一数字来源） |
| `GET /api/skills`、`/api/experiences` | 技能库 / 经验库 |
| `GET /api/runs`、`/api/runs/<id>` | 运行列表 / 完整轨迹（含诊断链、恢复链） |
| `GET /api/taxonomy`、`/api/report` | 分类法 / 实验报告 Markdown |
| `POST /api/run` | 真实跑一次（支持单故障与复合故障） |

前端**没有任何写死的数字**，全部来自上述接口；后端连不上会明确报错而不是显示空白。

### 关于 BoardUI

[BoardUI](https://www.boardui.com/zh-hans) 是面向智能体界面的 React 19 + Tailwind v4 设计系统
（103 个组件、19 种图表、400+ 设计变量，免费层 MIT）。本项目**没有安装它的运行时**，
而是按它的信息架构（侧边栏 / KPI 卡 / 数据表格 / 状态徽章 / 智能体进度 / 深浅双主题）
做了一套零依赖原生复刻。

- 优点：离线可跑、不破坏「零安装」底线、视觉有真实设计系统依据
- 缺点：不是官方 React 组件，没有 React Aria 无障碍能力；Pro 模板需付费，用不了
- 若后续愿意引入 Node，可把 `frontend/` 换成官方组件，REST 契约不用改

---

## 15 类故障

基线 8 类：`tool_timeout` / `tool_exception` / `invalid_json` / `missing_field` /
`silent_corruption` / `retrieval_failure` / `context_loss` / `process_interruption`

为测「学习」增设 2 类：`stale_data`（规则表无恢复策略）/ `incorrect_planning`（原诊断盲区）

按 MAST / TRAIL 分类法补 5 类：`format_violation` / `wrong_tool` /
`tool_hallucination` / `resource_abuse` / `premature_termination`

完整的分类映射、故意不覆盖的类别、已知盲区见 `docs/taxonomy.md`。
`resource_abuse` 是成本型故障，不用成功率衡量，已从成功率矩阵中移除。

---

## 接本地模型

```bash
# 1. Ollama 装到非 C 盘：OllamaSetup.exe /DIR="F:\ollama"
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

**为什么要先跑探针**：Qwen3 4B 在 Ollama 下默认开启思考模式，`content` 可能为空、
`reasoning` 占满 token（`E_LLM_THINKING`）。探针把 `max_tokens` 提到 1500，
在你白跑一轮之前把这件事查出来。**真实模型全量四组评测尚未完成**（204 例 × 4 模式，
单次调用 12–20 秒，全量约 45 分钟以上）。

---

## 已知限制（别当成能力）

1. **模型默认是 mock**：本报告衡量的是**机制自洽性**，不是模型能力。切真实模型后 B/C/D 都会掉。
2. **故障是构造出来的**，不是真实事故里自然共现的错误；复合故障证明的是「机制上限」，
   不是「真实分布下的收益」。
3. **策略空间仍是固定词表**（6 种策略的组合），不是开放式规划；技能是声明式映射，不生成代码。
4. **真实工具链未接入**：仍是内存模拟工具，没有真文件读写 / HTTP 超时 / DB 错误 / 限流。
5. **流程不变量有盲区**：纯执行型流程（步骤里没有启动/收尾动作词）检测不到顺序颠倒。
6. **RAG 仍是 SQL 过滤**，没有 embedding / 向量检索 / rerank。
7. **负迁移率 0% 证据仍弱**：技能只有 7 个，只能说「本实验未观察到」，
   不能说「不会发生负迁移」。
8. FastAPI / PostgreSQL / Vue / Java / Docker / OpenTelemetry 一律未做。

---

## 下一步（按对「落地」的贡献排序）

- [ ] 接**真实工具**：真文件读写 + 真 HTTP 超时/错误，验证故障在真实链路上还能不能被抓到
- [ ] 真实模型全量四组评测（含模型版本 / Prompt 版本 / 数据版本记录）
- [ ] 负迁移压力测试：批量投毒，测晋升门槛在不同投毒比例下的召回/误报
- [ ] 策略组合规划：从固定词表组合，升级为按轨迹状态动态生成组合
- [ ] 跨 Agent 迁移实验（H3：迁移距离越远收益越低，可能被证伪）
