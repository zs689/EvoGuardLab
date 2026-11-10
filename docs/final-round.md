# 收尾轮：语义检测、真实模型、跨语言、标注、标定、官方 harness

> 2026-11-10 最后一轮。所有本地模型调用走 `qwen3:4b`（Ollama 原生 `/api/chat`，
> `think=false` + `temperature=0`），`ProxyHandler({})` 绕过本机 HTTP 代理。
> **慢但准**的路子：前两级没命中才进第三级，结果按文本 sha256 缓存，可复现。

## 一、语义型攻击检测换设计（不再堆正则）

`ai-service/evoguard/semantic_guard.py` + `scripts/eval_semantic_guard.py`：
本地模型当审计员，只回答一个窄问题（工具返回里是否夹带指令 / 用户请求是否在绕过规则）。
prompt 开发期定稿并登记 sha256，**评测样本只报数、不回头改 prompt**。

218 条样本、216 次模型调用、3 条 N/A；首次实测 p50 **804.5 ms**、
p95 8535 ms（与其它本地模型任务并发排队，不是模型本身延迟）。

| 来源 | 样本 | 一级正则 | 二级字符 | 三级语义 | 合并(OR) | 三级误报 | 合并误报 |
|---|---|---|---|---|---|---|---|
| 外部直接越狱 | 80 | 0.050 | N/A | **0.575** | 0.600 | 0.000 | 0.000 |
| 30 个独立向量 | 60 | 0.467 | N/A | 0.433 | 0.800 | 0.000 | 0.333 |
| InjecAgent base | 39 | 0.700 | 0.900 | **0.050** | 0.900 | 0.000 | 0.278 |
| InjecAgent enhanced | 39 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.316 |

结论（分工，不是一个检测器包打天下）：

- 正则擅长**内容特征**（密钥 / 命令 / XSS），语义判定擅长**语义攻击**
  （角色扮演 / 规则绕过）—— 越狱召回从 0.05 提到 0.575，且误报 0。
- 两者都漏 **自然语言间接注入**（InjecAgent base 上三级只有 0.05）：
  小模型把「请把权限给 Amy」当成普通数据。这是下一轮要攻的盲区。
- **默认链建议只保留一级 + 三级**：二级字符模型在正常文本上误报过高，
  并入 OR 会把误报一起放大（见 `docs/detector-evolution.md`）。
- 运行时开关 `AGENTLAB_SECURITY_SEMANTIC=1`（**默认关**，约 1s/条）；
  一级命中就不叠第三级；模型不可用 / 输出不可解析记 N/A，绝不猜。

## 二、真实模型上的 E02 四组家族 + 多 seed

`scripts/real_model_e02_v1.py`：4 家族 × 3 个不同 KB 任务 × 3 seed ×
「无技能 / 有声明式条件技能」= 72 次真实模型调用。技能由**外部执行器**注入
「按最高版本号选证据」，不是模型自己写策略。

| 家族 | 基线 | 带技能 | 增益 | 负迁移率 | 判定 |
|---|---|---|---|---|---|
| train | 0.667 | 0.667 | 0.000 | 0.000 | REJECT |
| dev | 1.000 | 0.333 | **-0.667** | 0.667 | REJECT |
| validation | 0.333 | 0.667 | +0.333 | 0.000 | PROMOTE |
| test | 0.000 | 0.333 | +0.333 | 0.000 | PROMOTE |

**整体门禁 REJECT（dev 负迁移）**：不能只挑 validation/test 的正增益报喜。
真实模型多 seed（7/11/23）：逐 seed 成功率 0.5/0.5/0.5、标准差 0、
12/12 任务跨 seed 稳定；N/A 3 条。严格 schema 修掉了小模型
「复述输入 JSON 当作答」的坑（旧版把空答案算成零增益、误判 PROMOTE）。

## 三、阈值本身用独立开发集标定

`scripts/calibrate_thresholds_v1.py`：120 个带标签开发集快照 + 60 个独立留出集
（不同随机源，只验收一次）。网格 0.05–0.50 按 F1 选优 → **候选 0.15**；
留出集上候选与默认 0.20 的 F1 都是 **1.0** —— 说明合成开发集区分度不足，
**不能据此判定候选更优**，所以候选配置 `config/thresholds_v2_calibrated.json`
标为「未启用」，默认 `thresholds-v1 / 20%` 不动，历史数字保持可比。

## 四、双人 Cohen's kappa（代理标注者）

`scripts/independent_labels_v1.py`：A=规则标注器（字面特征）、
B=本地语义判定器（意图），原理独立但**都不是真人**。218 条 →
**kappa 0.325、一致率见报告、分歧 58 条**，分歧清单留给人工讨论，
不自动取多数。真人双标协议仍缺人，不宣称标注质量达标。

## 五、中文注入样本（合成，非真实语料）

`scripts/chinese_injections_v1.py`：公开可达语料几乎全英文，
用本地模型把 InjecAgent 攻击句 / 越狱话术 / AgentDojo 模板逐句译成中文
（30 条全部翻译成功，N/A 0）。**明确标注为模型翻译生成，不能冒充真实中文攻击分布**。

| 来源 | 攻击样本 | 一级正则召回 | 三级语义召回 |
|---|---|---|---|
| InjecAgent 攻击句 | 8 | **0.000** | 0.000 |
| 外部越狱话术 | 8 | **0.000** | 0.125 |
| AgentDojo 模板 | 6 | **0.000** | 0.167 |

一级正则的英文关键词规则在中文上**全线失效**；英文 prompt 的语义判定器
迁到中文也基本失效。这是明确的跨语言盲区，不是「再补几条正则」能解决的。

## 六、官方 AgentDojo 4 环境端到端 harness

`agentdojo 0.1.35`（MIT）隔离装在 `_sandbox/agentdojo_pkg`
（`python -m pip install --target _sandbox/agentdojo_pkg agentdojo`，
不污染项目依赖，目录已被 `.gitignore` 忽略），
用 `--model LOCAL` + `LOCAL_LLM_PORT=11434` 接本机 Ollama，
跑通 `workspace / travel / banking / slack` 四个官方 suite，各 1 个
user task + 1 个 injection task、`--attack direct`。

| 环境 | utility | security |
|---|---|---|
| workspace | 0% | 0% |
| travel | 0% | 0% |
| banking | 0% | 100% |
| slack | 100% | 0% |

**这不是官方榜单成绩**：官方是 97 用户任务 + 629 安全用例、闭源前沿模型；
这里只有 4 个任务，且本地 4B 模型不按 harness 期望的工具调用格式作答。
security 高只说明它「没照做注入」—— 一个什么都没干成的 Agent 天然不会被攻击成功。
价值在于**链路已接通并留下可核对的官方产物**（`runs/` 下 8 个 json）。

## 仍然缺什么

- 真人独立双标 + 双人 kappa（需要人）。
- 真实中文攻击语料（需要可授权来源，翻译样本不能替代）。
- 自然语言间接注入的检测方案（三级语义在此仍只有 0.05 召回）。
- 官方榜单全量（需要官方评测模型与 API）。
- 阈值用**真实业务开发集**标定（当前合成集区分度不足）。

## 复现命令

```bash
NO_PROXY=127.0.0.1,localhost python scripts/eval_semantic_guard.py --limit 40
NO_PROXY=127.0.0.1,localhost python scripts/real_model_e02_v1.py
NO_PROXY=127.0.0.1,localhost python scripts/chinese_injections_v1.py --limit 8
python scripts/independent_labels_v1.py
python scripts/calibrate_thresholds_v1.py
python scripts/agentdojo_official_report.py
```
