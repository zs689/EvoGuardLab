# 数据分类、保留与最小化策略（EG-022）

这份文档定义「哪类数据活多久、谁能看、怎么删」。**当前是策略 + 已实现的
边界清单，不是完整的数据生命周期产品** —— 没做的部分在 §五逐条列出。

验证方式：`tests/test_data_governance.py` 用合成敏感标记
`CANARY_SECRET_9f2c7b1e` 真的塞进请求，再从结构化日志、指标、
`/api/health`、运行列表与详情逐个出口检查原值不出现。

## 一、字段分类

| 类别 | 内容 | 落点 | 敏感级别 | 保留期（默认） |
|---|---|---|---|---|
| **审计事件** | `tenant_id / request_id / actor / method / path / status / decision / latency_ms` | `AGENTLAB_AUDIT_DB`（sqlite，逐行哈希链 + 归档锚点） | 高（谁在何时调了什么） | 30 天，`TenantPolicy.data_retention_days` 可调 |
| **结构化日志** | 仅白名单 7 字段（见 `service_controls.py:log`） | stderr / 日志 sink | 中 | 由日志后端决定，应用不落盘 |
| **运行轨迹** | `steps / diagnoses / recoveries / faults`、运行输入输出、技能与经验 | 业务库（按租户分库） | **最高**：可能含业务正文与模型输出 | 无自动过期，需人工/租户级清理 |
| **模型缓存** | 判定结果、文本 sha256、`ms`、`status` | `SemanticGuard.cache`（内存 LRU）+ 可选 jsonl | 中 | TTL 3600 s、LRU 上限 4096 条 |
| **指标** | 计数器与直方图，标签只有 `route / status / decision` | 进程内内存，`/metrics` 拉取 | 低 | 进程生命周期 |
| **导出** | `/api/runs` 列表、`/api/runs/{id}` 详情、报告/分类法 Markdown | HTTP 响应 | 视内容 | 不落盘；`Cache-Control: no-store` |

## 二、入口最小化（已实现）

- `service_controls.log()` **按白名单重建记录**：调用方即使误把
  `body` / `token` / `Authorization` / `password` 塞进 event，也不会写进日志。
- 守卫层不把请求正文写进审计事件；审计只记元数据（方法、路径、状态、
  决策、耗时），`detail` 默认空。
- 指标标签固定为 `route / status / decision`，`_route_label()` 把 ID 归一，
  避免基数被撑爆、也避免把 ID 当标签外泄。
- 前端令牌只存 `sessionStorage`（关标签即失效），不进 URL、不进
  `localStorage`、不留在 DOM 明文（`frontend/index.html`）。
- 500 响应只给稳定错误码 + `request_id`，不回传堆栈/路径/SQL。

## 三、租户级操作（部分实现，别当成已有 API）

| 能力 | 现状 | 接口 |
|---|---|---|
| 按租户保留期清理 | ✅ | `AuditStore.purge_before(ts, tenant_id=...)`、`purge_older_than(days, tenant_id=...)`；合法清理写 `audit_anchors`，链仍可验 |
| 授权删除与篡改区分 | ✅ | `verify()` 只在缺口处匹配归档锚点才放行；手工 `DELETE` 报 `prev_mismatch` |
| 完整性锚点独立权限域 | ⬜ | `AuditStore.anchors()` 可导出，**但没有自动备份到独立权限域**；生产需外部只追加存储 |
| 租户级数据导出 | ⬜ | 只有 `/api/runs` 与详情，没有打包导出与审计留痕 |
| 保留冻结（legal hold） | ⬜ | 无 |
| 运行轨迹的自动删除/归档 | ⬜ | 业务库目前只增不减 |

## 四、加密与备份（部署侧，应用不假装做了）

- **传输**：应用只说 HTTP，绑定默认 `127.0.0.1`；TLS 必须由反向代理终止
  （`tls_proxy.build_context()` 提供应用内 TLS 的可选能力，但生产入口应走反代）。
- **静态**：应用不做数据库加密。需要时由部署侧提供加密卷 / SQLite 加密层，
  并把密钥放进独立 KMS —— **不要**写进 `.env` 或镜像。
- **备份**：`AGENTLAB_DB` / `AGENTLAB_AUDIT_DB` / `data/tenants/` 都要一起备份，
  且审计锚点要与审计库分开保存。若开启 `AGENTLAB_STORE_WAL=1`（必须与
  `AGENTLAB_STORE_POOL=1` 同开），**还要把未 checkpoint 的 `-wal` 文件一起
  备份**，否则恢复时可能丢最近事务；这一条目前只在
  `SECURITY.md` 写了警告。**实测证据**：只复制 `.db` 恢复 0/200 行，
  连 `-wal` 一起复制恢复 200/200（`store-pool-stability.md` §二）。
  但 WAL 已被判定不可用（24 并发约 1/8 `readonly`），所以这条主要针对
  误开启后的应急；**没有做过完整恢复演练**。RPO/RTO 与恢复流程本轮未做
  （EG-016 未完成）。恢复后必须 `AuditStore.verify()` 重算链。

## 五、已知限制（如实）

1. 没有租户级删除/导出/冻结的 HTTP API，只有库层面的按租户保留期清理。
2. 归档锚点与审计库同库，能改库的人理论上可插锚点伪造删除；
   `SECURITY.md` 已列为已知边界。
3. 没有真实备份恢复演练，没有静态加密配置，没有第三方审计。
4. 合成敏感标记测试覆盖的是「日志/指标/详情/响应」出口；
   **业务库里 steps 的 `answer` / `expect` 字段仍可能包含模型输出正文**，
   当前只靠「接口不透传」约束，没有字段级脱敏。这是最大的剩余风险。
5. 多租户隔离靠按租户分库，跨库统一备份与删除要企业侧自己实现。
