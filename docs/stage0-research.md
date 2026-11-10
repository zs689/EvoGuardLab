# EvoGuard · 阶段 0 研究报告

> 日期：2026-11-06　｜　调研与整理：EvoGuardLab 项目
> **本报告的证据边界**：我检查了 `E:\agent\github` 下 10 个仓库的**实际存在性、目录结构、许可证、
> pyproject 依赖、关键类名、README**，**没有逐行阅读实现，也没有运行任何仓库**。
> 下文凡写"未读"的地方，就是我没读过，不做推测。

---

## 一、目录核查结论（先看真实情况）

`E:\agent\github` 下 10 个目录**全部存在，且都是完整 git 克隆**：

| 仓库 | 文件数 | 体积 | LICENSE |
|---|---|---|---|
| Agent0 | 1315 | 53M | 有 |
| AgentChaos | 69 | 7.9M | **⚠️ 无**（顶层和三层子目录都没有） |
| AgentEvolver | 408 | 62M | 有 |
| AgentLab | 215 | 6.6M | 有 |
| AgentRx | 190 | 14M | MIT（Microsoft） |
| EvoAgentX | 596 | 31M | MIT |
| EvoSkill | 239 | 13M | Apache-2.0 |
| GenericAgent | 241 | 52M | 有 |
| agent-chaos | 143 | 4.7M | Apache-2.0 |
| evolver | 499 | 19M | 有 |

**第一条硬结论：`AgentChaos` 没有许可证。**
无许可证 = 默认版权保留（all rights reserved）。可以读它的思路，**不能复制它的代码**。
这与规格第十九条"必须核实许可证和兼容性"直接冲突，必须先处理。

---

## 二、四个优先仓库（详读）

### 2.1 EvoAgentX（MIT）

- **定位**：用 LLM 自动生成并进化 agentic workflow 的框架。README 原话："Building a Self-Evolving Ecosystem of AI Agents"。
- **技术**：Python ≥3.10，370 个 .py，包名 `evoagentx`，有 pytest.ini、mkdocs 文档站。
- **核心目录**（14 个子包）：
  `actions / agents / benchmark / core / evaluators / frameworks / hitl / memory / models / optimizers / prompts / rag / storages / tools / utils / workflow`
- **关键类**（实际 grep 到的）：
  - 工作流层：`ActionGraph`、`WorkFlowController`、`Operator`、`OperatorOutput`
  - **轨迹层：`TrajectoryState`、`TrajectoryStep`** ← 对我们最直接有用
  - 模型层：`ModelSelector`、`DefaultModelSelector`、`SimpleModelSelector`、`ToolBasedModelSelector`
  - 进化层：`optimizers/{aflow, mipro, evoprompt, map_elites, sew}_optimizer.py`
  - 还有 `hitl/`（human-in-the-loop）和 `storages/`
- **可复用（学设计，不搬代码）**：
  1. `TrajectoryState / TrajectoryStep` 的字段划分 —— 我们自己的 steps 表可以直接对齐
  2. `ModelSelector` 的"按任务选模型"抽象 —— 对应我们的 Model Gateway 路由
  3. optimizers 里的 **SEW / AFlow**：把"工作流"当作可优化对象，而不是只优化 prompt。这正是我们 Skill 进化的理论同源
- **不适合复用**：它面向"生成 workflow"（让 LLM 画图），我们的 Runtime 需要**确定性可复现的状态机**。它的图是动态生成的，我们的必须能被反复重放和注入故障。**两套状态机不能混用**（这条在 AgentLab 阶段已经被验证过一次教训）。
- **集成难度**：中。MIT 许可友好，但依赖较重，且它的抽象是"进化 workflow"，我们要的是"进化 skill"。
- **建议**：**读设计，不复用代码**。重点抄它的 Trajectory 字段和 optimizers 的"候选-评测-择优"三段式。

### 2.2 EvoSkill（Apache-2.0）

- **定位**：给**编码 Agent** 自动发现技能的工具包。有论文（alphaxiv 2603.02766）和技术报告 PDF。
- **技术**：Python **≥3.12**，151 个 .py。依赖很重且很垂直：
  `opencode-ai / openai-codex-sdk / claude-agent-sdk / openhands-sdk / openhands-tools`
  → 本质是**绑定多个编码 Agent harness**（claude / codex / openhands / goose / harbor）。
- **核心目录**：`agent_profiles/{skill_generator, skill_proposer, prompt_generator, prompt_proposer}`、`loop/runner.py`、`evaluation/reward.py`、`registry/manager.py`、`harness/{claude,codex,openhands,goose}/`、`cache/run_cache.py`、`remote/daytona.py`
- **可复用（学设计）**：
  1. **`loop/runner.py`** —— 进化循环的主控：提议 → 评测 → 择优，每轮产出一个**全新的 agent program**（不是原地改一个 prompt）
  2. **`registry/manager.py`** —— 技能注册与版本管理
  3. **`evaluation/reward.py`** —— 用 held-out 数据算收益，而不是看训练集
  4. `run_cache.py` —— 避免重复跑同样的评测（省 token 的直接手段）
- **不适合复用**：
  - 它是**为编码 Agent 定制的**，harness 全是代码执行沙箱。我们的三个业务 Agent（订单/检索/规划）套不进去。
  - 依赖链极重（要装 4 个 SDK），不符合我们"轻依赖、能离线跑"的约束。
- **集成难度**：高（依赖），**但设计可抄**。
- **建议**：**只抄 `loop/runner` 的三段式和 `registry` 的版本管理**，代码不搬。

### 2.3 Microsoft AgentRx（MIT）★ 最直接可借鉴

- **定位**：从执行轨迹里**定位失败的关键步骤**并归类。README 原话：
  "pinpoints the *critical failure step* in a failed agent trajectory. It synthesizes constraints (invariants), evaluates them step-by-step, and produces an auditable validation log"
- **技术**：Python，28 个 .py + **106 个 json**（大量是数据/规则），`run.py` 为 CLI 入口。
- **流水线（README 明确写的）**：
  `Raw logs → Trajectory IR → Invariants → Checker → Judge → Reports`
- **关键类**：
  - `invariants/checker.py` → `AllVerifier`、`Violation`、`CheckTelemetry`
  - `invariants/static_invariant_generator.py` / `dynamic_invariant_generator.py`
  - `ir/trajectory_ir.py` → 轨迹中间表示（**这层正是我们缺的**）
  - `judge/judge.py` → LLM judge
  - `reports/analyze_failure_frequencies.py` / `analyze_metrics.py`
- **数据资产**：`data/{ground_truth, policies, tool_schemas, magentic_dataset, tau_retail}`
  → **`policies` 和 `tool_schemas` 是"业务不变量"的现成范本**
- **可复用（这是重点）**：
  1. **"不变量（invariant）"这个概念本身** —— 这是治"静默故障"的正解：
     不看 JSON 合不合法，看**业务约束**成不成立（金额必须等于明细重算值、订单状态必须允许结算）
  2. **轨迹 IR** —— 把异构日志压成统一中间表示，才能让诊断规则可复用
  3. 分层诊断：静态不变量 + 动态不变量 + LLM judge 兜底
  4. 10 类失败分类法 —— 可以直接对照我们的 Fault Taxonomy
- **不适合复用**：它的 judge 依赖 Azure / TRAPI 端点（企业内网服务），**我们本地跑不了**。而且它是"离线分析已有轨迹"，不是"在线实时注入+恢复"。
- **集成难度**：**低到中**。MIT、纯 Python、模块清晰，是我们最该深读的一个。
- **建议**：**深读 `invariants/` + `ir/` 两个目录的实现**，把"不变量检查器"作为我们 Reliability Engine 的第二层诊断。

### 2.4 AgentChaos（无许可证）⚠️

- **定位**：SWE-bench Pro 场景下的故障注入。目录只有 `README.md / SWE-bench_Pro-os / datasets / docs / pyproject.toml / scripts`，22 个 .py。
- **可借鉴**：故障场景与数据集的**组织方式**（怎么把一个 chaos 场景描述成可复现的配置）。
- **致命问题**：**没有任何 LICENSE 文件**。
- **建议**：**只读思路，一行代码都不要复制**。如果确实需要复用，必须先去仓库页确认是否有其他许可声明，或直接联系作者。法律上默认"保留所有权利"。

### 2.5 补充：agent-chaos（Apache-2.0）★ 比 AgentChaos 更该用

规格把它排在第二优先级，但我读完之后认为**它的价值高于 AgentChaos**，因为它是**可用的代码**：

- 84 个 .py，Apache-2.0，目录结构专业：`chaos/ core/ core/metrics/ event/ events/ integrations/ patch/ patch/providers/ scenario/ stream/ ui/`
- 关键类（实际 grep 到）：**`ToolFuzzConfig`、`StreamFuzzConfig`、`LLMFuzzConfig`、`ContextFuzzConfig`、`ChaosSpace`**、`fuzz_chaos()`
- 含义很清楚：它把故障分成 **工具 / 流式 / 模型 / 上下文** 四类，并且用 `patch/providers` 做**注入点插桩**，还带 `metrics/` 和 `ui/`
- **可复用**：`patch/providers` 的插桩思路（不改业务代码就注入故障）+ 四类故障的配置化设计 + `ChaosSpace` 的随机组合
- **建议**：**这是四个里唯一"可以直接读代码并借鉴实现细节"的**（Apache-2.0，保留版权声明即可）。

---

## 三、其余六个仓库（速览，未深读）

| 仓库 | 从我看到的判断 | 用途 |
|---|---|---|
| **ServiceNow AgentLab** | 215 文件，6.6M，有 LICENSE。看名字是"批量跑 agent 实验 + 数据集 + 评测"，与我们 Layer 7 重叠 | 参考实验管理与结果组织的表结构 |
| **evolver** | 499 文件，19M，有 LICENSE。规格说"可审计进化、技能版本、经验资产" | 参考"进化事件审计"的记录方式 |
| **GenericAgent** | 241 文件，52M，有 LICENSE。规格说"技能树、自主积累技能" | 参考技能树的组织，不搬代码 |
| **AgentEvolver** | 408 文件，62M。训练型自进化 | 首版不涉及训练，暂不看 |
| **Agent0** | 1315 文件，53M（最大）。自动任务生成 | 后期"任务自动生成"再回来看 |
| **AgentEvolver / Agent0** | 都偏训练/自博弈 | 与阶段 10 才相关 |

**这六个我确实没深读**，只看了结构和许可。等 MVP 跑通再回来看，不浪费现在的时间。

---

## 四、从四个仓库得出的关键技术判断

### 判断 1：我们的定位不是"再造一个框架"，而是"给已有的可靠性机制加一条进化闭环"

四个仓库各占一块，**没有一个是"故障→诊断→恢复→提炼经验→验证→升级"的完整闭环**：

- EvoAgentX：能进化 **workflow**，但不做故障注入和恢复验证
- EvoSkill：能从失败里提炼 **skill**，但只针对编码 Agent，且不做"负面效果检测"
- AgentRx：能**定位**失败步骤，但不恢复、不学习
- agent-chaos：能**注入**故障，但不诊断、不学习

**空白正好在中间：谁来保证"提炼出来的经验是真的有用、而不是有害的"。**
这就是 EvoGuard 唯一值得做的位置。

### 判断 2：不变量（invariant）比"JSON 合法性"重要一个数量级

AgentRx 的 `invariants/` 给了我一个明确方向：静默故障能不能被发现，取决于**你有没有写出业务不变量**。
我们 AgentLab 里那条"用轨迹里的明细独立重算金额"，本质就是一条不变量。EvoGuard 应该把它系统化成**可配置的不变量库**，而不是散落在诊断规则里。

### 判断 3：插桩层要独立于业务代码

agent-chaos 的 `patch/providers` 说明：故障注入应该做成**在工具边界插一层**，业务 Agent 完全不知情。
我们 AgentLab 现在是"在 Ctx.call 里 if 判断故障类型"，能跑，但**注入逻辑和运行时耦合**。
EvoGuard 应该拆成独立的 `fault proxy`。

---

## 五、EvoGuard 架构（复用 AgentLab 的已验证部分，不重写）

### 5.1 我的反驳：不要从零开始

规格要求"新项目独立建立在 `E:\agent\EvoGuardLab`"。我建议**目录独立，但代码不重写**。

理由（优缺点都摆出来）：

| 方案 | 优点 | 缺点 |
|---|---|---|
| A. 完全从零写 | 结构干净、没有历史包袱 | 丢掉 AgentLab 已跑通的 Runtime + 8 类故障 + 诊断 + 恢复 + 66 用例 + 11 个测试，**约 2 周白干**；且有 4 个 bug 是踩过才知道的（步号定位、B/C 平局、故障没注入、连接未关闭） |
| B. 目录独立 + 搬运自己写的模块 | 保留已验证资产；许可证无问题（都是我自己写的）；2 周内能到阶段 4 | 需要一次重构（拆 `Ctx.call` 里的注入逻辑） |
| C. 直接在 AgentLab 里加 | 最省事 | 偏离你"独立项目"的要求，且 AgentLab 的定位是"评测平台"，EvoGuard 是"进化平台"，混在一起会讲不清 |

**我的推荐：B。** `EvoGuardLab/ai-service/` 下新建，把 `runtime.py / sim.py / evaluate.py` 搬过去并重命名为 EvoGuard 的模块，然后在上面**只加三样新东西**（经验、技能、治理）。

### 5.2 复用与自研的边界（明确写死）

| 层 | 来源 | 做法 |
|---|---|---|
| Runtime（状态机/Checkpoint/预算） | 复用 AgentLab | 搬运 + 拆出 fault proxy |
| 故障注入 | 借鉴 agent-chaos 的插桩思路 | **自己实现**（Apache-2.0 可参考，但我们的故障类型不同） |
| 轨迹 IR | 借鉴 AgentRx 的 `ir/trajectory_ir.py` | **自己实现**，字段对齐 AgentRx |
| 不变量检查 | 借鉴 AgentRx 的 `invariants/` | **自己实现**，做成可配置的规则库 |
| 诊断 | 自己（AgentLab 已有 6 条规则） | 扩展为五层 |
| 恢复 | 自己（已有） | 扩展策略表 |
| **经验提取 / 技能生成 / 验证 / 晋升 / 回滚** | 借鉴 EvoSkill 的 loop 三段式 + registry | **全部自研** ← 这是创新点所在 |
| Memory Governance | 自研 | 自研 |
| 评测 | 自己（已有 A/B/C） | 扩展为 A/B/C/D 四组 |

### 5.3 模块图（首版）

```
ai-service/
├── core/            store(SQLite) / 状态机 / 错误码 / ID
├── runtime/         executor / checkpoint / budget / trace
├── faults/          proxy / taxonomy(14类) / injector
├── candidates/      invariants 规则库 / verifier
├── diagnosis/       五层诊断（规则→不变量→轨迹→经验RAG→LLM）
├── recovery/        planner / executor / approval
├── experience/      extractor / validator / store
├── skills/          generator / registry / tester / promoter / rollback
├── memory/          RAG（BM25+向量）/ governance
├── evaluation/      A/B/C/D runner / metrics / report
└── agents/          三个 Target Agent
```

前端与 Java 层：**阶段 1 不做**。规格自己也说了"优先最小闭环"。

---

## 六、三个创新点（收敛到可验证的）

### 创新点 1：经验的"准入门禁"（不是生产经验，而是**拦截坏经验**）

普通做法：成功/失败 → 总结一段话 → 塞进记忆。
我们的做法：经验必须带 **evidence_refs（指向具体 step_id）+ 适用条件 + 反例集**，
并且必须通过**独立验证集**测试才允许进 RAG。未验证经验只进"待审区"，检索时被过滤。

**研究价值**：这是"记忆越多越蠢"问题的直接解药。

### 创新点 2：负迁移检测（Negative Transfer Detection）

每次技能升级，不只看"目标场景成功率涨没涨"，必须同时看**"原本能成功的任务有没有被弄坏"**。
弄坏的比例超过阈值 → 拒绝晋升。

**研究价值**：几乎所有自进化项目只报"提升"，不报"伤害"。这是我们能拿出的差异化指标。

### 创新点 3：进化收益的成本核算

每次升级记录：成功率变化 / Token 变化 / 延迟变化 / 恢复步骤变化，
算出一个**收益成本比**。没有稳定收益的技能保持候选状态。

**研究价值**：让"自进化"从"看起来更聪明"变成"可核算的工程决策"。

---

## 七、可测量的研究假设（必须可被证伪）

| # | 假设 | 怎么测 | 如果成立 | 如果被证伪 |
|---|---|---|---|---|
| H1 | 在静默数据篡改场景，D（完整进化）比 B（仅规则恢复）成功率高 ≥15 个百分点 | 同一测试集跑 B/D | 证明经验学习有增量价值 | 说明规则比学习更有效，那本身就是结论 |
| H2 | 不严格验证经验的 C 会产生负迁移，D 能把负迁移率压到 <5% | 测"原本成功任务"在 C/D 下的失败率 | 证明门禁必要 | 说明负迁移不严重，创新点 2 要重新定位 |
| H3 | 技能收益随任务迁移距离衰减：同模板 > 同 Agent > 跨 Agent | 三分层测试集 | 给出"经验适用边界"的定量结论 | 说明技能可泛化，价值更大 |
| H4 | 每次升级的 Token 成本增幅 <30% 时才有正收益成本比 | 记录每次升级前后 | 给出升级的工程阈值 | 阈值会更严或更松，仍是有用数据 |

**H3 是我最看重的**：它有可能被证伪，而"跨 Agent 迁移几乎无效"这种**负面结论**恰恰是有科研价值的。

---

## 八、MVP 定义（就是那张图里的 5 步）

**MVP 只证明一件事：Agent 经历一次错误后，真的变强了，而且没有变坏。**

范围（2–3 周）：
1. 复用 AgentLab 的 runtime + 8 类故障 + 诊断 + 恢复 + 评测
2. 加 `experience/extractor`：从一次故障-恢复轨迹生成一条结构化经验（含 evidence_refs）
3. 加 `skills/`：把经验变成候选技能（声明式 JSON，**不生成可执行代码**）、在独立任务集测试、PROMOTE/REJECT/NEEDS_REVIEW
4. 加 `memory/governance`：未验证经验不进检索
5. 四组对照 A/B/C/D 跑同一测试集

**明确不做（首版）**：FastAPI/Java/Vue/PostgreSQL/Redis/Docker/pgvector/RAG 向量库/MCP/QLoRA。
理由：这些都不影响"能不能证明进化有效"这个核心问题。等核心问题有答案再加皮。

验收标准（可执行）：
- 同一个订单 Agent，在 `silent_corruption` 场景：第一次失败 → 生成候选技能 → 独立任务集通过 → 晋升 → 第二次自动触发 → 成功
- 同时报告：原本 100% 成功的 baseline 任务，晋升后仍然是 100%（负迁移 = 0）
- 报告里同时给出 A/B/C/D 四条曲线

---

## 九、开发任务清单（按依赖排序）

| # | 任务 | 依赖 | 验收条件 |
|---|---|---|---|
| T0 | 确认 AgentChaos 许可证问题；确认是否复用 AgentLab | — | 有明确文字结论 |
| T1 | 建 `EvoGuardLab` 目录 + 搬 `core/runtime` + 让 11 个测试在新目录跑通 | T0 | `python -m unittest` 全绿 |
| T2 | 拆出 `faults/proxy`，故障注入与 runtime 解耦 | T1 | 11 个测试仍全绿；注入逻辑集中在 proxy |
| T3 | 轨迹 IR（对齐 AgentRx 字段）+ 不变量规则库（至少 5 条业务不变量） | T1 | 不变量能独立于诊断规则运行，有单测 |
| T4 | `experience/extractor`：轨迹 → 结构化经验（含 evidence_refs） | T3 | 一次故障能生成 1 条经验，字段完整 |
| T5 | `experience/validator` + `memory/governance`：未验证经验不可检索 | T4 | 单测：未验证经验被检索过滤掉 |
| T6 | `skills/`：候选技能生成（声明式）+ 独立验证集测试 + 三态判定 | T5 | 候选技能必须通过测试才 PROMOTE；有负迁移检测 |
| T7 | 四组对照 A/B/C/D 评测 + 报告（含失败案例） | T6 | 输出四条曲线 + 负迁移率 |
| T8 | 演示脚本：一次错误 → 进化 → 二次成功（一张图能讲清） | T7 | 一条命令跑出完整叙事 |
| T9 | README + 架构文档 + 许可证清单（引用来源声明） | T8 | 第三方代码零复制，或有明确声明 |

**阶段 1 的"基础工程"我不在本轮做**，因为 T0 没定之前建目录是浪费——见我的反驳。

---

## 十、本轮没做、也不能假装做了的

- 没有运行任何参考仓库的代码（它们的依赖太重，且部分需要 Azure/API Key）
- 没有逐行读 EvoSkill / EvoAgentX 的实现，只读了结构、类名、README
- 没有建 `E:\agent\EvoGuardLab` 工程（等 T0 结论）
- 没有验证 AgentChaos 是否有隐藏许可声明（需要在 GitHub 页面确认）
- 规格里的 PostgreSQL / Redis / Vue / Java 一律未触碰
