# 企业就绪度边界说明

这份文档回答一个问题：**这个仓库里哪些是企业级能力，哪些是「留了口子等企业自己接」。**

判定原则只有一条：我们只把**能自己验一遍**的东西叫「真实现」。验不了的
（需要真客户流量、需要第三方审计、需要多机集群）就写清接口与缺口，不写进
能力表里充数。

自评分数由 `evoguard scorecard` 生成，见 `docs/enterprise-scorecard.md`。
分数是**找缺口用的排序依据**，不是认证、不是 SLA、不代表可以生产部署。
**独立审查明确「仓库自评分数不计入结论」**：27 项发现的逐项状态以
[整改台账](review-remediation/README.md) 为准，别只看那个分数。

---

## 一、真实现（有测试/有产物可核对）

| 模块 | 能力 | 怎么验证 |
|---|---|---|
| `service_controls` | Bearer 鉴权（`hmac.compare_digest` 恒定时间比对）、按 actor 滑动窗口限流、停止开关、启动期 fail-closed、结构化日志 | `tests/test_service_controls.py`（13 项） |
| `enterprise/tenancy` | `X-Tenant-Id` 解析、每租户配额、未知租户收紧到最小权限、SQL 缺租户条件守门 | `tests/test_enterprise.py::TestTenancy` |
| `enterprise/observability` | 结构化日志管道（sink 故障不影响主流程）、Prometheus 文本指标（counter/gauge/histogram）、span 追踪、只采元数据的流量采样 | `TestObservability` + `GET /metrics` 实测 |
| `enterprise/audit` | 审计表（`tenant_id` 非空约束）、版本化迁移（v1→v4 幂等）、按租户查询与保留期清理、**逐行哈希链 + 归档锚点**、`BEGIN IMMEDIATE` 跨进程事务、流式校验 | `TestAudit` + `TestAuditTamperEvidence` + `TestAuditRetention` + `test_audit_multiprocess`（两独立进程并发写） |
| `enterprise/jobs` | 持久作业：任务 ID / 轮询 / 取消 / 幂等键 / 租约 / 崩溃后 `unknown` 不自动重放；按租户分库、队列有界 | `tests/test_jobs.py` 10/10 + `scripts/jobs_smoke.py` 11/11 |
| `semantic_guard` | 有界 LRU 缓存 + TTL + 作用域隔离，**临时失败不入缓存** | `tests/test_semantic_cache.py` 7/7 |
| `enterprise/resilience` | 熔断三态、超时预算、SLO 计算（分母 0 返回 `None`）、演练骨架 | `TestResilience` |
| `enterprise/tls_proxy` | TLS 上下文构建（证书缺失报错不降级）、代理头按对端可信度采信 | `TestTLSProxy` + `tests/test_tls_handshake.py`（真握手） |
| `enterprise/scorecard` | 按证据打分、输出缺口 | `TestScorecard` |
| 服务接线 | `request_id → 鉴权 → 限流 → 停止开关 → 租户 → 配额 → 路由`，含指标/审计/追踪；企业模式 token→身份绑定、按租户分库、角色与启动期 fail-closed | `TestLiveService` + `scripts/enterprise_smoke.py`（9/9） |
| 压测与演练 | 3 轮 × 4 场景（含写路由）× 24 请求 / 8 并发，四类状态分开报 + 原始样本；5 条故障演练（注入→断言→恢复） | `experiments/loadtest_v1/result.json`、`experiments/drill_v1/result.json` |
| 契约与门槛 | `docs/api-contract-v1.json` + 防漂移测试；`scripts/review_checks.py --gate` 逐项硬门槛 | `tests/test_api_contract.py`（8/8）、`docs/review-remediation/after.md` |

## 二、预留接口（结构给了，实现等企业接）

| 接口 | 位置 | 企业接什么 |
|---|---|---|
| `TenantResolver` | `tenancy.py` | JWT / SSO / API key 到租户的映射。现在只有 `HeaderResolver`（信任网关已校验过的头）。 |
| `LogSink` | `observability.py` | 文件 / Fluent Bit / CloudWatch。现在有 `NullSink` / `StderrSink` / `JsonlFileSink` / `MemorySink`。 |
| `MetricsExporter` | `observability.py` | 推到远程 write endpoint（VictoriaMetrics / Mimir）。现在是进程内 `/metrics` 等 Prometheus 拉。 |
| `TraceExporter` | `observability.py` | OTel Collector / Jaeger。现在只在内存里存最近 N 条 span。 |
| `AuditSink` | `audit.py` | 同时写 SIEM / Kafka。`AuditStore` 是其中一个实现。 |
| `AlertSink` | `resilience.py` | PagerDuty / Webhook / 飞书。现在只算出告警事件，不负责投递。 |
| 证书轮换 | `tls_proxy.py` | ACME / 内部 CA 轮换。现在只接受现成证书路径。 |

## 三、明确不做（写清楚，免得被当成「应该有」）

- **数据库层行级隔离（RLS）**：sqlite 没有 RLS，本仓库也没有 PostgreSQL。
  企业模式下改用**按租户分库**：`tenant_db_path()` 给每个租户一个独立
  sqlite 文件，`get_store(tenant)` 按身份取对应库；`scripts/enterprise_smoke.py`
  实测 acme 有 1 条 run 时 beta 查到 0 条。这是**物理隔离**，比原来的
  `require_tenant_filter()` 字符串守门强得多，但**仍然不是 RLS**：跨库聚合、
  统一备份、schema 变更要企业侧自己处理；换成 PostgreSQL + `ROW LEVEL
  SECURITY` 才是数据库层策略。见台账 EG-001。
- **持久作业与写路径性能**：同步 `POST /api/run` 仍在兼容期；新增
  `/api/jobs` 把提交与执行拆开（8 并发提交 P95 14.1 ms，完成 P95 1785 ms）。
  作业化改善的是**提交响应**，不是实验本身变快。同负载改前改后对比显示
  写路径 P95 3216→3447 ms，**无实质改善**（`review-remediation/perf-before-after.md`）。
  WAL 尝试因 3/24 `readonly` 500 按证据撤回。EG-009 / EG-010 仍未完成。
- **前端会话**：已有 `sessionStorage` + Bearer 令牌传递与 401/403 反馈，
  但真实反代认证、失效/登出流程与 CSRF 未在部署拓扑实测（EG-020）。
- **多机 / 多副本一致性**：指标与限流都是**进程内内存**，N 个副本各算各的。
  真实部署要在网关或 Redis 做集中限流。
- **集中式日志与追踪后端**：只有 sink 接口与本地文件实现，没有部署拓扑。
- **混沌工程的随机注入**：演练是**已知可控故障**（改环境变量重启），不是随机
  杀进程/注网络延迟。没有长时（小时级）稳定性与内存增长验证。
- **第三方合规认证**：没有 SOC 2 / ISO 27001，也不声称等价。

## 四、检测能力这一维为什么只给 2/5

预注册 v2 的检测器**没通过自己的判定线**（AUC 从 0.9917 掉到 0.9593，允许下降
0.01，实际掉 0.032）。负结果照记在 `experiments/semantic_injection_v2/`。

不放宽判定线、不改 test 集、不挑好看的口径。下一轮要另立 v3 预注册，而不是
在这个文件里把 0.9593 讲成成功。企业不会部署一个「在留出集上没过自己定的线」
的检测器 —— 这一点比任何一次调参都重要。

## 五、独立审查之后：这一轮补了什么 / 还差什么

2026-11-12 的独立审查给了 27 项发现，并明确「仓库自评分数不计入结论」。
这一轮按它的交接规则整改：**先复现差距 → 再改 → 留修前修后证据**。
逐项状态（✅5 / 🟡12 / ⬜10）与证据见 [整改台账](review-remediation/README.md)。

补了（都能自己验）：企业模式身份绑定与按租户分库、启动期 fail-closed、
wheel 制品在源码树外可用、请求体上限与堆栈不外泄、审计归档锚点、
热路径 DDL 消除、游标分页、探针拆分与指标口径、压测四类状态、
前端可选面板解耦、机器可读接口契约、CI 逐项硬门槛。

第二轮（同日）继续：持久作业、审计跨进程事务与流式校验、v3 预注册
（判定线冻结、实验未跑）、详情读取预算、前端企业令牌会话、数据分类与
合成敏感标记验收、缓存有界与临时失败隔离；并做了同负载改前改后对比，
按证据撤回了引入 500 的 WAL。台账从 ✅5/🟡12/⬜10 推进到 ✅6/🟡17/⬜4。

**没解决的硬缺口**（不因改了一堆代码就改口）：

1. 核心检测 v2 **仍未过自己的预注册线**；v3 只完成预注册，实验与独立复跑都没做；
2. 隔离是按租户分库，**不是数据库 RLS**；
3. 写路径性能无实质改善，`POST /api/run` 同步路径仍在；
4. 单进程限流/指标、无真实业务流量、无 SSO、无多副本验证；
5. Docker 镜像层与 GitHub CI **本轮未实测**（daemon / 远程仓库都没连）。

> 结论一句话：**工程形态与可验证据又推进了一批，但审查的核心判断没变。**
> 能在本机复现并修掉的都修了；修不了的，逐条写清了为什么。
