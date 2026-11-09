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

# 2) 回归测试（224 个：56 核心 + 9 评测口径 + 19 难度组 + 44 检查器 + 27 静态 + 36 技能验证 + 25 安全层 + 8 保障视图）
python -m unittest discover -s tests -v

# 3) 启动 Web 看板（BoardUI 风格，零依赖）
python ai-service/api/app.py           # http://127.0.0.1:8770

# 4) 可选：检查本地模型是否就绪
python scripts/check_model.py --base http://127.0.0.1:11434/v1

# 5) 真实模型全量评测（936 run，可断点续跑；重跑必须落新目录）
python ai-service/real_model_train.py --output-dir experiments/train_A          # 训练产物（含 C/D 库）
python ai-service/real_model_bench.py --library-dir experiments/train_A \
                                    --output-dir experiments/bench_A            # 评测指定库
python ai-service/real_model_attribution.py   # 归因：C/D 命中了哪条技能

# 6) 冻结 / 校验实验基线（产物哈希 + 技能库内容指纹 + 配置指纹 + 分母自洽）
python ai-service/evoguard/baseline.py check --manifest experiments/p0_06_20261108/evolution_smoke/manifest.json
```

不需要 pip 安装任何东西，Python 3.9+ 标准库即可跑（接本地模型才需要 Ollama）。
Web 看板是**原生 HTML/CSS/JS 单文件**，不引 CDN、不需要 Node 构建。

### 数据集结构（2026-11-09 起）

每个 agent **15 个任务**：dev 9 + test 6。

- dev 9 再按角色切成三组、**彼此任务不重叠**：前 3 个探索（学经验）、中 3 个 target（量增益）、后 3 个 guard（量负迁移）。
- test 6 个任务完全不参与训练与验证，所以单故障测试集仍是 **204 例**，与历史口径可比。
- 为什么必须这么切：晋升门禁要求 target / guard / guard 基线成功各 ≥3 个**独立任务**，且 target 与 guard 不能是同一个任务。之前 dev 只有 2 个任务，41 个候选全被判 `NEEDS_REVIEW`（`target has fewer than 3 independent tasks/families`）—— 那是数据配置问题，修数据，不下调门槛。
- 难度组同样扩到 6 个训练任务（3 target + 3 guard），calc / xref 的技能才验得过去。
- ⚠️ **和上面的历史数字不可比**：测试集虽然还是 204 例，但装的题目换了（原来的 order A1002/A1003
  等现在是 test 题）。扩集后 mock 闭环是 **A 0.5 / B 24.0 / C 89.2 / D 86.3**，
  复合 **3.3 / 86.7 / 86.7** —— 这才是新数据集上的基线。

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
  单故障上 C 100% / D 97.1%。C 的 0% 不是模型不行 —— 它库里 38 条垃圾技能把两条有效的链
  挤到了永远轮不到的位置（第 1 轮恢复优先用单步技能，链长 1 就结束，第二个坏点没机会处理）。
- **D 少的 2.9pp 是能力缺口，不是治理出错**：6 条失败全在 `planner/incorrect_planning`，
  真实模型下 `cross_verify` 在验证集上 `before=0 / after=0`，严格验证拒了它，
  规则表也没有这条策略，只能转人工。其中 3 条恰好伴随模型报错，但**不构成不可评**——
  轨迹显示它们有答案、且计划本身就是倒序的，模型答不答都一样。
  mock 库里这条是有的 —— 说明 mock 的成功有虚高。
- **技能必须有作用域**：复合学到的链不能挂到单故障的键上，否则单故障用例会被套上多出来的一步
  （实测打挂 6 个用例）。现在技能带 `scope`，第 1 轮优先单故障技能、第 2 轮起优先多故障技能。

### 门禁上线后重跑（2026-11-09）：严格库为空，这不是代码 bug，是数据够不着门槛

P0 01–06 把「成对验证 + 样本充分性 + 晋升门禁」补齐后，同一套训练流程重跑了一遍：

- 训练：dev 49 例 → 41 个候选技能 → **严格晋升 0 条**，放宽库 41 条。
- 评测：270 例 × 4 模式 = **1080 run**（204 单故障 + 30 复合 + 36 难度组），1036 次模型调用。
- 产物：`experiments/real_model_20261108/{train,bench}/`，bench 带 manifest 与配置/库指纹
  （C 41 条、D **0** 条），`baseline.py check` 通过。

| 测试集 | A 无恢复 | B 固定规则 | C 不验证全放行 | D 完整闭环 |
|---|---|---|---|---|
| 单故障 204 例 | 0.0% | 26.5% | 100.0% | **94.1%** |
| 复合故障 30 例 | 0.0% | 0.0% | 0.0% | **0.0%** |
| 难度组 36 例 | 22.2% | 44.4% | 69.4% | **69.4%** |

**这几个数字不能按「D 变差了」来读**，得先看清 D 库里有什么：

- D 库 **0 条技能**。D 的 94.1% 全部来自「查不到技能就退回按根因的固定策略表」——
  查库里的 `recoveries` 可以核实：D 的恢复记录**没有 `skill_id`**，C 的有。
- 复合故障 D 0.0% 也是同一个原因：两步链技能一条都没晋升，第 2 轮起没有链可用。
- 历史那份 97.1% / 复合 100% 是**门禁前**直写库（38 条、不校验评估）跑出来的，
  与本轮不是同一个实验条件，不能混算。

**为什么一条都晋升不了**：`skills.evaluate` 的判决理由写得很直白 ——

```
['target has fewer than 3 independent tasks/families',
 'guard has fewer than 3 independent tasks/families',
 'guard has fewer than 3 baseline-success tasks/families']
```

`target_n = 1`、`guard_n = 2`（且只有 1 个独立任务族）。数据集里每个 (agent, fault) 只有 **1 个**
dev 任务，而门禁要求 target / guard / guard 基线成功各 ≥ 3 个独立任务。**门槛是对的，
数据不够**。这也解释了一个反直觉现象：本轮 C（不验证）100% > D（严格）94.1% ——
这里比的不是「严不严」，而是「有技能 vs 没技能」。

要恢复可比性，下一步必须把数据集扩到每个 (agent, fault) ≥ 3 个独立任务，
而不是下调 `MIN_VALIDATION_TASKS` 让数字好看。

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

### 离线检查器：不运行 Agent，只读轨迹 / 只读定义

前面所有实验都要「把 Agent 跑起来」。但企业里更常见的场景是：
**Agent 已经跑过了（或别人写的），我要知道它有没有出问题。** 这一层不执行、不调模型、不改数据。

```bash
cd ai-service
python -m evoguard_inspect.cli --list-rules                      # 看轨迹规则库（31 条）
python -m evoguard_inspect.cli --list-static-rules               # 看静态规则库（20 条）
python -m evoguard_inspect.cli --trace trace.json --out ../experiments/inspect
python -m evoguard_inspect.cli --trace t.json --config inspect.json   # 适配别的 Agent
python -m evoguard_inspect.cli --trace t.json --group tool flow       # 只看某几组
python -m evoguard_inspect.cli --agent agent.json --out ../experiments/inspect  # 静态检查
python -m evoguard_inspect.cli --agent repo/ --split-dir        # 扫真实代码仓库
python -m evoguard_inspect.cli --trace t.json --fail-on error   # CI：有 error 就退出码 3
```

**退出码**（三种结果必须分得开，否则 CI 没法用）：

| 码 | 含义 |
|---|---|
| 0 | 跑完了，没有达到 `--fail-on` 阈值的发现 |
| 2 | 输入解析失败（文件不存在 / 空文件 / 格式不认识）；`--strict-input` 下部分失败也算 |
| 3 | 跑完了，但发现数达到 `--fail-on error\|warn\|info` 指定级别 |

**零命中 ≠ 检查通过**：报告里有「规则适用性」一节，写清选中多少条、可执行多少条、
跳过的规则与原因（缺 token 用量 / 缺时间戳 / 配置没声明 `expect_keys` 等）。
只有全部规则都具备输入条件且零命中，结论才写「通过」；否则写「覆盖有限」。
这一条比多写几条规则重要 —— 一张写着 0 的体检单，如果一半项目没做，它比不做更有欺骗性。

**输入格式**（自动识别，认不出就明确报错，不静默返回空）：

| 来源 | 格式 | 说明 |
|---|---|---|
| 任意 OTel 化框架 | OTLP JSON（`resourceSpans`） | `gen_ai.*` 语义约定；LangChain / LlamaIndex / OpenAI Agents SDK / Langfuse 都能导出 |
| LangSmith | `{"runs": [...]}` | `run_type` 为 `llm` / `tool` 的记录 |
| Agent 定义 | JSON（prompt / tools / config） | 静态检查，不运行 |
| 代码仓库 | 目录（只读文本） | 启发式提取定义，`confidence=low` |

接 OTel 而不是给每个框架写适配器，是因为它是目前唯一被多框架同时采纳的标准。

**轨迹规则库 31 条，分 6 组**（全部确定性，不调用模型）：

| 分组 | 条数 | 查什么 |
|---|---|---|
| `answer` 输出层 | 6 | 空答案、缺约定字段、数字/标识无出处、PII 泄漏、**自称成功但证据不支持** |
| `tool` 工具层 | 7 | 报错被吞、零工具调用、参数为空、结果未引用、参数与上游不符、工具名幻觉、**永久错误仍重试** |
| `flow` 流程层 | 6 | 写前不读、同参数重试、A→B→A→B 循环、过早收尾、**重试风暴**、**静默空转** |
| `resource` 资源层 | 2 | token 超预算、单步异常慢 |
| `structure` 结构层 | 4 | 模型报错仍出答案、相邻双写、**写超时后重复调用**、**多 Agent 幻觉传播** |
| `security` 攻击面 | 6 | 工具结果里的注入指令、白名单外的工具、外带敏感数据、参数里的凭据、破坏性命令、答案里的 XSS |

**静态规则库 20 条，分 3 组**：prompt 层 6 / 工具定义层 8 / 配置层 6。
查的是「没运行时也能看见的东西」：prompt 缺拒答边界、工具描述没写副作用、
写操作没审批、密钥硬编码、命令执行工具没沙箱说明、缺超时/重试/轮次上限、
工具结果未声明为不可信数据、写工具没幂等键、没有单次运行预算。

其中 **10 条规则来自公开生产事故**（Replit 删库、Air Canada 幻觉政策、
重试风暴烧钱、CrewAI 静默死锁、AutoGen 幻觉传播、工具结果注入等）。
案例 → 根因 → 对应规则 → 怎么修的完整对照表见
`experiments/inspect/agent_failure_cases.md`。

分组的用处是**排查顺序**：先看输出层（结论错了），再往工具层/流程层倒推原因。

**泛用性靠配置，不靠改代码**（`--config`，JSON）：

```json
{"write_tools": ["order.settle"], "read_only_tools": ["kb.search"],
 "allowed_tools": ["kb.search", "order.get_order"], "expect_keys": ["total"],
 "max_repeat": 3, "token_budget": 20000, "slow_step_ms": 30000, "check_pii": true}
```

写操作识别、工具白名单、答案契约、预算阈值全部可配 —— 否则规则里写死「settle 是写操作」，
换个 Agent 全是误报。配置写错键名会**直接报错**，不静默忽略。

**怎么验证它不是摆设**（这一点比规则本身重要）：

| 夹具 | 来源 | 期望 | 实测 |
|---|---|---|---|
| `trace_synthetic_bad.json`（17 条轨迹） | 合成，每条只坏一处 | 各规则能对上具体那条 | 25 条规则里 20 条命中 |
| `trace_clean.json` | 真实成功轨迹 | 零命中 | 0 条 |
| `trace_retry_ok.json` | 真实「工具报错后重试成功」 | **不许**报 `error_swallowed` / `self_report_contradiction` | 均未报 |
| `trace_hallucination.json` | 真实工具输出幻觉 | 命中 `hallucinated_number` | 命中 |
| `agent_bad.json` / `agent_good.json` | 手写坏/好定义 | 坏的命中、好的零命中 | 16 / 0 |

只测命中，规则可以乱报；只测不误报，规则可以什么都不报。所以两类一起测。
未命中的 5 条不是失灵：`answer_schema_missing` 需要配 `expect_keys`、
`slow_step` 需要耗时字段、`model_error_ignored` / `tool_never_called` /
`unsupported_entity` 需要对应的数据形态，夹具里没有就**不该报**。

**误报是这类工具的头号死因，所以每条规则都必须写「什么时候会误报」**，
报告会把它印在每条问题下面。没有误报说明的规则不允许入库（注册时直接抛错）。

第一版 `tool_result_unused` 拿整个 JSON 的 token 做交集，11 条轨迹全命中 ——
因为 JSON 的 key（items / docs / ok）本来就不会出现在答案里。改成「只看值、
且要求返回有实质内容」后降到 5 条，并加了回归断言（命中数不得超过轨迹数一半）。

### 真实框架实测与误报治理

扫了 **9 个框架、76 个 Agent 定义**：OpenAI Agents SDK / Swarm、Google ADK、
Microsoft AutoGen、Hugging Face smolagents、LangGraph、CrewAI、PydanticAI、Agno
（大厂 + 小厂 + 多 Agent 框架）。完整报告见 `experiments/inspect/real_agents_scan.md`。

**最重要的结果是「报得更少」**：第一版报出 298 条线索，其中
`no_timeout` / `no_retry_policy` / `no_max_iterations` / `no_output_schema`
在几乎所有框架 100% 命中。按项目里踩过的教训 ——
**一条规则如果命中率接近 100%，它就是噪音，不是发现** —— 根因是这些
「缺失型」规则在从源码猜出来的定义上判断「没读到 ≠ 不存在」：
教学示例的配置在框架默认值 / 环境变量 / 部署平台里。

修法：给静态规则加 `needs_high` 标记，13 条缺失型规则只在
`confidence=high`（显式定义文件）上生效；低置信度下只保留 3 类
「存在型」证据（读到 PII / 读到密钥 / 读到命令执行工具）。
**298 条 → 2 条真线索，0 条高命中噪音规则。**

顺带补齐了提取器对各框架特有写法的适配，并各加一条回归测试：
AutoGen `system_message=`、Google ADK `instruction=`、CrewAI `role/goal`、
PydanticAI `@agent.tool`、多行字符串拼接 prompt。修复前扫 AutoGen /
PydanticAI 一个定义都拿不到。

**安全**：下载物全程只读文本，未执行、未 import、未安装；解压拒绝路径穿越；
`_sandbox` 用完删除（11934 个文件 / 403.9 MB，可执行文件 0 个，目录已确认不存在）。
清理脚本 `scripts/cleanup_sandbox.py` 会打印可核对的清点记录。

**这份实测不说明什么**：「0 条线索」不等于框架没问题；静态检查看不到运行时行为，
示例不等于生产配置；闭源 Agent（Replit、Air Canada bot）拿不到定义，
只能依据公开案例分析。真正查运行时问题要靠轨迹检查，两层一起用。

> 想复现实测：先跑 `scripts/fetch_mini_repos.py` 与整包下载重建 `_sandbox`，
> 再跑 `scripts/scan_real_agents.py`，最后 `scripts/cleanup_sandbox.py` 清理。
> 当前 `_sandbox` 已按要求删除，扫描结果以 `real_agents_scan.json` 为准。

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

### 保障视图：一个 Agent 能不能放心跑

**AI 保障 = 可靠 + 防护 + 可运行**。三个维度各自判定，不合成 0–100 分 ——
分数会让人优化分数而不是优化系统。

```bash
cd ai-service
python -m evoguard_inspect.cli --trace ../experiments/p0_09_20261109/audit_smoke/evolution.db \
  --assurance --out ../experiments/assurance      # 直接读运行库（按文件头识别 SQLite）
```

| 维度 | 看什么 | 判定 |
|---|---|---|
| 可靠 | 结论有没有证据、流程/工具/资源是否乱 | 25 条轨迹规则 |
| 防护 | 注入、越权、外带、凭据、危险命令、XSS | security 组 6 条 |
| 可运行 | 模型/工具报错率、预算、耗时、报错后仍出答案 | 轨迹状态与 token |

零命中但有用例没跑成 → 判 **覆盖有限**，不写「通过」；报告每维都写「测了什么 / 没测什么」，
并标注数据里有多少条是故障注入（否则失败率会被当成线上失败率读）。详见 [docs/assurance.md](docs/assurance.md)。

**退出码**：0 通过/有风险，3 不通过，2 输入解析失败。

### 安全层：三层防护，默认只检测不改写

攻击面有三条口子：**用户输入**、**工具返回的外部内容**、**Agent 吐给浏览器的输出**。

| 层 | 位置 | 做什么 |
|---|---|---|
| 定义层 | 静态规则（20 条） | prompt 有没有拒答边界、有没有声明「工具结果是数据不是指令」、写操作有没有审批、有没有危险 shell |
| 行为层 | 轨迹规则 security 组（6 条）+ 运行时 4 个钩子 | 注入指令、白名单外工具、外带敏感数据、参数里的凭据、破坏性命令、答案里的 XSS；运行时在输入 / 工具调用 / 工具返回 / 模型输出四处扫描 |
| 边界层 | `api/app.py` | `nosniff`、`X-Frame-Options: DENY`、CSP、按落点转义（`encode_for`） |

```bash
# 只看安全相关的发现
cd ai-service && python -m evoguard_inspect.cli --trace t.json --group security

# 运行时拦截（默认 audit：只记账不改内容，保证历史实验可比）
AGENTLAB_SECURITY_MODE=enforce python ai-service/api/app.py
```

**延迟**：1 KB 0.044 ms / 10 KB 0.31 ms / 50 KB 1.84 ms；真实运行每次约 **51 µs**，
相对真实模型的 627 ms 是 **0.008%**（`experiments/p0_09_20261109/security_bench.json`）。

**必须说清楚的边界**：这是特征匹配，改个说法就能绕过；它是「发现 + 降风险」，
不是「防住」。真正兜底的是工具权限最小化、写操作审批与沙箱 ——
这三样在本仓库里仍然只是**声明**。详见 [docs/security.md](docs/security.md)。

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
│   │   ├── baseline.py      冻结可复现基线：manifest / 库内容指纹 / 防混跑 / 分母自洽
│   │   └── evaluate.py      A/B/C/D 四组对照 + 指标
│   ├── evolution_bench.py   闭环入口（产出 report.md + metrics.json）
│   ├── real_model_train.py  真实模型自举训练（严格库 / 放宽库，test 不参与）
│   ├── real_model_bench.py  真实模型全量评测（断点续跑 / grammar 对照 / 指纹）
│   ├── real_model_attribution.py  归因：C/D 命中的是哪条技能
│   └── api/app.py           REST API（标准库 http.server）
├── frontend/index.html      BoardUI 风格看板（双主题，零依赖）
├── tests/
│   ├── test_core.py         56 个测试（对抗性投毒 / 策略链 / 完整回滚 / 经验溯源 / 基线冻结）
│   ├── test_judgeability.py 9 个测试（可评口径：业务失败 vs 服务抖动）
│   ├── test_hard_tasks.py   19 个测试（真值可独立算 / 判定不误杀 / 训练集与评测集不相交）
│   ├── test_inspect.py      42 个测试（规则全命中 / 零误报 / 配置适配 / 事故模式 / 覆盖率与退出码）
│   ├── test_static.py       27 个测试（静态规则 / 框架写法适配 / 低置信度噪音治理 / 覆盖率）
│   ├── test_skill_validation.py 36 个测试（成对负迁移 / 样本充分性 / 晋升门禁 / 严格库链路）
│   ├── test_security.py     25 个测试（注入/越权/外带/凭据/XSS 检测，误报与延迟预算，audit vs enforce）
│   └── test_assurance.py    8 个测试（三维判定 / 覆盖有限不算通过 / 报错后仍有答案 / 不给分数）
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

