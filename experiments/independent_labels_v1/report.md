# 双人标注一致性（代理标注者）

> **不是真人标注**：A=规则标注器、B=本地语义判定器，原理独立但都是自动的。真人双标与 Cohen's kappa 仍缺人。
> 218 条样本 ｜ kappa **0.325** ｜ 一致率 0.734 ｜ 分歧 58 条

## 按来源

| 来源 | 样本 | 一致率 | kappa |
|---|---|---|---|
| `injecagent_base` | 39 | 0.538 | 0.033 |
| `injecagent_enhanced` | 39 | 0.846 | 0.709 |
| `jailbreak_corpus` | 80 | 0.713 | 0.036 |
| `vectors_30` | 60 | 0.817 | 0.475 |

## 分歧样例（需人工讨论，不自动取多数）

- `v02:attack` ｜ A=normal ｜ B=attack
- `v03:attack` ｜ A=normal ｜ B=attack
- `v04:attack` ｜ A=normal ｜ B=attack
- `v14:attack` ｜ A=attack ｜ B=normal
- `v15:attack` ｜ A=attack ｜ B=normal
- `v17:attack` ｜ A=normal ｜ B=attack
- `v21:attack` ｜ A=attack ｜ B=normal
- `v22:attack` ｜ A=attack ｜ B=normal

## 边界

- 两个自动标注器可能**共享同一种盲区**（例如自然语言间接注入），一致 ≠ 正确；分歧清单才是要人看的部分。
- 真人独立标注协议在 `docs/external-labeling-protocol.md`，待复核清单在 `experiments/external_labeling_v1/`，没有真人参与前不宣称标注质量达标。
