# 语义注入检测 v2：重新预注册（2026-11-12）

> 这是**新实验**。`experiments/semantic_injection_v1/frozen_split.json` 的判定线与结果
> 一律不动；本实验另建冻结集、另写判定线，跑完不改。
> 设计依据来自实验后的外部复核 `docs/semantic-injection-external-review.md`。
>
> **状态：已执行，总判定「未通过」**（2026-11-12）。结果见
> `experiments/semantic_injection_v2/report.md` 与本文 §八。
> §一–§七 的设计与判定线保持实验前原样，没有事后修改。

## 一、上一轮错在哪（外部材料指出）

- **E2**：把 hard negatives 放在「按业务域留出的、没见过的 schema」上做主口径，
  而 StackOne 原文明确它不泛化到未见 schema。口径错配 + 12 条剂量不足。
- **E3**：把 SFE 的「逐字段独立判 pass/drop」实现成了「拼接后整体一次打分」，
  复现前提就没满足；且 `score()` 的 `z/√n` 长度归一化让固定阈值失效。
- 结论：两个负结果都**不能**写成方法无效，要重做实验而不是改判定线。

## 二、融合什么、不融合什么

| 来源 | 融合 | 不融合 |
|---|---|---|
| StackOne SFE（一手全文） | 逐字段按形状过滤、结构字段不送分类器 | 0.7MB FastText 权重、connector 业务数据 |
| Meta Prompt Guard 2（模型卡） | 重叠分窗 + 取 max、报 AUC 与 FPR 预算下召回 | **gated Llama 4 Community License 权重 + transformers**（违反零依赖与许可边界） |
| DeFuse arXiv:2609.22510（一手 PDF） | max-pooling 误报随窗口数复合的机理、长度/预算感知阈值、报 AUC | 它的模型与数据 |
| The Guardrail Cries Wolf（Zenodo 摘要） | 末尾 payload 位置测试、分窗的误报代价必须同报 | 全文（PDF 直链 403，未取到） |
| NotInject / InjecGuard（MIT，ACL 2025） | 「良性但含触发词」的过度防御探针思路 | **数据集本体**：HF 官方端点 502、镜像 503，本次不可达；不伪造下载 |

检测器底座仍是既有 `deep_scan` 的 char-n-gram NB，**不换模型、不调它的权重**；
升级的是「怎么把文本送进打分器」和「按什么口径评价」。

## 三、数据与划分（新冻结集）

- 正/负：InjecAgent kept 1984 条，按 `attacker_instruction` **整组**划分；
  test 用没见过的 `FILLER_UNSEEN` 填充语构造严格最小对照。
- **calibration（只放干净样本）**：train 划分的对照 + 确定性合成的 hard negatives
  的 train 组；阈值只在这里标定。
- **留出 test**：冻结组里的注入文本 + 对照，只报数。
- **过度防御探针（不进 calibration）**：16 条正常用户请求 + 151 条中文干净样本
  + hard negatives 的 test 组。
- **长文本位置测试**：把注入 payload 埋在 >8000 字符的干净文档末尾，
  复现「截断扫描对末尾几乎全瞎」；这是 baseline `MAX_SCAN_CHARS=8000` 的真实边界，
  不是人为挑的弱基线。
- 合成负例按**模板组整组**划分，不按条随机切；全部确定性、无随机、无联网。

## 四、三组对照

| 组 | 做法 |
|---|---|
| `baseline` | 既有 `DS.classify` 整段打分（受 8000 字符截断） |
| `v2_window_only` | 只做重叠分窗 + max + 预算阈值（消融，看字段过滤贡献） |
| `v2_full` | 逐字段过滤 + 重叠分窗 + max + 按窗口数分桶的预算阈值 |

## 五、判定线（先写死，跑完不改）

主口径是**同分布**留出集；跨域另报、不作达标判据。

1. 留出集 **AUC ≥ baseline − 0.01**（阈值无关，防止靠挪阈值刷数）。
2. 留出集在 calibration 阈值下的 **FP ≤ 0.05**（沿用项目 5% 发布线）。
3. **长文本末尾 payload 检出率 ≥ baseline + 0.10**。
4. **过度防御探针 FP ≤ 0.05**。
5. 四条全满足才算通过；任一条不满足就写「未通过」及差距，
   **不删实验、不放宽线、不改冻结集**。同时报窗口数、丢弃比例与耗时代价。

## 六、明确不能说的事

- 不能写「达到 Prompt Guard 2 水平」：没有它的权重，只用了窗口与评价口径。
- 不能写「企业级检测能力」：底座仍是字符 NB，真实业务流量未验证。
- 不能把 calibration 上的阈值说成「在 test 上调好的」。
- 不能把探针里的中文自建集写成留出集成绩。
- 下载的公开数据集本次不可达，不能写成「已接入」。

## 七、产物

`experiments/semantic_injection_v2/{frozen_split.json,result.json,report.md}`、
`ai-service/evoguard/injection_scorer.py`、`scripts/run_semantic_injection_v2.py`、
`tests/test_injection_scorer.py`。

## 八、执行结果（实验后追加，判定线未改）

| 组 | 留出 AUC | 留出 FP | 末尾检出 | 过度防御 FP | 四条全过 |
|---|---|---|---|---|---|
| baseline | 0.9917 | 0.000 | **0.000** | **0.1477** | 否 |
| `v2_window_only` | **0.9947** | 0.000 | **1.000** | 0.1477 | 否 |
| `v2_full`（申报组） | 0.9593 | 0.000 | **1.000** | **0.009** | 否 |

**总判定：未通过**（`v2_full` 的 AUC 比 baseline 低 0.032，超过允许的 −0.01）。

如实记录的两点：

1. **确实修好了一个真实缺陷**：重叠分窗 + max + FP 预算阈值把「payload 埋在
   8000 字符之后」的检出从 0 拉到 1.0，且留出 AUC 不降反升、FP 守住 5% 预算。
   这是 `MAX_SCAN_CHARS` 截断造成的盲区，不是挑出来的弱基线。
2. **两个机制互相拖累**：逐字段过滤把过度防御误报从 14.8% 压到 0.9%，
   却让 AUC 掉约 0.03 —— 结构字段本身携带判别信号，一刀切全丢会损失排序质量。

按预注册 §五，**没有放宽任何判定线、没有拿 test 调策略、没有把未通过改写成通过**。
下一步若要继续，应另立预注册研究「按字段类型决定是否过滤」，而不是在本次结果上
改口径。`per_view_checks` 逐组判定留在 `result.json` 里，由
`tests/test_injection_scorer.py` 守门。

**局限**：公开数据集 NotInject（MIT）本次因 HF 官方端点 502 / 镜像 503 不可达，
未接入；过度防御探针用的是本地许可清楚的样本 + 确定性合成负例（1860 条，
按模板组整组划分），**不能**冒充真实业务分布。
