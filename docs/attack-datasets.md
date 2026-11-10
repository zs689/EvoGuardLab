# 外部攻击数据集与 Agent 测试集：候选清单与接入方案

> 状态：**调研完成，尚未接入**（2026-11-10）。
> 本文只回答「用哪些、怎么接、注意什么」，不说已经跑过。
> 许可状态凡未核实的一律标「待核实」，不要当结论引用。

## 一、为什么必须用外部数据

现有 12 条 case 全是自己构造的：攻击手法是我能想到的，不是真实攻击者用过的。
计划 §5 要求「≥30 独立攻击场景 + ≥30 正常场景」，靠改措辞凑数没有意义。
外部基准的价值在于：**攻击样本来自别人的红队、任务来自真实业务场景、指标有公开基线可比**。

## 二、候选基准（按覆盖度排序）

| 基准 | 覆盖攻击面 | 规模 | 接入方式 | 许可 |
|---|---|---|---|---|
| **AgentDojo**（ETH Zurich） | 间接提示注入：工具返回里藏指令，4 个环境（banking / Slack / travel / workspace） | 97 用户任务 + 629 安全用例 | `pip install agentdojo`；自带 benchmark 脚本 | 待核实（以仓库 LICENSE 为准） |
| **InjecAgent**（UIUC） | 直接/间接注入诱导调用恶意 API，ReAct 风格 Agent | 1K API 调用用例 | HuggingFace 数据集 | 待核实 |
| **Agent Security Bench (ASB)** | 10 类场景、400+ 工具，含隐私窃取/破坏/越权 | 数百用例 | 仓库 + 数据 | 待核实 |
| **WASP**（Meta） | Web Agent：网页里的注入，需自主导航 | 82 样本 | 仓库 | 待核实（可能限研究用） |
| **CyberSecEval**（Meta） | 注入、 insecure code、cyberattack 辅助 | 多套 | 仓库 | 待核实 |
| **Gandalf**（Lakera） | 真实用户尝试过的越狱/注入话术 | 大规模众包 | HuggingFace | 待核实 |

工具链（不是数据集，可作红队补充）：
**garak**（NVIDIA，Apache-2.0）、**PyRIT**（Microsoft，MIT）、
**promptmap**（待核实）、**Gandalf**（在线）。

标准与分类法（写 README / 相关工作可引）：
OWASP Top 10 for LLM Applications（LLM01 Prompt Injection）、
OWASP Top 10 for Agentic Applications（2025-12）、
NIST AI 100-2e2025、MITRE ATLAS `AML.T0051`。

## 三、接入方案（不改现有数据结构）

原则：**数据不进仓库，适配器进仓库**。

1. 数据放 `data/attack_sets/<基准名>/`，`.gitignore` 已忽略 `data/*`，
   只在仓库里保留 `scripts/fetch_attack_sets.py`（下载脚本）和 `docs/attack-datasets.md`。
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

## 四、禁止事项（写进脚本断言）

- 不对外部真实服务做攻击演示；所有接收方是 `127.0.0.1:9` 或本地回环。
- 不把受限许可的数据复制进仓库；只放下载脚本与指纹。
- 不使用真实凭据；只用明确的假 key（如 `AKIAIOSFODNN7EXAMPLE`）。
- 不把外部基准的「攻击成功率」直接当成我们系统的成绩：环境、工具集、模型都不同。

## 五、接入后能补上哪些缺口

- 攻击样本从 6 个独立向量扩到数十个，且来自真实红队话术；
- 正常任务来自真实业务场景（banking / travel / workspace），误阻断率更有意义；
- 可与公开榜单的 ASR / UA 做**同环境对比**（不是跨环境比大小）。

## 六、待办

- [ ] 核实 6 个基准的许可，只接允许再分发/研究使用的。
- [ ] 写 `scripts/fetch_attack_sets.py`（下载 + 指纹 + 许可登记）。
- [ ] 写 AgentDojo 适配器，先跑 12 条子集，对比现有构造集。
- [ ] 把外部样本纳入 `freeze_mode_compare` 的四模式对照与统一 manifest。
- [ ] 报告里单列「外部样本 / 自构造样本」，不混成一个准确率。
