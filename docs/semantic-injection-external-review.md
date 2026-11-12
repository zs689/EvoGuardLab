# 语义注入实验：外部方法复核（实验后补充，2026-11-12）

> 性质：**E1–E4 跑完之后**才做的联网复核。
> `frozen_split.json`、判定线、四个 `e*_result.json` 的指标值**一律未改**；
> 本文只修正对结果的**归因**，并给出下一步该重新预注册的实验。
>
> 为什么补这一轮：四个实验原本只依据项目自己的语料、代码和此前下载的
> CaMeL / StackOne 材料。外部一手资料显示，**我给 E2 和 E3 写的单因果归因
> 不充分**，属于闭门推理。这里如实登记。

## 一、核实到的材料（分清一手 / 二手）

| 来源 | 状态 | 关键事实 |
|---|---|---|
| StackOne, *Detection Isn't the Hard Part*（2026-05-12） | **一手，全文已抓取** | 940 条 connector payload：hard negatives 让 FP ~7% → ~1.7%，再叠 SFE → ~0.2%；SFE（FastText ~0.7MB）**逐字段**判形状，drop ~45% 字段；延迟 83ms → 45ms；OOD ToolACE ~0.9% FP、ChatML ~0.1% |
| DeFuse, arXiv:2609.22510v1（2026-09-18, Szczepaniak / Feldman / Viner / Nassi） | **一手，PDF 已下载校验**（`_sandbox/semantic_refs/defuse_2609.22510.pdf`，4,913,087 bytes，`%PDF-1.7` + `%%EOF`） | max-pooling 下文档级 FP = `1−(1−p)^N`；固定阈值下 20→2000 词 FPR 0.064→1.000、F1 0.9690→0.6667，而 AUC 仍 ≥0.9978；原文「F1 at a fixed threshold therefore measures **threshold placement**, not detection quality」；需要 length-aware thresholds |
| *The Guardrail Cries Wolf*（Zenodo 23540999, Shrestha 2026） | **页面摘要已核，全文未取到**（直链 403，已清除无效文件） | 1,528 benign / 6 channels；quoted analyst notes FP 100%；truncate-512 时 payload 在开头检出 83.3%、末尾 8.3%；阈值 0.9999 仍 88.3% FP 而召回 41.4→20.5；chunk-max 把末尾恢复到 75%，但长 benign FP 6%→18% |
| Wolf Defender v2 / prompt-injection-guard-base 模型卡 | **二手模型卡，未独立复现** | 训练量 ~280k（43% positive）+ v2 增补 ~30k；256/2048-token 混合窗口、64-token overlap、Normalized Smooth-Max；hard negatives 明确列为「讨论注入的文档、良性系统提示、含指令样文本的代码/配置片段」等 |
| CaMeL arXiv:2503.18813v2、Dual LLM、Agents Rule of Two | 一手（前一轮已核） | 保证在 tool 边界，不在模型嘴上 |
| 数据集合集（Qwak 汇总页、Kaggle、HF 卡片） | **二手条目，许可与条数按卡片转录，未下载** | 见 §五 |

## 二、E1 / E4：外部材料与实测一致（口径得到印证）

- **E1 的 100% 误报不是本项目特例**。`The Guardrail Cries Wolf` 用 1,528 条
  六渠道 benign 文本测三个开源检测器，得出「**误报是渠道问题，不是模型常数**」：
  引用攻击载荷的分析师笔记在 ProtectAI v2 上 FP 100%，而 CISA 漏洞文本 0.0%。
  它还测到「**没有哪个阈值能逃出饱和分数**」（0.9999 阈值下仍 88.3% FP）。
  本项目 E1 的 16/16 + 151/151 正是同一现象在字符 n-gram 模型上的复现，
  `deep_scan.applicable()` 的落点硬限制因此更有必要。
- **E4 的「检测率不是验收指标」有公开共识**。StackOne 结论句是
  *「Detection wasn't the hard part. Calibration was.」*；Belgavi.AI 的
  分类器工程总结写「把分数当作触发可逆缓解的**信号**，永远不要当安全边界」；
  CaMeL 把保证放在工具调用时。本项目 E4 里 **audit 检测率 1.0 但 ASR 0.833**、
  enforce 才把 `sim.call_tool` 停住，正是这一共识的可执行证据。

## 三、E2 归因修正：不只是「剂量不足」，还有「评估集选错」

**报告里原本写的是**：12 条额外负例 vs 1240 条训练负例，剂量太小，决策边界没动，
所以「本实验无法判断分布匹配是否有效」。这句话没错，但**不完整**。

外部一手材料补上两点：

1. **hard negatives 本来就不泛化到没见过的 schema**。StackOne 原文：
   > "Hard negatives raise the floor on schemas the model has been trained on,
   > or close cousins of them. **They can't help on a payload shape the model
   > has never seen**."

   而 E2 的主口径恰恰是 **按 `user_tool` 整组留出的跨域 test**（3 组没见过的业务域）。
   也就是说，我把 hard negatives 放到了它理论上**最不该起作用**的评估集上，
   却拿它当「是否达标」的判据。这是实验设计偏差，不只是数据量问题。
   StackOne 自己也只在**同分布** connector benchmark 上报 1.7%，跨域部分是靠
   **结构过滤（SFE）**兜底，不是靠 hard negatives。
2. **量级差 3–4 个数量级**。公开模型的训练量是 ~280k（43% positive）、
   80k、70k、23,699 行这个量级；我加了 12 条。剂量判断成立，但独立看它
   会漏掉第 1 条设计问题。

**更正后的表述**：E2 未通过 = 「**评估口径与方法的泛化边界不匹配**」+「**负例剂量不足**」
共同造成；不能简化成「剂量不足」，更不能写成「hard negatives 无效」。
`docs/detector-evolution.md` 第七节那条待办仍成立，但下一版实验应该
**把主口径换成同分布留出集**，跨域另设独立判据。

## 四、E3 归因修正：我复现的不是 SFE

**报告里原本写的是**：`score()` 的 `z/√n` 归一化让整段 JSON 被长度稀释，
过滤反而抬高了剩余自由文本的相对分数，属机制性反例。机制描述本身成立，
但**实现前提和 StackOne 的 SFE 不是一回事**：

| | StackOne SFE | 本次 E3 近似 |
|---|---|---|
| 打分单位 | **每个 string field 独立**判 pass/drop，只把 pass 字段送 Tier 2 | 把 JSON 序列化成**一整段字符串**一次打分（`full`），或把保留字段**拼接**后一次打分（`filtered`） |
| 判定依据 | FastText 按字段形状/内容类型分类，不看 connector 身份 | 正则 + 字段名后缀 |
| 丢弃比例 | 940 条真实 payload 上 ~45% 字段 | 构造的 9 字段 payload 上 66.7%（6/9） |
| 适用前提 | 多字段企业 JSON；**单一自由文本字符串时 SFE 无事可做** | 只有 3 个自由文本字段，且攻击放在其中一个 |

StackOne 原话：*"For free-form text classification (a single string rather than
a JSON payload) SFE has nothing to filter and the model runs alone."*
我构造的 payload 更接近「拼接后的自由文本」，并且把**逐字段独立打分**
简化成了「拼接后整体归一化」，所以 `full` 与 `filtered` 的对比测的其实是
「同一归一化打分下拼接长度变化」，**不是 SFE 的逐字段过滤效果**。

DeFuse 进一步解释了为什么长度会主导结果：max-pooling 下文档级误报随窗口数
按 `1−(1−p)^N` 复合，固定阈值下的 F1 只在量「阈值放得对不对」，AUC 才是
阈值无关的；它给的解法是**长度感知阈值 + 报 AUC/标定 FPR 下的召回**。
`The Guardrail Cries Wolf` 还测到分窗的代价：chunk-max 把末尾检出从 8.3%
拉到 75%，但长 benign 误报从 6% 涨到 18% —— **分窗不是免费收益**。

**更正后的表述**：E3 未通过 = 「**未满足 SFE 的逐字段独立打分前提**」+
「**固定阈值 + 长度归一化**」共同造成，不是「推理前结构过滤方法无效」。
`dropped_only` 组 FP 100% 依旧成立，确实证明结构字段是噪声源。

## 五、可接入的公开语料（许可清楚，供 E2′ 用）

按卡片转录，**尚未下载、未核实标签质量**，接之前要复查许可与来源族切分：

| 数据集 | 规模 | 许可 | 为什么相关 |
|---|---|---|---|
| `rgeada/tool-response-injections` | 14.8k 行 | Apache-2.0 | 真实 tool response + 成对 clean/injected，最贴 E2；需按原始响应与攻击种子分组防泄漏 |
| `usezn/zn-prompt-injection-bench` | 23,699 行 | CC-BY-4.0 | 含 benign hard negatives 与多攻击族、带来源/语言溯源 |
| `operant-ai/prompt-injection-false-positive-test-data` | 1,140 条 | 卡片标 unspecified | 纯 benign 过防御对照，保留 train/val/test 来源；**许可未定，用前必须查** |
| BIPIA（KDD 2025，微软） | 基准 | 见仓库 | 间接注入一手基准，攻击藏在 email / 表格 / 网页 / 代码里 |
| `prompt-safety-classifier-100k`（Kaggle） | 80k train / 10k val / 10k test | MIT | 六类标注、含 `surface_form_vs_intent` 字段 |

中文侧：`benchmark-2-chinese-m2m` / `-gt` 各 1,000 条，是**机器翻译**生成，
本项目此前已明确「翻译样本不能替代真实中文语料」，这条判断仍成立；
WARD-Base（NUS，2026-05，约 17.7 万条网页样本）目前只见到**二手报道**，
未拿到一手论文/卡片，**不写进能力结论**。

## 六、下一步：E2′ / E3′ 必须重新预注册

本次 `frozen_split.json` 的判定线已经写死并校验，**不许在它上面改口径**：
下列都属于**新实验**，要另写预注册文档、另冻结留出集：

- **E2′**：把主口径换成**同分布**（按 payload 话术留出）测 hard negatives，
  跨域单独作为 SFE 的判据；负例量级提到千条以上并做类别平衡；
  按来源族整组切分（公开数据集卡片反复警告 split leakage）。
- **E3′**：改成**逐字段独立打分 + max/any 聚合**，复现 SFE 的真实前提；
  同时报 **AUC（阈值无关）**、标定 FPR 下的召回、以及长度感知阈值；
  明确报分窗带来的额外误报代价（不能只报末端召回提升）。
- 两个实验都继续沿用本项目零依赖边界：下载语料只放 `data/external/`，
  不装系统 Python 包、不引模型服务。

## 七、边界

- 本文**没有**改任何实验结果、判定线或冻结集；只更正归因与下一步设计。
- Zenodo 那篇只核到页面摘要，PDF 直链 403，**不能当全文引用**。
- 模型卡与数据集条目的数字是二手转录，未独立复现，引用须标注。
- WARD-Base、中文翻译集的局限按上文如实写，不拿来充「已接入中文真实语料」。
- 不因为外部材料好看就回头把 E2/E3 改成「通过」—— **那正是预注册要防的事**。

## 八、参考

- https://www.stackone.com/blog/detection-isnt-the-hard-part （一手全文）
- https://arxiv.org/abs/2609.22510 （DeFuse v1，PDF 已存 `_sandbox/semantic_refs/`）
- https://zenodo.org/records/23540999 （页面摘要；PDF 未获取）
- http://www.aicassindra.com/blogs/llm_security/input_classifier_injection_detection.html （工程总结，二手）
- https://www.kaggle.com/models/aisuko/wolf-defender-prompt-injection/ 、
  https://savrn.com/models/prompt-injection-guard-base （模型卡，二手）
- https://cdn-avatars.qwak.ai/collections/tuandunghcmut/text-only-prompt-injection-datasets-and-benchmarks （数据集条目汇总，二手）
- 本项目：`experiments/semantic_injection_v1/report.md`、
  `docs/semantic-injection-plan.md`、`docs/detector-evolution.md`
