# 结构化工具契约授权（§5）

> 契约版本 `tool-contract-v1` ｜ 通过 7/7
> 授权由结构化契约决定；正则命中只写进 regex_signals，不改变 decision。

## 两个反例

1. **正则命中但契约允许**：文本里有 `rm -rf /`，正则会命中，但结构化检查全过 → `allow`。命中只记进 `regex_signals`。
2. **正则零命中但越界**：域名/金额/身份越界，正则什么都没抓到 → `deny`。「没认出攻击」不等于「有权执行」。

## 逐场景

| 场景 | 期望 | 实际 | 正则信号 | 结构化失败项 | audit 下 | 结果 |
|---|---|---|---|---|---|---|
| 正常只读查询 | allow | allow | 无 | 无 | allow | 通过 |

  - 为什么：身份、资源 ID、方法都在契约内
| 正则命中但契约允许 | allow | allow | dangerous_shell_rm | 无 | allow | 通过 |

  - 为什么：文本里有危险字样，正则会命中；但域名/路径/金额/资源/方法/身份全合规，正则不承担授权决策
| 正则零命中但域名越界 | deny | deny | 无 | identity.known_identity、tool.policy_allowlist、url.allowed_domain、side_effect.declared、approval.approval_request_id | report | 通过 |

  - 为什么：文本没有任何攻击特征，但契约没登记身份、没允许域名
| 金额超过契约上限 | deny | deny | 无 | total.max_amount | report | 通过 |

  - 为什么：没有正则特征，但 99999 > 10000 上限
| 未知身份 | deny | deny | 无 | identity.known_identity | report | 通过 |

  - 为什么：未知身份在 enforce 下拒绝，不能让输入文本自行扩权
| 写操作缺审批请求 id | deny | deny | 无 | approval.approval_request_id | report | 通过 |

  - 为什么：写操作必须带 approval_request_id
| 未登记的工具契约 | deny | deny | 无 | tool.known_contract | report | 通过 |

  - 为什么：没有契约就不能授权

## 契约字段

身份 / 允许域名 / 资源 ID 格式 / 可写路径 / 最大金额 / 方法 / 副作用类型 / 是否需要审批请求 id。

## 边界

- 本模块独立于 `security.guard_tool_args`，默认行为不变，351 项回归可比。
- 正则只作附加信号，不承担授权决策；不得让输入文本、模型或经验自行扩大权限。
- 未知身份在 `enforce` 下拒绝；`audit` 下只 `report` 并留原因。
