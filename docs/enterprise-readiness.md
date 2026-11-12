# 企业就绪度边界说明

这份文档回答一个问题：**这个仓库里哪些是企业级能力，哪些是「留了口子等企业自己接」。**

判定原则只有一条：我们只把**能自己验一遍**的东西叫「真实现」。验不了的
（需要真客户流量、需要第三方审计、需要多机集群）就写清接口与缺口，不写进
能力表里充数。

自评分数由 `evoguard scorecard` 生成，见 `docs/enterprise-scorecard.md`。
分数是**找缺口用的排序依据**，不是认证、不是 SLA、不代表可以生产部署。

---

## 一、真实现（有测试/有产物可核对）

| 模块 | 能力 | 怎么验证 |
|---|---|---|
| `service_controls` | Bearer 鉴权（`hmac.compare_digest` 恒定时间比对）、按 actor 滑动窗口限流、停止开关、启动期 fail-closed、结构化日志 | `tests/test_service_controls.py`（13 项） |
| `enterprise/tenancy` | `X-Tenant-Id` 解析、每租户配额、未知租户收紧到最小权限、SQL 缺租户条件守门 | `tests/test_enterprise.py::TestTenancy` |
| `enterprise/observability` | 结构化日志管道（sink 故障不影响主流程）、Prometheus 文本指标（counter/gauge/histogram）、span 追踪、只采元数据的流量采样 | `TestObservability` + `GET /metrics` 实测 |
| `enterprise/audit` | 审计表（`tenant_id` 非空约束）、版本化迁移（v1→v3 幂等）、按租户查询、保留期清理、**逐行哈希链** | `TestAudit` + `TestAuditTamperEvidence`（改一行 / 删一行都能发现） |
| `enterprise/resilience` | 熔断三态、超时预算、SLO 计算（分母 0 返回 `None`）、演练骨架 | `TestResilience` |
| `enterprise/tls_proxy` | TLS 上下文构建（证书缺失报错不降级）、代理头按对端可信度采信 | `TestTLSProxy` + `tests/test_tls_handshake.py`（真握手） |
| `enterprise/scorecard` | 按证据打分、输出缺口 | `TestScorecard` |
| 服务接线 | `request_id → 鉴权 → 限流 → 停止开关 → 租户 → 配额 → 路由`，含指标/审计/追踪 | `tests/test_enterprise.py::TestLiveService`（起真进程发真 HTTP） |
| 压测与演练 | 1000 请求 / 20 并发的实测底数；5 条故障演练（注入→断言→恢复） | `experiments/loadtest_v1/result.json`、`experiments/drill_v1/result.json` |

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

- **数据库层行级隔离**：sqlite 没有 RLS。`require_tenant_filter()` 是**应用层**
  字符串守门，能挡住「忘了写 `WHERE tenant_id`」的常见错误，但**不是隔离**。
  真要隔离必须换 PostgreSQL 并上 `ROW LEVEL SECURITY`，或在表名前缀/独立 schema
  上做物理隔离。这是本仓库最大的架构级缺口，见 scorecard「多租户」一维。
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

## 五、这一轮补了什么 / 还差什么

补了（都是能自己验的）：真实进程级验收、Prometheus 抓取与告警规则、压测底数、
故障演练、审计哈希链、TLS 真握手、scorecard 与 CI 守门线。

还差（写进 scorecard 缺口清单）：数据库层 RLS、检测器 v3 预注册。

> 结论一句话：**工程形态基本齐了，核心检测没过线，隔离是应用层的。**
> 前一句靠代码，后一句靠诚实。
