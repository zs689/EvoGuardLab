# EvoGuard

**自进化 Agent 可靠性与自主恢复系统**
Self-Evolving Agent Reliability & Autonomous Recovery

> 一句话：不只让 Agent 从错误里「学到经验」，还要用**独立测试**证明这条经验真的有用、且没有把原本能做的事弄坏。

## 五分钟离线跑通（不需要语料 / 网络 / 模型）

```bash
# 在仓库 checkout 根目录执行
pip install -e .                 # 零运行时依赖，Python 3.10+
evoguard demo                    # 同一批攻击 × 五档守卫配置
evoguard config                  # 打印生效的安全配置
evoguard test                    # 全量回归（数量以输出为准，文档不写死）
python scripts/review_checks.py --gate   # 逐项客观门槛，不是自评分数
```

不装包也行：`python evoguard_cli.py demo` 是同一个入口。

**不想被长实验卡住**：`POST /api/jobs` 只写一行就返回任务 ID（8 并发提交
P95 ≈ 14 ms），后台单 worker 顺序执行；`GET /api/jobs/{id}` 轮询，
`Idempotency-Key` 保证重复提交不产生第二次副作用；进程崩溃后过期租约
标成 `unknown`，**不自动重放**（副作用可能已经发生），需人工对账后显式
retry。详见 [接口契约](docs/api-contract-v1.json)。

**跑完看什么**：打开 `experiments/defense_demo_v2/demo.md`，找「检测命中」
和「工具真的执行」两列。这个项目的核心主张就是——**检测到 ≠ 拦住了**；
一个 `decision=deny` 却留下 `executed > 0` 的守卫，在这里算失败，照实记数。

更多入口：[接口契约](docs/api-contract-v1.json) ·
[企业边界与已知缺口](docs/enterprise-readiness.md) ·
[独立审查整改台账](docs/review-remediation/README.md) ·
[贡献指引](CONTRIBUTING.md) · [安全策略](SECURITY.md) · [变更记录](CHANGELOG.md)。

## 这是什么 / 不是什么

**是**：一个可复现的本地研究原型 —— Agent 轨迹检查器（31 条轨迹规则 + 20 条静态规则）、
技能晋升门禁（前后配对 + 负迁移 + 样本充分性）、三维保障判定（可靠 / 防护 / 可运行）、
可恢复审批状态机、结构化工具授权、按**真实执行后果**计量的防御演示。

**不是**：

- **不是生产级 WAF / 沙箱**：检测是特征匹配，改个说法就能绕；没有通用执行隔离。
- **不是「防住提示注入」**：已接入外部语料 InjecAgent 2108 条（间接注入）
  与 1998 条直接越狱话术；后者实测正则召回仅 **4.1%**，实测**字面注入话术**召回 1.0、
  **自然语言注入**召回 0.0（训练的第二级模型把它拉到 0.96，但误报 6%，门禁判定暂缓发布）。
  6 个自构造向量的工程冒烟仍在，两者分开报，不混成一个准确率
  （见 [docs/attack-datasets.md](docs/attack-datasets.md)、[docs/detector-evolution.md](docs/detector-evolution.md)）。
- **不是真实业务准确率**：37 条自生成标注只能当回归护栏，没有独立标注者与外部轨迹。
- **不是多 Agent 安全**：互信函数只在库级，运行时没有多 Agent 链路可验。

---

## 现在能跑什么（真实状态，不是路线图）

```bash
cd EvoGuardLab

# 1) 完整闭环：故障 → 诊断 → 策略链恢复 → 提炼经验 → 独立验证 → 晋升 → 对照实验
python ai-service/evolution_bench.py

# 2) 回归测试（814 个：56 核心 + 9 评测口径 + 19 难度组 + 44 检查器 + 27 静态 + 36 技能验证 + 34 安全层 + 8 保障视图 + 15 标注集 + 8 安全契约 + 12 防御演示 + 12 文档一致性 + 15 四模式对照 + 15 审批与结算 + 21 评测质量与 CI + 21 条件技能 + 12 工具契约 + 17 E02/E03 + 12 公平消融 + 34 边界异常 + 8 守卫模糊 + 14 不可信输入 + 8 可复现性 + 11 安全解压 + 14 看板接口 + 13 外部语料 + 10 CI 后果证据 + 36 第二级模型 + 11 真实模型链路 + 10 越狱语料 + 12 AgentDojo 适配器 + 17 最后一轮扩样 + 24 收尾轮 + 38 LlamaFirewall 融合 + 15 执行入口 + 10 审批加固 + 9 基线指纹 + 16 中文语料 + 22 按工具护栏 + 6 分档守门 + 13 双标注守门 + 17 预注册实验守门 + 7 双标注结果守门 + 11 检测器 v2 守门 + 12 服务层守门）
python -m unittest discover -s tests -v

# 3) 启动 Web 看板（BoardUI 风格，零依赖）
python ai-service/api/app.py           # http://127.0.0.1:8770

# 4) 可选：检查本地模型是否就绪
python scripts/check_model.py --base http://127.0.0.1:11434/v1

> **复现分层**：`data/external/` 与 `_sandbox/` 被 `.gitignore` 排除（语料正文、
> 下载缓存不随仓库分发，涉及许可与体积）。全新 clone 上 `demo` / `config` /
> 核心回归可直接绿；依赖语料的评测与测试用 `skipUnless` 明确跳过并打印前置条件，
> `evoguard eval` 只跑不依赖语料的两项，不再抛 traceback。导入语料后重跑，
> 产物与仓库内已提交的报告逐字节一致。`.gitattributes` 把 fixtures 与产物锁成 LF，
> 避免 Windows 的 `core.autocrlf=true` 改掉字节、触发 sha256 守门失败。
> 文档里的「814 项」指**语料齐全时**的全量套件；clean clone 上会如实显示 skipped。

**检测能力 v2（重新预注册，结果是负结果）**：`docs/semantic-injection-v2-plan.md`
用联网核实的一手方法（StackOne SFE 逐字段过滤、Prompt Guard 2 重叠分窗取 max、
DeFuse 长度/预算感知阈值与 AUC 口径）重做了最弱的一环；**没有引入** Prompt Guard 2
的权重（gated 许可 + 需 transformers）。重叠分窗把「payload 埋在 8000 字符之后」
的检出从 **0 拉到 1.0**、AUC 0.9917 → 0.9947；字段过滤把过度防御误报
**14.8% → 0.9%**；但两者叠加会让 AUC 掉约 0.03。**预注册四条判定线未全部满足，
总判定「未通过」**，没有放宽判定线、没有改结果；详见
`experiments/semantic_injection_v2/report.md`。

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

### 门禁后重跑（2026-11-09）：先 0 条，补数据集后 11 条

P0 01–06 把「成对验证 + 样本充分性 + 晋升门禁」补齐后，第一次重跑的结果是**严格库 0 条**：
门禁要求 target / guard / guard 基线成功各 ≥3 个**独立任务**，而当时每个 (agent, fault) 只有 1 个验证任务。
门槛没错，是数据不够。于是把数据集从每 agent 8 个任务扩到 15 个（dev 9 = 探索 3 / target 3 / guard 3，
三组任务不重叠；test 6），再重训。

| 步骤 | 严格库 | 单故障 D | 复合 D |
|---|---|---|---|
| 门禁后第一次（1 个验证任务） | **0 条** | 94.1%（全是规则表回退） | 0.0% |
| 补数据集后（3+3 独立任务） | **11 条** | **79.9%** | **53.3%** |

补数据集后的完整结果（`experiments/real_model_20261109/`，Qwen3 4B，1080 run / 1058 次模型调用）：

| 测试集 | A 无恢复 | B 固定规则 | C 不验证全放行（127 条） | D 完整闭环（11 条） |
|---|---|---|---|---|
| 单故障 204 例 | 0.0% | 22.1% | 82.8% | **79.9%** |
| 复合故障 30 例 | 0.0% | 0.0% | 0.0% | **53.3%** |
| 难度组 36 例 | 22.2% | 44.4% | 75.0% | **69.4%** |

三条必须一起读：

- **复合故障上 D 第一次真正领先**：53.3% vs C 0%。C 的 127 条技能把两步链挤到了轮不到的位置
  （第 1 轮优先单步技能，链长 1 就结束），D 的 11 条里含链，才有 53.3%。
  查库可核实：D 的恢复记录里 **54/280 次带 `skill_id`**（其余退回规则表），C 是 262/262 全带。
- **单故障上 C 82.8% > D 79.9%**：宽松库覆盖更广、代价是复合故障归零。这不是「不验证更好」，
  是「广撒网 vs 精确」的取舍，两边各有 30 个百分点的得失。
- **和更早那份 97.1% / 100% 不可比**：那是门禁前直写库（38 条、不校验评估）跑出来的，
  数据集也不是现在这批。旧数字只能当旧条件下的结果引用。
- **难度组 D 69.4% 是规则表的成绩，不是学到的技能**：把难度组训练任务补到 9 个
  （3 训练 / 3 target / 3 guard）之后，13 个硬组候选的 `gain=None` 消失了，但**增益全是 0** ——
  `retry` / `fallback_tool` / `replay_checkpoint` 这些策略规则表里本来就有。
  硬组崩在「4B 不会多步算术、不比文档版本」上，那是模型能力，不是恢复层缺策略。

### 2026-11-10 重跑：补上第二轮评测，并验证「库指纹必须登记」

用当前代码（配置指纹 `8fe5db47…`，与 11-09 **完全相同**）跑了两遍 1080 run：

| 运行 | 技能库 C / D | 库指纹 | 单故障 D | 复合 D | 难度组 C |
|---|---|---|---|---|---|
| 11-09 冻结 | 127 / 11 | `d6dea8be…` | 79.9% | 53.3% | 75.0% |
| **11-10 第二轮库** | 127 / 11 | `5b6b8495…` | **79.9%** | **53.3%** | **75.0%** |
| 11-10 data 旧库 | 41 / 6 | `afac4ecf…` | 81.4% | 73.3% | 69.4% |

产物：`experiments/real_model_20261110_r2/`（正式，补上第二轮）、
`experiments/real_model_20261110/`（旧库对照）。

三条结论：

1. **第二轮训练产物（11-09b）的库指纹与第一轮不同，但结果与冻结评测逐项一致**——
   说明「结构相近」的两套库在这批任务上行为等价；不能因此说两者是同一份库。
2. **换成 `data/` 里的旧库（41/6）后结果明显不同**（复合 53.3% → 73.3%）——
   **技能库内容变了，指标就变**。所以 manifest 必须登记库指纹，只写「C/D」不够。
3. **配置指纹三轮完全一致**，说明本轮代码改动（S01 契约、审批门禁、工具契约）
   不在这条评测路径上；但这**不能**推广成「任何改动都不影响结果」。

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

### 标注集：目前只有「回归护栏」，没有准确率

用**执行时的注入状态**当 ground truth，生成 37 条标注轨迹（clean 12 / failed 17 / recovered 8），
导出成 OTLP 后走与检查别人完全相同的代码路径。

| 口径 | 检出 | recall | 误报 | precision |
|---|---|---|---|---|
| 确诊（error 级） | 7/17 | 0.412 | 0/20 | 1.000 |
| 提示（warn 及以上） | 7/17 | 0.412 | 2/20 | 0.778 |

```bash
python scripts/eval_labeled.py                    # 默认写 experiments/labeled_eval_v2，不覆盖冻结历史
python scripts/eval_labeled.py --out /tmp/labeled # 显式隔离输出
```

**这不叫准确率**：样本是本仓库自己跑出来的、标签来自注入状态、标注者就是规则作者。
漏掉的 10 条全是「内部自洽但结论错」（静默篡改 / 陈旧数据 / 计划顺序），
需要外部真值才判得出。口径与边界写在 [docs/labeling-protocol.md](docs/labeling-protocol.md)。

另外 `eval_labeled.py` 把**轨迹实际用到的工具**填进 `allowed_tools`（只为消除结构评测的
无配置噪声），所以它**不能用来判断越权**：未经授权但已执行的工具会被自动视为允许。
权限专项必须从事先冻结的 Agent 策略取白名单。

> `experiments/labeled_eval/`（末尾仍写 49 条）与 `experiments/p0_09_20261109/assurance/`
> 是冻结历史原件，保留不动；当前版本报告在 `experiments/labeled_eval_v2/` 与
> `experiments/assurance_v2/`。

### 评测质量与 CI 策略（Q01–Q05）

| 任务 | 落地 | 关键结果 |
|---|---|---|
| Q01 三种真值 | `independent_labels.py` 分开存执行状态 / 可观察故障 / 业务结果 | 37 条全标 `pending_independent_review` |
| Q02 独立授权策略 | `scripts/eval_permission.py` 只用冻结白名单，缺策略标不可评 | 冻结策略检出越权 **2**，反推白名单 **0**（反例成立） |
| Q03 外部轨迹与独立标注 | 协议 + 待复核清单 + Cohen's kappa 函数 | 外部轨迹 0、独立人工标注 0、kappa `null` |
| Q04 按维度覆盖 | `assurance.py` 每维各自算 `coverage_ok` | 安全缺白名单只压 security，不牵连可靠性 |
| Q05 CI 发布策略 | `ci_policy.py` + CLI `--policy` | 报告判定与流水线阻断分文件保存 |

```bash
python scripts/eval_permission.py
python scripts/build_external_label_registry.py
cd ai-service && python -m evoguard_inspect.cli \
  --trace ../tests/fixtures/trace_attack.json --assurance \
  --policy ../tests/fixtures/ci_policy.json --out ../experiments/assurance_ci_v1
```

`ci_decision.json` 里 `report_verdict` 与 `ci_action` 分开：`review` 退出码 0
但明确需要人工处置，**退出码 0 不等于全面安全**。协议见
[docs/external-labeling-protocol.md](docs/external-labeling-protocol.md) 与
[docs/ci-policy.md](docs/ci-policy.md)。

**如实标注**：没有外部真实业务轨迹、没有独立标注者，这些数字仍不能叫准确率；
CI / 可运行性阈值仍是工程经验值；现已登记版本、开发集分布与指纹
（`config/thresholds_v1.json`），但**尚未用独立开发集标定**。

### 条件决策技能（阶段 D / E01 骨架）

`ai-service/evoguard/conditional_skill.py`：技能只有八项声明式字段，
模型提出候选后由外部执行器校验 schema 与权限。

- 禁止字段（含嵌套）与代码特征：不得改评测标准 / 审批状态 / 系统策略 / 允许工具集合，
  **先不生成任意 Python 代码**。
- `allowed_tools` 必须是冻结策略子集、不得声明写工具；执行前二次校验。
- 确定性执行器只按 `max_version` / `declared_order` / `first` 选证据，
  不调用工具、不执行代码；缺证据或超步数走失败回退。
- `paired_check()` 做同 task/seed 前后配对：业务收益**不能抵消**未授权操作，
  没有稳定收益允许拒绝晋升并发布负结果，零分母给 `None`。

```bash
python -m unittest tests.test_conditional_skill -q
```

协议与公平消融安排见 [docs/conditional-skill-protocol.md](docs/conditional-skill-protocol.md)。
**未做**：四组任务家族、真实 guard 数据集、完整公平消融与回滚证据、
E03 经验适用范围与检索改进；没有重跑 1080 run。

### 安全层：三层防护，默认只检测不改写

攻击面有三条口子：**用户输入**、**工具返回的外部内容**、**Agent 吐给浏览器的输出**。

| 层 | 位置 | 做什么 |
|---|---|---|
| 定义层 | 静态规则（20 条） | prompt 有没有拒答边界、有没有声明「工具结果是数据不是指令」、写操作有没有审批、有没有危险 shell |
| 行为层 | 轨迹规则 security 组（6 条）+ 运行时 6 个执行点 | 注入指令、白名单外工具、外带、参数里的凭据、破坏性命令、答案里的 XSS；`enforce` 下在**执行前**拦参数与越权工具，写操作必须审批 |
| 边界层 | `api/app.py` | `nosniff`、`X-Frame-Options: DENY`、CSP、按落点转义（`encode_for`） |

```bash
# 只看安全相关的发现
cd ai-service && python -m evoguard_inspect.cli --trace t.json --group security

# 运行时拦截（默认 audit：只记账不改内容，保证历史实验可比）
AGENTLAB_SECURITY_MODE=enforce python ai-service/api/app.py
```

**检测建议 ≠ 拦截成功（S01 契约）**：`enforce` 且未开 `AGENTLAB_SECURITY_WRAP` 时，
`guard_tool_output` 会返回 `action="blocked"`，但正文逐字未改、调用也没停 ——
旧演示把它算成「4/4 拦截」。现在每个 guard 都返回
`detected / decision / attempted / executed / effect / reason`，
真实执行点另落 `security_effects` 表；只有 `executed=True` 且 `effect` 有实际后果
才算「拦住了」，`action` 降级为检测建议。
详见 [docs/security.md](docs/security.md)，反例回归见 `tests/test_security_contract.py`。

**检测能力自己长（第二级模型 + 两段门禁）**：正则在外部语料上只认字面话术。
用**没通过**的样本训练字符 n-gram 模型做第二级，只在一级没命中时启用：
`--by payload` 划分下 test 召回 0.500 → **0.961**，但独立复核误报 **6.0%**
（换业务域 33.3%），超过 5% 发布线 → **门禁判定暂缓发布，默认关闭**。
开启方式与全部数字见 [docs/detector-evolution.md](docs/detector-evolution.md)。

**防御演示按真实后果计量**：四档（off / audit / enforce / enforce+wrap）×
四类攻击 + 各自的合法业务对照，分别统计未授权调用是否执行、模拟接收方是否命中、
危险操作是否执行、输出是否仍可被当代码、任务是否完成。`FakeSink` 只计数不联网，
`dangerous_executed` 恒为 False（演示本身拒绝执行任何命令）。

```bash
python scripts/defense_demo.py                     # 默认写 experiments/defense_demo_v2
python scripts/defense_demo.py --followup real     # 可选：本地真实模型驱动后续动作
```

默认后续动作是**确定性最坏情况观测器，不是真实模型**；攻击目标达成率只在该受控
执行模型下成立，报告把真实模型复核标为 N/A。旧 v1 的 `enforced=4 / leaked=3`
是已知高估与虚报，冻结原件留在 `experiments/defense_demo/`，不再引用。

### 四模式对照：先冻结配置，再看逐 case 结果

只记 `mode` 一个字段解释不了结果：实测同一个 `enforce`、`AUTO_APPROVE=True`，
仅 `AGENTLAB_ALLOW_AUTO_APPROVE` 从 0 改到 1，恢复期写操作就从
`E_APPROVAL_REQUIRED` 变成真的落库。`scripts/freeze_mode_compare.py` 把全部有效开关
（mode / wrap / auto_approve / allow_auto_approve / 工具白名单 / 工具契约 /
输出落点 / 威胁模型版本 / 后续决策器）写成规范化配置并算 sha256 指纹，
再跑四档模式，产出**逐 case 结果、明确分母**与 `manifest.json`
（含攻击集 / 正常集内容指纹、独立授权策略、代码指纹）。

| 模式 | 业务成功 | 攻击目标达成 | 真实阻断率 | 正常误阻断率 |
|---|---|---|---|---|
| off | 12/12 | 5 | 0.0 | 0.0 |
| audit | 12/12 | 5 | 0.0 | 0.0 |
| enforce | 8/12 | 0 | 1.0 | 0.0 |
| enforce+wrap | 8/12 | 0 | 1.0 | 0.0 |

⚠️ `enforce` 下业务 12/12→8/12 的四条下降**全部是攻击场景的工具调用被拒**，
正常误阻断率仍是 0.0，不是「防护损失了 25% 业务」。审批与资源成本在演示引擎里
拿不到，一律记 N/A 不写 0；审批开关的真实影响由真实 runtime 子实验单独验证。

```bash
python scripts/freeze_mode_compare.py          # 默认写 experiments/mode_compare_v2
```

**12 条 case**（6 场景：6 攻击 + 6 合法对照，6 个独立攻击向量组）已达到计划 §5
的 12 个工程冒烟起点；新增的两组是**参数里的假凭据 / 外带 URL**，不是改措辞。
仍未扩到「≥30 独立攻击 + ≥30 正常」，如实标注。口径与边界见
[docs/mode-compare-protocol.md](docs/mode-compare-protocol.md)。

### 审批状态机与真实副作用闭环（会话 5/6）

`ai-service/evoguard/approval.py`：`approval_request` 绑定 run_id / 工具 /
规范化参数摘要 / 资源范围 / 候选版本 / 24 小时有效期 / 批准人 / 一次性消费状态；
暂停存检查点，批准后从原检查点续跑。参数变化、权限变更、过期、拒绝、重复消费
一律拒绝并留原因。状态全在 SQLite，进程重启后重新打开即可恢复。

`ai-service/evoguard/real_tasks.py`：127.0.0.1 本地 HTTP 读测试订单 →
项目沙箱内规则文件 → 按规则算总额 → 过统一门禁 → 专用 SQLite 结算表（幂等键）。
`runtime.Ctx` 挂上 `approval_gate` 后，**首次写入与恢复期重放写入走同一个执行门禁**；
默认不挂，历史实验行为与冻结数字不变。

| 计划「必须测试的情形」 | 结果 |
|---|---|
| 首次写入没有审批 | 通过：零落库副作用，状态可定位到 request + 检查点 |
| 审批前后参数变化 | 通过：摘要不匹配，原审批不复用且未被消费 |
| 提交成功但响应丢失 | 通过：同一幂等键重试仍只有一笔结算 |
| 拒绝与过期 | 通过：任务不自动继续，事件有原因 |
| 暂停后进程重启 | 通过：检查点与批准状态恢复，不重复写入 |

```bash
python scripts/real_settlement_demo.py     # 默认写 experiments/real_settlement_v1
```

边界：订单是回环测试服务、规则在项目沙箱内、结算只写专用表；不碰外部账号、
真实支付、任意 shell 执行。`sim` 保留作快速回归。这不是生产级审批工作流
（无鉴权 / 多租户 / 限流）。详见 [docs/approval-protocol.md](docs/approval-protocol.md)。

**延迟**：1 KB 0.044 ms / 10 KB 0.31 ms / 50 KB 1.84 ms；真实运行每次约 **51 µs**，
相对真实模型的 627 ms 是 **0.008%**（`experiments/p0_09_20261109/security_bench.json`）。

**审批是真执行的，沙箱不是**：`enforce` 下恢复期重放写操作必须人工审批，否则中断恢复。
代价实测：单故障 D 从 86.3% 降到 59.8%（-26.5pp）、复合链 86.7% → 46.7%（-40pp），
默认 audit 模式里「审批被默默放行」记了 825 次（`mode_compare.json`）。
**沙箱仍然没有**：危险命令能在调用前被拦，但真要跑 shell 工具时没有隔离层。

**必须说清楚的边界**：特征是匹配出来的，改个说法就能绕过；它是「发现 + 降风险」，不是「防住」。
详见 [docs/security.md](docs/security.md)。

### Word 计划剩余任务落地（§5 / §10 / E02 / E03）

| 任务 | 交付 | 关键结果 |
|---|---|---|
| §5 先改权限结构 | `ai-service/evoguard/tool_contract.py` | 域名 / 资源 ID / 可写路径 / 最大金额 / 方法 / 副作用由契约校验，正则只作信号。反例：正则零命中但域名越界 → `deny`；正则命中但契约允许 → `allow` |
| §5 12 个工程冒烟场景 | 6 场景 / 12 条 case / 6 个独立攻击向量组 | 新增参数假凭据、参数外带 URL 两个新入口，不是改措辞；四模式对照已按 12 重算 |
| E02 独立家族增量 | `scripts/conditional_skill_eval.py` | train/dev/validation/test 四组，同 task/seed 前后配对；validation gain 0.167、test 0.25；无收益候选发布负结果，越权候选 REJECT |
| E03 经验适用范围 | `ai-service/evoguard/experience_scope.py` | 工具契约/数据版本/权限/有效期/撤销先结构过滤，再 BM25；反例：文本最像但已撤销的记录不进候选 |
| §10 公平消融 | `scripts/ablation_v1.py` | 六组单变量消融 13 配置 + 审批×隔离 2×2 交叉 4 配置；八项业务与防护指标同时报告；统一 manifest 登记 |

```bash
python scripts/tool_contract_demo.py
python scripts/conditional_skill_eval.py
python scripts/experience_scope_demo.py
python scripts/ablation_v1.py
```

协议见 [docs/tool-contract.md](docs/tool-contract.md)、
[docs/ablation-protocol.md](docs/ablation-protocol.md)、
[docs/conditional-skill-protocol.md](docs/conditional-skill-protocol.md)。

**如实标注**：E02 仍是确定性构造集（不是真实模型/线上流量），不给置信区间；
E03 未加 embedding；公平消融不是 1080 run 历史评测的重跑，
不能据此声称「验证门禁的因果收益已证明」；仍未扩到 30 攻击 + 30 正常。

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
│   ├── test_inspect.py      44 个测试（规则全命中 / 零误报 / 配置适配 / 事故模式 / 覆盖率与退出码）
│   ├── test_static.py       27 个测试（静态规则 / 框架写法适配 / 低置信度噪音治理 / 覆盖率）
│   ├── test_skill_validation.py 36 个测试（成对负迁移 / 样本充分性 / 晋升门禁 / 严格库链路）
│   ├── test_security.py     34 个测试（注入/越权/外带/凭据/XSS、误报与延迟预算、audit vs enforce、审批真执行）
│   ├── test_assurance.py    8 个测试（三维判定 / 覆盖有限不算通过 / 报错后仍有答案 / 不给分数）
│   ├── test_labels.py       15 个测试（指标算法 / 空壳答案 / 中文改写 / 重放循环 / 标注集完整性）
│   ├── test_security_contract.py 8 个测试（S01 反例 / 六字段契约 / 真实执行点留痕）
│   ├── test_defense_demo_v2.py 12 个测试（按后果计量 / 不虚报外泄 / 正常对照 / 输出隔离）
│   ├── test_docs_consistency.py 12 个测试（当前口径 / 冻结历史不被改写）
│   ├── test_mode_compare.py 15 个测试（审批开关反例 / 配置指纹 / 分母与 N/A / 冻结历史）
│   ├── test_approval.py     15 个测试（5 个必须验收情形 / 统一门禁 / 进程重启 / 幂等）
│   ├── test_permission_and_ci.py 21 个测试（Q01–Q05：权限反例 / kappa / 维度覆盖 / CI 策略）
│   ├── test_conditional_skill.py 21 个测试（声明式 schema / 越权拒绝 / 确定性执行 / 配对验证）
│   ├── test_tool_contract.py  12 个测试（结构化授权 / 正则只作信号 / 反例）
│   ├── test_e02_e03.py        17 个测试（四组家族 / 负结果 / 越权 / BM25 / 撤销反例）
│   ├── test_ablation.py       12 个测试（六组消融 / 单变量 / 八项指标 / manifest）
│   ├── test_edge_cases.py     34 个测试（边界与非法状态迁移 / off 档不判 / 待批票复用 / 账本冲突）
│   ├── test_guard_fuzz.py      8 个测试（固定 seed 模糊 / 六字段契约恒成立 / effect 不许虚报）
│   ├── test_untrusted_input.py 14 个测试（技能候选 / CI 策略 / 契约参数的畸形输入）
│   ├── test_reproducibility.py 8 个测试（同配置重跑逐字节一致 / 汇总等于逐条之和）
│   ├── test_safe_extract.py   11 个测试（路径穿越 / 盘符与反斜杠 / 类型白名单 / 体积上限）
│   ├── test_api_smoke.py      14 个测试（真起 HTTP 服务：接口 / 静态穿越 / 安全响应头 / 参数校验）
│   ├── test_external_attacks.py 13 个测试（语料清单 sha / 口径钉死：PII 不算注入 / 离线报错）
│   ├── test_ci_effects.py     10 个测试（副作用证据接进 CI：判拒绝却仍执行→阻断）
│   ├── test_deep_scan.py      36 个测试（分组无泄漏 / 最小对照 / 归一化 / 两段门禁 / 落点限制）
│   ├── test_real_followup.py  11 个测试（真实模型结构化决策 / 失败记 N/A / 复核覆盖不夸大）
│   ├── test_jailbreak_prompts.py 10 个测试（直接越狱入口 / 口径 / 二级模型不适用）
│   ├── test_agentdojo_adapter.py 12 个测试（AST 不执行源码 / 组合样本口径 / 召回并列误报）
│   ├── test_final_expansion.py  17 个测试（阈值版本 / 多 seed / 跨 Agent / 30+30 独立性 / 任务格）
│   ├── test_final_round.py      24 个测试（语义开关 / 缓存与降级 / E02 门禁 / kappa / 标定 / 中文）
│   └── test_llamafirewall_merge.py 38 个测试（不可见字符绕过与等价性 / 角色流水线 / 轨迹回放 / 目标对齐 / 代码检查 / 意图门禁）
├── scripts/check_model.py   本地模型就绪探针
├── scripts/freeze_mode_compare.py  四模式配置冻结 + 逐 case 对照
├── scripts/real_settlement_demo.py 5 个审批与结算验收情形
├── scripts/eval_permission.py        Q02 权限专项（冻结策略）
├── scripts/build_external_label_registry.py  Q01/Q03 待复核清单
├── ai-service/evoguard/approval.py     可恢复审批状态机 + 幂等结算账本
├── ai-service/evoguard/real_tasks.py   本地 HTTP 订单 + 沙箱规则 + 结算链
├── ai-service/evoguard_inspect/independent_labels.py  三种真值 + kappa 协议
├── ai-service/evoguard_inspect/ci_policy.py           CI 发布策略
├── ai-service/evoguard/conditional_skill.py           声明式条件决策技能 + 配对验证骨架
├── ai-service/evoguard/tool_contract.py               §5 结构化工具契约授权
├── ai-service/evoguard/experience_scope.py            E03 经验适用范围 + BM25
├── scripts/tool_contract_demo.py          §5 契约反例演示
├── scripts/conditional_skill_eval.py      E02 四组家族配对验证
├── scripts/experience_scope_demo.py       E03 检索反例演示
├── scripts/ablation_v1.py                 §10 六组公平消融
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
ollama serve &          # 按本机安装位置启动；不要写死盘符或模型目录
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

## 相关工作与引用

- **AgentDojo**（Debenedetti et al., NeurIPS 2024 Datasets & Benchmarks）——
  97 个用户任务 + 629 个安全用例，间接注入的公开基准；本项目下一步的外部评测来源。
- **InjecAgent** —— 1K API 调用用例，测「注入诱导 Agent 调用恶意 API」。
- **Agent Security Bench (ASB)**、**WASP**（Meta）—— 更广的工具面与 Web Agent 攻击面。
- **OWASP Top 10 for LLM Applications**（LLM01 Prompt Injection）；
  **OWASP Top 10 for Agentic Applications**（2025-12）。
- **NIST AI 100-2e2025**（对抗性机器学习分类法）；**MITRE ATLAS** `AML.T0051`。
- 红队工具：**garak**（NVIDIA）、**PyRIT**（Microsoft）、**promptmap**、**Gandalf**（Lakera）。

本项目的定位与这些基准一致：**测的是「在这组任务与配置下的可观察后果」，
不是通用安全能力**。外部基准的 ASR / UA 不能跨环境直接比大小。

---

## 已知限制（别当成能力）

1. **验证集仍偏小**：dev 已按 3 / 3 / 3 切成 train / target / guard，但每个 (agent, fault)
   不一定都有 ≥3 条独立任务；kb 的部分故障在 guard 上基线成功率仍不足。
   一次 before/after 就决定晋升仍有抛硬币风险 —— 这条比任何算法细节都更能解释早期 97.1%。
2. **模型层没被测到**：任务是抽取式的，grammar 约束清掉格式错误，真实模型 936 次调用 0 次错答。
   要测模型的推理可靠性，得把任务难度提上去。
3. **故障是构造出来的**，不是真实事故里自然共现的错误；复合故障证明的是「机制上限」，
   不是「真实分布下的收益」。
4. **策略空间**：旧技能仍是 6 种策略的固定词表；E01 加了受限声明式条件技能
   （适用条件 / 必需证据 / 允许工具 / 确定性参数 / 停止条件 / 失败回退），
   但只覆盖「按文档版本选证据」「按声明顺序组合规则」两个问题族，且**明确不生成代码**。
5. **真实工具链只接了一条**：本地订单核对（127.0.0.1 回环 HTTP + 沙箱规则文件 +
   专用 SQLite 结算表），没有通用工具链、真文件读写、DB 错误注入或限流；
   `sim` 仍是默认快速回归路径。
6. **流程不变量有盲区**：纯执行型流程（步骤里没有启动/收尾动作词）检测不到顺序颠倒。
7. **检索**：E03 已做「结构过滤 + BM25」（纯标准库、中文 2-gram），仍无 embedding /
   向量检索 / rerank；相似度不能替代可信性，撤销 / 过期 / 版本 / 权限先于打分剔除。
8. **负迁移只在两个维度上被挡住**：`guard` 覆盖「同 agent 不同故障」，
   `scope` 覆盖「同故障但单/复合」，没有单独的量化指标；批量投毒还没做。
9. **多 seed 只在确定性仿真上做过**（5 个 seed、成功率标准差 0，属设计结果）；
   真实模型随机性仍未量化。
10. FastAPI / PostgreSQL / Vue / Java / Docker / OpenTelemetry 一律未做。

---

## 下一步

### 已完成（保留记录，不再重复开发）

- [x] 让 C 走自己的放宽验证流程重训技能库 —— C/D 第一次拉开差距
- [x] 技能作用域：复合链不再占单故障键位（`scope` + 回归测试）
- [x] 真实模型全量四组评测（含模型版本 / Prompt 版本 / 代码指纹记录）
- [x] 数据划分改为 train / target / guard 三组 3/3/3，test 全程不参与
- [x] 配置冻结 + 四模式逐 case 对照（`scripts/freeze_mode_compare.py`）
- [x] 可恢复审批状态机 + 本地 HTTP / SQLite 幂等闭环（`approval.py` / `real_tasks.py`，
      5 个必须验收情形全过）
- [x] 独立授权策略 + 权限专项评测（Q02，`scripts/eval_permission.py`）
- [x] 三种真值分开存 + 待复核清单 + kappa 协议（Q01 / Q03 协议部分）
- [x] 按维度覆盖 + CI 发布策略分离（Q04 / Q05）
- [x] 声明式条件技能 + 四组家族配对验证（E01 / E02 骨架）
- [x] 经验适用范围 + 结构过滤 + BM25（E03，`experience_scope.py`）
- [x] 六组公平消融 + 统一 manifest 登记（§10，`scripts/ablation_v1.py`）
- [x] 结构化工具契约 + 12 条工程冒烟 case（§5）
- [x] 严格 2×2 消融（审批 × 内容隔离，17 个配置，§10）
- [x] 第二轮真实模型 1080 run 全量评测（11-10，配置指纹一致、结果逐项相同）
- [x] 外部语料接入：InjecAgent 2108 条 + 直接越狱 1998 条 + AgentDojo 10 模板
- [x] 30 个独立攻击向量 + 30 个相似合法对照（`expanded_vectors_v1.py`，
      独立性用 (入口 × 手法) + 字符 3-gram Jaccard < 0.85 校验）
- [x] 每格 ≥3 条独立任务：102 个 (agent × fault × difficulty) 格子全部达标
- [x] 阈值版本登记 + 开发集分布 + 指纹（`config/thresholds_v1.json`）
- [x] 跨 Agent 迁移实验：架构**不允许**直接迁移，模拟迁移判定 REJECT（见 stability_v1）
- [x] 仿真多 seed（5 个 seed，标准差 0；不等于真实模型稳定）
- [x] 语义型检测换设计：本地模型第三级判定器（`AGENTLAB_SECURITY_SEMANTIC`，默认关），
      外部越狱召回 0.05 → 0.575、误报 0（详见 `docs/final-round.md`）
- [x] 真实模型 E02 四家族 + 真实模型多 seed（整体门禁 REJECT：dev 负迁移）
- [x] 阈值开发集标定（候选 0.15，未启用；留出集 F1 与默认相同，不宣称更优）
- [x] 双人 kappa 代理标注（kappa 0.325 / 分歧 58 条，**不是真人标注**）
- [x] 中文语料**真人单人标注**（60 条 vs 语料标签）：按入口分档后间接注入档
      一致率 0.878 / kappa 0.752；全体混合口径 0.467 只作「口径不能混算」的记录。
      见 `experiments/human_labels_v1/result.json`、`docs/attack-datasets.md` 第十节三。
- [x] 中文注入样本（30 条翻译生成，一级/三级在中文上召回接近 0，明确盲区）
- [x] 官方 AgentDojo 4 环境 harness 接通（各 1 任务；**不是榜单成绩**）
- [x] 融合 LlamaFirewall（MIT）的三类方法：不可见字符归一化（补上一个真实绕过）、
      按角色可插拔扫描器 + 轨迹回放、AlignmentCheck 目标对齐审查的本地版
      （见 [docs/llamafirewall-merge.md](docs/llamafirewall-merge.md)）

### 未完成（按前置条件分类，不是「再写点代码」就能做完）

需要**外部数据或人**：
- [ ] 可授权外部轨迹 ≥30 条 + 独立标注者 + 双人 Cohen's kappa。
      现在只有协议与待复核清单（`experiments/external_labeling_v1/`），
      37 条全是自生成样本，**不能当真实业务准确率**。
- [ ] 负迁移压力测试：批量投毒，测晋升门槛在不同投毒比例下的召回 / 误报。

需要**外部条件或人**：
- [x] **真人独立双标 + 标注者间 Cohen's kappa**（评审 §7 要求的那个数，已完成）：
      第二位标注者匿名盲标 60 条基础页 + **200 条加长批**，导出已回收。
      正式结果 `experiments/human_labels_v1/double_result.json`：
      两人交集 **60/200 条，一致率 0.717 / 标注者间 kappa 0.414**；
      60 条页对照 0.733 / 0.442；**注入档 41 条 0.854 / kappa 0.688 为主口径**。
      140 条只有第二位标，记入 `unpaired`、未混入分母。
      顶层被 `harmful_request` 口径差拉低（12 条里 11 条分歧），不是标注误差；
      详见 `docs/double-annotation-results.md`。**不是准确率、不是标注质量**。
- [ ] 可授权的真实中文攻击语料（翻译样本不能替代）。
- [ ] 官方榜单全量评测（需官方评测模型与 API；本地 harness 只跑了 4 个任务）。

需要**补实验设计**：
- [ ] 负迁移压力测试：批量投毒，测晋升门槛在不同投毒比例下的召回 / 误报。
- [ ] **自然语言间接注入**检测：三级语义在该类上召回仅 0.05，
      中文上更是接近 0；这是当前最大的盲区。
      `docs/semantic-injection-plan.md` 的**预注册实验 E1–E4 已执行**（2026-11-12）：
      **2 通过 / 2 未通过**，负结果照写；结果见
      `experiments/semantic_injection_v1/report.md`。
      E2（中文真实负例）与 E3（推理前字段过滤）都没达到预注册判定线，
      **盲区仍在**；这些是对照实验结论，不是已发布的检测能力。
      实验后联网复核修正了两处归因（评估口径错配、SFE 复现前提未满足），
      见 `docs/semantic-injection-external-review.md`；**结果、判定线、冻结集未改**。
- [ ] 阈值用真实业务开发集标定（当前合成集区分度不足，候选未启用）。
- [ ] 提任务难度：多步计算 / 跨文档推理，让模型真的会答错。

---

## 交接（给下一位）

- **入口模块**：`ai-service/evoguard/` 下的
  `runtime.py`（执行/审批门禁）、`security.py`（检测与 S01 契约）、
  `approval.py`（可恢复审批 + 幂等账本）、`real_tasks.py`（本地 HTTP/规则/SQLite）、
  `conditional_skill.py`（声明式技能）、`tool_contract.py`（结构化授权）、
  `experience_scope.py`（经验适用范围 + BM25）。
- **回归**：`python -m unittest discover -s tests -q` —— 当前 **814 项全绿**。
- **冻结证据（只读，不要覆盖）**：`experiments/p0_09_20261109/`、
  `experiments/real_model_20261109/` 与 `...20261109b/`、`experiments/defense_demo/`、
  `experiments/labeled_eval/`。当前版本产物在 `*_v1` / `*_v2` 目录，
  用脚本 `--out` 重新生成。
- **改安全开关必须同时更新 manifest**：威胁模型版本、攻击/正常集指纹、
  独立授权策略、mode / wrap / auto_approve / allow_auto_approve、工具契约、输出落点。
  历史真实模型 metrics 没登记这些开关，**不能事后补猜成某种安全模式**。
- **文档索引**：`docs/progress.md`（逐阶段完成事项与证据）、
  `docs/security.md`（检测与 S01 契约）、`docs/approval-protocol.md`、
  `docs/tool-contract.md`、`docs/mode-compare-protocol.md`、
  `docs/external-labeling-protocol.md`、`docs/ci-policy.md`、
  `docs/conditional-skill-protocol.md`、`docs/ablation-protocol.md`、
  `docs/attack-datasets.md`；`docs/plan-alignment.md` 只对 11-08 旧计划有效。
- **三条铁律**：未改正文、未停止调用不算拦截成功（S01 契约）；
  业务收益不能抵消未授权操作（E02）；相似度不能替代可信性（E03）。

