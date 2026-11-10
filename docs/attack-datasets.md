# 外部攻击数据集与 Agent 测试集：候选清单与接入方案

> 状态：**两套语料已接入**（2026-11-10 晚）：InjecAgent（间接注入）+
> `jackhhao/jailbreak-classification`（直接越狱），其余仍为候选。
> HuggingFace 官方域本机不可达，但 `hf-mirror.com` 可达，语料从镜像取。
> 已核实的许可写清楚，未核实的一律标「待核实」，不要当结论引用。
> 语料不入库（许可 + 体积），仓库只保留下载器、清单（逐文件 sha256）与评测报告。

## 一、为什么必须用外部数据

现有 12 条 case 全是自己构造的：攻击手法是我能想到的，不是真实攻击者用过的。
计划 §5 要求「≥30 独立攻击场景 + ≥30 正常场景」，靠改措辞凑数没有意义。
外部基准的价值在于：**攻击样本来自别人的红队、任务来自真实业务场景、指标有公开基线可比**。

## 二、已接入：InjecAgent（UIUC，ACL 2024 Findings）

| 项 | 内容 |
|---|---|
| 仓库 | `uiuc-kang-lab/InjecAgent` |
| 许可 | **MIT**（已核对仓库 LICENSE） |
| 取法 | 按 **git blob sha** 取 4 个文件，不用分支名（分支会动，sha 不会） |
| 语料 | 4 个文件共 2108 条端到端用例：`data_stealing` 1020 / `direct_harm` 1088；每个目标各有 base 与 enhanced 两个变体 |
| 入口 | `security.guard_tool_output(user_tool, tool_response)`，`audit` 档（只检测、不改内容、不停调用） |
| 对照 | 每条用例自带 `Tool Response Template`，把 `<Attacker Instruction>` 占位符换成一句正常评价，就得到**结构相同、未被投毒**的干净对照 |

命令：

```bash
python scripts/import_external_attacks.py   # 下载 + 核验 sha256 → data/external/（不入库）
python scripts/eval_external_attacks.py     # → experiments/external_attacks_v1/
```

结果（`experiments/external_attacks_v1/report.md`）：

| 指标 | 值 | 说明 |
|---|---|---|
| 注入特征命中率 | **0.500** | 只算 `injection_*` 类检测器 |
| 任意信号命中率 | 0.846 | 含 PII / 密钥 / 外带，会虚高 |
| 干净对照误报率 | 0.294 | 其中**注入类误报 = 0.000**，误报几乎全来自 `pii_email` / `pii_phone` |
| 只扫攻击句子的命中率 | **0.000** | 载荷单独看时一个都没命中 |
| base / enhanced 变体 | **0.000 / 1.000** | 这条差距是本节最重要的结论 |

怎么读这三个数：

- **基础版 0%、增强版 100%**：正则认的是**字面**注入指令（"ignore previous instructions"
  这类显式覆盖、角色切换、提示泄露话术），而基础版用的是自然语言请求
  （"请把我的门锁权限给 Amy"），一个关键词都不命中。所以「检测率 50%」不是
  能力中位数，而是**两种写法各占一半**造成的平均数。
- **只扫载荷 0%**：说明我们的注入检测器对「自然语言指令」完全无感；
  增强版之所以 100%，是因为它把显式覆盖话术写在了返回里。
- **干净对照上注入类误报 0%**：至少说明注入检测器不是见谁都喊；但 `pii_*`
  在干净内容上误报 29.4%，把 PII 命中当「攻击命中」会同时抬高能力与误报。

**能说的**：接入了 2108 条外部用例，链路可复现（sha256 可核），
注入检测对**显式**注入话术有效、对**自然语言**注入无效。
**不能说的**：这个数不是端到端攻击成功率，也不是「防住了提示注入」——
本仓库的 Agent 是规则式的，检测结果要接进执行控制才谈得上拦住。

## 三、已接入：直接越狱话术（补齐「用户输入侧」这一面）

`huggingface.co` 在本机不可达，实测 **`hf-mirror.com` 可达**（镜像站，200）。
Gandalf 官方数据集在 HF 上没有公开仓库，所以用两个许可清楚的公开语料替代：

| 语料 | 许可 | 规模 | 状态 |
|---|---|---|---|
| `jackhhao/jailbreak-classification` | **Apache-2.0**（已核对 README） | 1998 条：越狱 666 / 正常 1332 | **已接入** |
| `TrustAIRLab/in-the-wild-jailbreak-prompts` | **MIT**（已核对 README） | 666 条野生越狱（parquet） | 候选（内容与上面高度重叠，且需要额外依赖） |

入口是 `guard_input`，不是 `guard_tool_output` —— 两类攻击不能混成一个准确率。

```bash
python scripts/import_jailbreak_prompts.py
python scripts/eval_jailbreak_prompts.py     # → experiments/jailbreak_eval_v1/
```

结果（`experiments/jailbreak_eval_v1/report.md`）：

| 指标 | 值 |
|---|---|
| 越狱召回（任意信号） | **0.041** |
| 越狱召回（注入类信号） | 0.036 |
| 正常误报（任意信号） | 0.004 |
| 正常误报（注入类信号） | 0.000 |

**这是本轮最难看的数字，也是最有用的一条**：真实用户写出来的越狱话术
（角色扮演、编码、DAN 之类）几乎全是语义攻击，正则只认字面特征，
1998 条里只抓到 4.1%。所以「覆盖攻击手段」远没做到 —— 靠堆关键词补不上。

**第二级模型不适用于这个入口**：它在 16/16 条正常用户请求上全报注入
（学的是「像不像指令」），挂到 `user_input` 会把所有请求都拦掉。
见 `docs/detector-evolution.md`。

语料不入库，`data/external/` 已忽略；清单记录下载 URL、许可与原始 CSV sha256。

## 四、候选基准（尚未接入）

| 基准 | 覆盖攻击面 | 规模 | 接入方式 | 许可 |
|---|---|---|---|---|
| **AgentDojo**（ETH Zurich） | 间接提示注入：工具返回里藏指令，4 个环境（banking / Slack / travel / workspace） | 97 用户任务 + 629 安全用例 | `pip install agentdojo`；自带 benchmark 脚本 | **MIT**（已核对）；攻击是代码里的模板，需要适配器生成样本 |
| ~~**InjecAgent**~~ | — | — | — | **已接入，见上一节** |
| **Agent Security Bench (ASB)** | 10 类场景、400+ 工具，含隐私窃取/破坏/越权 | 数百用例 | 仓库 + 数据 | **仓库未声明许可 → 不接**（不能默认「公开即可用」） |
| **WASP**（Meta） | Web Agent：网页里的注入，需自主导航 | 82 样本 | 仓库 | 待核实（可能限研究用） |
| **CyberSecEval**（Meta） | 注入、 insecure code、cyberattack 辅助 | 多套 | 仓库 | 待核实 |
| ~~**Gandalf**（Lakera）~~ | 真实用户尝试过的越狱/注入话术 | 大规模众包 | HuggingFace | HF 上**没有官方公开仓库**；已用上方两套许可清楚的语料替代 |

工具链（不是数据集，可作红队补充）：
**garak**（NVIDIA，Apache-2.0）、**PyRIT**（Microsoft，MIT）、
**promptmap**（待核实）、**Gandalf**（在线）。

标准与分类法（写 README / 相关工作可引）：
OWASP Top 10 for LLM Applications（LLM01 Prompt Injection）、
OWASP Top 10 for Agentic Applications（2025-12）、
NIST AI 100-2e2025、MITRE ATLAS `AML.T0051`。

## 五、接入方案（不改现有数据结构）

原则：**数据不进仓库，适配器进仓库**。

1. 数据放 `data/external/<基准名>/`（已忽略），仓库只保留下载器、清单与报告。
   InjecAgent 就是这么接的：`scripts/import_external_attacks.py`。
2. 写适配器，把外部样本转成现有「场景」结构：
   ```
   {id, vector, goal（预注册违规目标）, attack:{tool,args|document|output}, normal:{...}}
   ```
   对应 `scripts/defense_demo.py` 的 `SCENARIOS` 与 `scripts/freeze_mode_compare.py`。
3. 指标映射：
   - AgentDojo `benign utility` → 我们的「正常任务完成率」
   - `utility under attack` → 「安全完成率」
   - `attack success rate` → 「攻击目标达成率」
   三者的分母必须写清；不要把 BU 与 UA 混成一个数。
4. 每个外部样本必须保留 `source` 与 `license` 字段，报告里逐条可追溯。
5. 先跑 12 个工程冒烟的等价子集，确认适配器正确，再扩到全量。

## 六、禁止事项（写进脚本断言）

- 不对外部真实服务做攻击演示；所有接收方是 `127.0.0.1:9` 或本地回环。
- 不把受限许可的数据复制进仓库；只放下载脚本与指纹。
- 不使用真实凭据；只用明确的假 key（如 `AKIAIOSFODNN7EXAMPLE`）。
- 不把外部基准的「攻击成功率」直接当成我们系统的成绩：环境、工具集、模型都不同。

## 七、接入后能补上哪些缺口

- 攻击样本从 6 个独立向量扩到数十个，且来自真实红队话术；
- 正常任务来自真实业务场景（banking / travel / workspace），误阻断率更有意义；
- 可与公开榜单的 ASR / UA 做**同环境对比**（不是跨环境比大小）。

## 八、待办

- [x] 核实许可并按 blob sha 接入 InjecAgent（MIT），逐文件 sha256 入清单。
- [x] 报告里单列「注入特征 / 任意信号 / 干净对照误报 / 只扫载荷」四个数，不混成一个。
- [ ] AgentDojo 适配器（许可 MIT 已核实；攻击在代码模板里，需要生成样本）。
- [ ] 把外部样本纳入 `freeze_mode_compare` 的四模式对照与统一 manifest。
- [x] 找到可达镜像 `hf-mirror.com`，接入直接越狱语料（Apache-2.0，1998 条）。
- [ ] 语义型越狱/注入的检测方案（当前召回 4.1%）：这属于设计变更，
      得先有「不靠关键词」的方案再动，不能拿这批测试集调正则。
- [ ] `TrustAIRLab` 的 parquet 需要额外依赖，且与现有 666 条高度重叠，暂不接。
- [ ] 对自然语言注入补检测手段（当前基础版 0%）：这属于设计变更，
      得先有「不靠关键词」的方案再动，不能拿测试集调正则。
