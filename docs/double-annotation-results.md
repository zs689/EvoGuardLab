# 真人双标注结果（评审 §7 的那个数，2026-11-12）

> 状态：**第二位标注者的导出已回收，标注者间 Cohen's kappa 已算出**。
> 这是「**人 vs 人**」，和 `result.json` 的「人 vs 语料标签」是两件不同的事。
> 它不是准确率，也不是标注质量（没有金标准）。

## 一、输入

| 角色 | 文件 | 条数 | 标签分布 | 编号 |
|---|---|---|---|---|
| A 第一位（本人） | `E:\agent\labels.json` | 60 | attack 20 / benign 40 | 真实 `case_id` |
| B 第二位（同学，基础页） | `labels_annotator2.json` | 60 | attack 26 / benign 34 | `r2_001…r2_060` |
| B 第二位（同学，加长页） | `labels_ext_annotator2.json` | 200 | attack 93 / benign 107 | `r2_001…r2_200` |

- A 与首轮 60 条样本集合**完全一致**（脚本校验通过）。
- 两份 B 都是匿名盲标页导出，页面不含语料标签、不含 A 的答案、不含规则实现；
  两份的呈现顺序与匿名编号各自独立。
- 加长批 `--n 100` 共 200 条，**必然包含首轮 60 条**（`sample()` 按哈希排序取前 n）。

## 二、三个产物

| 产物 | 含义 |
|---|---|
| `experiments/human_labels_v1/double_result.json` | **正式主口径**：A vs 加长版 B，200 条批次里两人交集 60 条 |
| `experiments/human_labels_v1/double_result_60.json` | 对照：A vs 基础页 B，60 条全配对 |
| `experiments/human_labels_v1/intra_annotator_check.json` | 诊断：同一位同学**两次作答**的稳定性 |

## 三、结果

| 口径 | n | 一致率 | kappa | 分歧 |
|---|---|---|---|---|
| 人 vs 语料标签 · 顶层混合（旧，`result.json`） | 60 | 0.733 | **0.467**（vs corpus） | 16 |
| 人 vs 语料标签 · 注入档（旧） | 41 | 0.878 | 0.752 | 5 |
| **人 vs 人 · 顶层（加长版配对，主）** | **60 / 200** | **0.717** | **0.414**（inter-annotator） | 17 |
| 人 vs 人 · 顶层（60 条页对照） | 60 / 60 | 0.733 | 0.442 | 16 |
| **人 vs 人 · 注入档（41 条，两份一致）** | **41** | **0.854** | **0.688** | 6 |
| 同一人两次作答（诊断） | 60 | 0.917 | 0.831 | 5 |

主口径里 **140 条只有 B 标、0 条只有 A 标**，记在 `unpaired.only_b`，
**没有混进 kappa 的分母**。

### 主口径分档（`double_result.json` 的 `by_category`）

| 类别 | n | 一致率 | 单档 kappa | 分歧 |
|---|---|---|---|---|
| benign | 5 | 1.000 | `None` | 0 |
| direct_jailbreak | 2 | 1.000 | `None` | 0 |
| **harmful_request** | **12** | **0.083** | 0.0 | **11** |
| indirect_clean | 25 | 0.920 | 0.0 | 2 |
| indirect_injection | 16 | 0.750 | **−0.103** | 4 |

17 条分歧的构成：`harmful_request` 11 + `indirect_injection` 4 + `indirect_clean` 2。

## 四、怎么读这些数（比数字本身重要）

1. **顶层 0.414 / 0.442 被口径差主导，不是「标注者能力差」**。
   `harmful_request` 12 条里 11 条分歧、一致率 0.083 —— 和单人「人 vs 语料标签」
   时那一档的 0.083 **完全一致**。也就是说两位真人对
   「有害请求算不算『注入/越狱』」有系统性理解差异：A 认为不是，B 倾向认为是。
   这是**判定问法的定义边界**，不是谁标错了。
2. **能引用的主口径是注入档 0.854 / kappa 0.688**（攻击 + 成对干净对照，
   41 条）。按常用分档属 substantial，但仍**不能**叫准确率或标注质量。
3. **单档 kappa 一律无意义**。每档语料标签近乎常量，所以出现 `None`、0.0
   甚至 **−0.103**；只有攻击与干净对照成对计算才有解释力。
   `score_double_labels.py` 的 note 已写明这一点。
4. **同一位同学两次作答也不是完全一致**：60 条里 5 条不同（一致率 0.917 /
   kappa 0.831），涉及 1 条 `zh_harm`、2 条 `zh_ind`、2 条 `zh_ind_clean`。
   因此两份 A-vs-B kappa 相差 0.028 **有一部分来自 B 自身噪声**，
   不能全归因于两人分歧。两次间隔很短、顺序不同但可能有记忆/疲劳效应，
   **不是严格重测信度**。
5. **140 条单人标签留着，不充数**。若 A 用已生成的
   `data/external/label_sheet/round2_ext_self.html` 补标 200 条，
   重跑 `--map-a` 就能把配对从 60 条扩到 200 条；在那之前不写「双标注 200 条」。

## 五、明确不能写的话

- ❌ 「准确率 / 标注质量」—— 没有金标准，全程是两两一致性。
- ❌ 「双标注 200 条」—— 只有 60 条两人配对，其余 140 条是单人标签。
- ❌ 「kappa 0.467 是标注者间一致性」—— 那是**人 vs 语料标签**。
- ❌ 「kappa 1.0」—— 把人工标签改成与语料一致得到的 1.0 是恒等式废数。
- ❌ 把任一**单档** kappa（0.0 / −0.103 / None）当成结论。
- ❌ 把「同一人两次 0.831」当成「两人之间的一致性」。

## 六、复现

在**仓库根目录**执行。两份标注导出是本机文件、不入库，路径按实际情况替换
（文档里不写死任何开发机的盘符或用户名）：

```bash
A="<首轮 labels.json 的路径>"
B60="<同学导出的 labels_annotator2.json 路径>"
B200="<同学导出的 labels_ext_annotator2.json 路径>"

python scripts/score_double_labels.py --a "$A" --b "$B200" \
  --map experiments/human_labels_v1/round2_ext_annotator_id_map.json \
  --n 100 --out experiments/human_labels_v1/double_result.json
python scripts/score_double_labels.py --a "$A" --b "$B60" \
  --map experiments/human_labels_v1/round2_id_map.json \
  --n 30 --out experiments/human_labels_v1/double_result_60.json
python scripts/check_annotator_consistency.py --b60 "$B60" --b200 "$B200"
```

守门测试：`tests/test_double_labels.py`（13 项）、`tests/test_human_labels.py`（6 项）。

## 七、口径沿革

- `result.json` v2（单人 vs 语料标签，分档主口径 0.752）保留不动，是**另一个问题**的答案。
- 本文是评审 §7「真实人工双标注 + 标注者间 kappa」的落地结果。
- 两份 B 的原始导出在用户本机 `Downloads/`，**含语料正文关联编号，不入库**；
  入库的是合并后的统计结果与匿名映射表。
