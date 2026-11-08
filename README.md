# EvoGuard

**自进化 Agent 可靠性与自主恢复系统**
Self-Evolving Agent Reliability & Autonomous Recovery

> 一句话：不只让 Agent 从错误里「学到经验」，还要用**独立测试**证明这条经验真的有用、且没有把原本能做的事弄坏。

---

## 现在能跑什么（真实状态，不是路线图）

```bash
cd EvoGuardLab

# 1) 完整闭环：故障 → 诊断 → 策略链恢复 → 提炼经验 → 独立验证 → 晋升 → 对照实验
python ai-service/evolution_bench.py

# 2) 回归测试（52 个：24 核心 + 9 评测口径 + 19 难度组）
python -m unittest tests.test_core tests.test_judgeability tests.test_hard_tasks -v

# 3) 启动 Web 看板（BoardUI 风格，零依赖）
python ai-service/api/app.py           # http://127.0.0.1:8770

# 4) 可选：检查本地模型是否就绪
python scripts/check_model.py --base http://127.0.0.1:11434/v1

# 5) 真实模型全量评测（936 run，约 13 分钟，可断点续跑）
python ai-service/real_model_bench.py
python ai-service/real_model_attribution.py   # 归因：C/D 命中了哪条技能
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

### 真实模型实测（Qwen3 4B / Ollama，2026-11-07）

真实模型**自己学一遍**（dev 探索 39 例 → 38 条经验 → 38 个候选技能），
然后训两套库：严格验证只放行 6 条（D 用），不验证全放行 38 条（C 用）。
评测 936 个 run（204 单故障 + 30 复合故障，各 × A/B/C/D），约 11 分钟，936 次模型调用；
再加难度组 144 run（36 例 × 4 模式），单独落盘。

| 测试集 | A 无恢复 | B 固定规则 | C 用经验不严格验证 | D 完整闭环 |
|---|---|---|---|---|
| 单故障 204 例 | 0.0% | 26.5% | 100.0% | **97.1%** |
| 复合故障 30 例 | 0.0% | 0.0% | **0.0%** | **100.0%** |

> **成功率口径**：只有**框架异常**（run 根本没跑完）才判「不可评」、剔出分母。
> 模型报错（`E_LLM_*`）**不剔** —— 只要 run 跑完、有答案，就计失败。
> 这条是查出来的：本轮 15 条失败伴随模型报错，逐条看轨迹发现它们全都有答案，
> 失败原因在注入的故障上（如 `incorrect_planning` 让工具返回倒序计划、规则表又无策略 → 转人工），
> 模型那步报不报错结果都一样。把这类剔出分母，等于用「服务抖了一下」洗白真实失败。
> 伴随报错的失败条数单独记在 `failures_with_llm_error`（D 为 3 条），不动分母；
> `bad_model_output` 一律计分。老口径保留在 `rate_raw`，可对照。本轮可评率 **100%**。

三条读法（完整解读见 `experiments/real_model/analysis.md`）：

- **治理第一次在四组对照里显形，而且是方向相反的两组差距**：复合故障上 D 100% / C 0%，
  单故障上 C 100% / D 98.5%。C 的 0% 不是模型不行 —— 它库里 38 条垃圾技能把两条有效的链
  挤到了永远轮不到的位置（第 1 轮恢复优先用单步技能，链长 1 就结束，第二个坏点没机会处理）。
- **D 少的 2.9pp 是能力缺口，不是治理出错**：6 条失败全在 `planner/incorrect_planning`，
  真实模型下 `cross_verify` 在验证集上 `before=0 / after=0`，严格验证拒了它，
  规则表也没有这条策略，只能转人工。其中 3 条恰好伴随模型报错，但**不构成不可评**——
  轨迹显示它们有答案、且计划本身就是倒序的，模型答不答都一样。
  mock 库里这条是有的 —— 说明 mock 的成功有虚高。
- **技能必须有作用域**：复合学到的链不能挂到单故障的键上，否则单故障用例会被套上多出来的一步
  （实测打挂 6 个用例）。现在技能带 `scope`，第 1 轮优先单故障技能、第 2 轮起优先多故障技能。

### 难度组：第一次真正测到模型（算术交给工具，规则顺序交给模型）

上面 204 例是**抽取式**任务 —— 工具把答案算好了，模型只要搬运，所以 936 次调用 0 次答错。
那测的是恢复机制，不是模型。所以另开一组 `--suite hard`：

- `xref`：同一主题返回**三份不同版本**的文档，真值只认版本号最高那份；不比版本就会答成旧条款。
- `calc`：**算术交给工具，规则顺序交给模型**。`calc.apply_rules` 按模型给出的顺序执行
  「满减 / 会员折扣」两条规则 —— 顺序不同结果不同（4020 先满减是 3724，先折扣是 3719）。
  这一条是改出来的：第一版让模型自己算，基线只有 33.3%，三题错法各异（抄小计 / 顺序反 / 误用门槛）；
  加"不得复制小计"的约束后仍然没用。**4B 模型不做多步算术，教不动**，所以把考点从「会算数」
  挪到「会编排规则」——那才是真实 Agent 的活。

| 指标 | 结果 |
|---|---|
| 纯能力基线（无故障，24 run） | **83.3%**（6 题对 5 题） |
| A 无恢复 | 22.2% |
| B 固定规则 | 44.4% |
| C 用经验不严格验证 | 69.4% |
| D 完整闭环 | **69.4%** |

三条读法：

- **基线就是天花板**：无故障时 83.3%，一出故障掉到 22.2%，恢复拉回 69.4%（基线的 83%）。
  这个分解比 97.1% 有信息量 —— 它把「模型会不会」和「机制救不救得回来」分开了。
- **两个 0% 是真结论，不是机制坏了**：`silent_corruption`（折扣率被篡改）和
  `stale_data`（只返回最低版本）都是**单条轨迹内没有交叉证据**的场景 ——
  Agent 手里只有一份被污染/过期的数据，任何恢复策略都没有第二份证据可比对。
  这和 README「已知限制」里 `silent_corruption` 对 KB 不成立是同一条边界。
- **这组不测 `format_violation`**：它的语义是「内容本来对、只是输出成散文」，
  而这一组模型要自己作决策，Agent 手里没有正确内容，fallback 只能是空结构 →
  `reformat` 恢复出来仍是空，必然失败。测一个语义不成立的故障只会得出假结论。

**扩技能到 `calc`/`xref` 之后（2026-11-08 补）：分数没变，但原因查清了。**

为了让这两个 agent 也有技能可用，给难度组加了一组**专用训练任务**（`T4001/T4002`、
换货期限、运费规则），与评测题完全不相交。训练后放宽库 38 → **41 条**（多了 3 条 calc），
严格库仍是 6 条 —— 因为那 3 条在验证集上 `gain = 0`（规则表已经能处理），全被拒了。

难度组四组一分没动（22.2 / 44.4 / 69.4 / 69.4），但路径不同：

| 故障 | C 用什么 | D 用什么 | 结果 |
|---|---|---|---|
| tool_timeout / tool_exception | `replay_checkpoint` | `retry`（规则表） | 都成功 |
| silent_corruption | `replay_checkpoint` | `human_takeover` | 都失败 |

C 的库里 calc 只有 3 条，最新那条是 `replay_checkpoint`，而 C 只按 agent 匹配 →
对所有 calc 故障都套它，恰好对瞬时故障也有效，于是歪打正着打平。

**本质**：规则表已经覆盖的故障，技能再加也只是规则表的复制品，加多少条都不改结果。
这跟 204 例上 C = D = 100% 是同一个机制，只是换了靶场。
要真拉开差距，得让技能能表达规则表**没有**的东西（比如 calc 的规则顺序偏好、
xref 的「比版本号而不是比命中位置」）—— 那是下一步，也是「自进化」真正的落点。

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
│   ├── real_model_train.py  真实模型自举训练（严格库 / 放宽库，test 不参与）
│   ├── real_model_bench.py  真实模型全量评测（断点续跑 / grammar 对照 / 指纹）
│   ├── real_model_attribution.py  归因：C/D 命中的是哪条技能
│   └── api/app.py           REST API（标准库 http.server）
├── frontend/index.html      BoardUI 风格看板（双主题，零依赖）
├── tests/
│   ├── test_core.py         24 个测试（对抗性投毒 / 策略链 / 不变量 / 技能定序与作用域）
│   ├── test_judgeability.py 9 个测试（可评口径：业务失败 vs 服务抖动）
│   └── test_hard_tasks.py   19 个测试（真值可独立算 / 判定不误杀 / 训练集与评测集不相交）
├── scripts/check_model.py   本地模型就绪探针
├── docs/
│   ├── stage0-research.md   10 个参考仓库的技术分析
│   └── taxonomy.md          15 类故障与 MAST/TRAIL 分类法映射
├── experiments/             evolution_report.md + metrics.json（自动生成）
│   └── real_model/          runs.jsonl / metrics.json / report.md / analysis.md
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
在你白跑一轮之前把这件事查出来。

**跑全量评测走这条**（OpenAI 兼容层关不掉思考，单次 14–22 秒，全量要 5 小时以上；
原生 `/api/chat` + `think:false` + `format=json` 压到 0.6 秒）：

```bash
# 1. 启动服务（模型目录在 F 盘，别放 C 盘）
cd /f/ollama && OLLAMA_MODELS='F:\Qwen3' ./ollama.exe serve &
# 2. 先让模型自己学一遍，训出两套库（约 7 分钟）
python ai-service/real_model_train.py            # → data/library_d.db / library_c.db
# 3. 全量 936 run，断点续跑（约 11 分钟）
python ai-service/real_model_bench.py --fresh    # 换过技能库就必须 --fresh，脚本会拦
# 4. 归因：C/D 到底命中了哪条技能（解释数字用，不跑模型）
python ai-service/real_model_attribution.py
# 5. 难度组（多步计算 / 跨文档冲突），单独落盘到 experiments/real_model/hard/
python ai-service/real_model_bench.py --suite hard --fresh
```

产物：`experiments/real_model/{runs.jsonl, metrics.json, report.md, attribution.md}`，
手写解读在 `experiments/real_model/analysis.md`。评测用独立库 `data/real_model.db`，
不覆盖 mock 结果。

**注意 grammar 约束的代价**：`format=json` 会从模型侧消除自然格式违规，
所以主实验里的 `format_violation` 只能靠注入产生。脚本内置 `ollama_raw` 无约束对照
（实测 0/6 严格 JSON、平均 10.4 秒、模型输出中文推理散文），用来测模型真实格式违规率。

---

## 已知限制（别当成能力）

1. **验证集太小**：每格只有 1 条用例，一次 before/after 就决定晋升与否 —— 等于抛硬币，
   容易误杀（`planner/incorrect_planning` 就是这么被拒的）。这条限制比任何算法细节都更能解释 97.1%。
2. **模型层没被测到**：任务是抽取式的，grammar 约束清掉格式错误，真实模型 936 次调用 0 次错答。
   要测模型的推理可靠性，得把任务难度提上去。
3. **故障是构造出来的**，不是真实事故里自然共现的错误；复合故障证明的是「机制上限」，
   不是「真实分布下的收益」。
4. **策略空间仍是固定词表**（6 种策略的组合），不是开放式规划；技能是声明式映射，不生成代码。
5. **真实工具链未接入**：仍是内存模拟工具，没有真文件读写 / HTTP 超时 / DB 错误 / 限流。
6. **流程不变量有盲区**：纯执行型流程（步骤里没有启动/收尾动作词）检测不到顺序颠倒。
7. **RAG 仍是 SQL 过滤**，没有 embedding / 向量检索 / rerank。
8. **负迁移只在两个维度上被挡住**：`guard` 覆盖「同 agent 不同故障」，
   `scope` 覆盖「同故障但单/复合」，没有单独的量化指标；批量投毒还没做。
9. **单次运行无多 seed**，模型随机性未量化。
10. FastAPI / PostgreSQL / Vue / Java / Docker / OpenTelemetry 一律未做。

---

## 下一步（按对「落地」的贡献排序）

- [x] **让 C 走自己的放宽验证流程重训技能库** —— 做完了，C/D 第一次拉开差距
- [x] **技能作用域**：复合链不再占单故障键位（`scope` + 回归测试）
- [ ] **扩大验证集**：每格至少 3 条，现在 1 条样本的验证等于抛硬币
- [ ] 接**真实工具**：真文件读写 + 真 HTTP 超时/错误，验证故障在真实链路上还能不能被抓到
- [ ] 提任务难度：多步计算 / 跨文档推理，让模型真的会答错
- [ ] 负迁移压力测试：批量投毒，测晋升门槛在不同投毒比例下的召回/误报
- [ ] 策略组合规划：从固定词表组合，升级为按轨迹状态动态生成组合
- [ ] 跨 Agent 迁移实验（H3：迁移距离越远收益越低，可能被证伪）
- [x] 真实模型全量四组评测（含模型版本 / Prompt 版本 / 代码指纹记录）

