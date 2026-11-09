# E03：经验适用范围 + 结构过滤 + BM25

> 版本 `experience-scope-v1` ｜ 结构过滤先于 BM25；撤销/过期/版本/权限不匹配一律剔除，分数再高也不进候选。相似度不能替代可信性。

## 反例（必须成立）

- 查询：`新版退货政策 按文档版本号选证据`
- 一条**文本最像但已撤销**的记录：实际被剔除 = **True**
- 若忽略撤销标记，它会排第一：`exp_revoked_best_match`
- 结构过滤后实际第一：`exp_valid_old`
- 结论：BM25 分数不能替代可信性，撤销/过期/版本/权限必须先过滤。

## 检索结果

| 排名 | 经验 id | BM25 | 来源 run |
|---|---|---|---|
| 1 | `exp_valid_old` | 1.3484 | run_demo |
| 2 | `exp_valid_new` | 0.5343 | run_demo |

## 被剔除的记录与原因

| id | 原因 |
|---|---|
| `exp_revoked_best_match` | 经验已撤销 |
| `exp_expired` | 经验已过期 |
| `exp_wrong_data_version` | 数据版本不匹配 |
| `exp_wrong_contract` | 工具契约版本不匹配 |
| `exp_privilege_overflow` | 权限范围超出当前策略：['shell.exec'] |

## 边界

- 纯标准库 BM25（k1=1.5 / b=0.75），中文按 2-gram、ASCII 按词。
- **未使用 embedding**：只有语义改写确实造成召回不足时才加；相似度不能替代可信性。
- 适用范围字段：工具契约版本 / 数据版本 / 权限范围 / 有效期 / 撤销状态 / 来源 run。
