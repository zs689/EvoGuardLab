# 企业就绪度自评（scorecard）

> 总分 **84.8 / 100** —— 这是按可核对证据的自评，不是认证、不是 SLA、不代表可生产部署。

| 维度 | 权重 | 得分 | 缺口数 |
|---|---|---|---|
| 检测能力 | 22% | 2.0/5 | 2 |
| 进程级验收 | 15% | 5.0/5 | 0 |
| 可观测性 | 12% | 5.0/5 | 0 |
| 多租户 | 12% | 4.2/5 | 1 |
| 审计与迁移 | 12% | 5.0/5 | 0 |
| 韧性 | 9% | 5.0/5 | 0 |
| 传输安全 | 8% | 5.0/5 | 0 |
| 可复现性 | 5% | 5.0/5 | 0 |
| 数据治理 | 5% | 5.0/5 | 0 |

## 逐项证据

### 检测能力（2.0/5）

- ✅ 预注册 v1 四个实验都有结果 —— `experiments/semantic_injection_v1/e*_result.json`
- ✅ 预注册 v2 已执行 —— `experiments/semantic_injection_v2/result.json`
- ✅ 至少一个实验达到判定线 —— `v1 通过 2/4`
- ❌ 申报的检测器变体达到判定线 —— `v2 passed=False`
- ✅ 逐组判定可见（未隐藏负结果） —— `per_view_checks`

### 进程级验收（5.0/5）

- ✅ 无 token 时返回 401（不是静默放行） —— `GET /api/health`
- ✅ 带 token 时返回 200 —— `Authorization: Bearer ***`
- ✅ 响应头回显 X-Request-Id —— `X-Request-Id`
- ✅ 租户头进入请求上下文 —— `X-Tenant-Id: acme`
- ✅ 审计在真实进程里开启 —— `AGENTLAB_AUDIT_DB`
- ✅ Prometheus 端点返回文本指标 —— `GET /metrics`
- ✅ 既有 /api/metrics JSON 契约未被破坏 —— `GET /api/metrics`
- ✅ 未知路由 404 且带 request-id —— `GET /api/definitely-not-a-route`
- ✅ 审计事件真的落到了库里 —— `audit rows=1`

### 可观测性（5.0/5）

- ✅ # TYPE 与 label 出现在 Prometheus 文本里 —— `render_prometheus()`
- ✅ 日志管道注入 tenant_id 与 ts —— `LogPipeline.emit`
- ✅ span 记录时长 —— `Tracer.span`
- ✅ 流量采样只留白名单、不采正文 —— `TrafficSampler`
- ✅ 可插拔 exporter 接口存在 —— `MetricsExporter/TraceExporter`
- ✅ 交付了 Prometheus scrape 配置 —— `deploy/prometheus.yml`
- ✅ 交付了告警规则 —— `deploy/alerts.yml`

### 多租户（4.2/5）

- ✅ 按请求头解析租户与主体 —— `HeaderResolver`
- ✅ 每租户配额独立生效 —— `TenantQuota.check`
- ✅ 未知租户落到最小权限 —— `TenantRegistry.policy（quota=10）`
- ✅ SQL 缺租户条件被守门拦下 —— `require_tenant_filter`
- ✅ 服务入口已接线租户上下文 —— `app.py`
- ❌ 数据库层行级隔离（非仅字符串守门） —— `deploy/rls.sql`

### 审计与迁移（5.0/5）

- ✅ schema 迁移到最新版本 —— `SCHEMA_VERSION=3`
- ✅ 审计事件可写入并按租户查询 —— `AuditStore`
- ✅ 缺 tenant_id 的审计写入被拒绝 —— `AuditStore.emit`
- ✅ 保留策略可清理过期事件 —— `purge_before`
- ✅ 服务入口可开启审计落库 —— `app.py`
- ✅ 审计链重算通过（防篡改） —— `rows=2/2`
- ✅ SQLite 开启 WAL（并发读不被写锁死） —— `journal_mode=wal`

### 韧性（5.0/5）

- ✅ 连续失败后熔断打开 —— `CircuitBreaker`
- ✅ 冷却后放行探测 —— `half_open`
- ✅ 探测成功后闭合 —— `closed`
- ✅ 超时预算可扣减 —— `TimeoutBudget`
- ✅ SLO 计算 SLI 与错误预算 —— `SLO.evaluate`
- ✅ SLO 有实测流量底数（非手工数字） —— `experiments/loadtest_v1/result.json`
- ✅ 故障演练可一键重跑 —— `scripts/drill.py`

### 传输安全（5.0/5）

- ✅ 回环默认受信、外网默认不受信 —— `is_trusted`
- ✅ 支持 CIDR 信任网段 —— `is_trusted`
- ✅ 不可信对端的代理头被忽略 —— `resolve_proxy`
- ✅ 受信对端的 https 被识别 —— `resolve_proxy`
- ✅ 证书缺失时明确报错，不静默降级 —— `build_context`
- ✅ 真实 TLS 握手通过（不只是配置对象） —— `TLS TLSv1.3 / TLS_AES_256_GCM_SHA384`

### 可复现性（5.0/5）

- ✅ 存在换行归一化（.gitattributes） —— `.gitattributes`
- ✅ CI 安装并可跑 demo/unittest —— `.github/workflows/test.yml`
- ✅ 记录了干净 clone 的实测结果 —— `upgrade-report.md`
- ✅ 外部语料与沙箱被忽略 —— `.gitignore`
- ✅ 有字节级可复现性守门测试 —— `tests/test_reproducibility.py`
- ✅ CI 每次推送都做干净 clone 复验 —— `.github/workflows/test.yml`

### 数据治理（5.0/5）

- ✅ 语料正文不入库（.gitignore） —— `.gitignore`
- ✅ 仓库有 LICENSE —— `LICENSE`
- ✅ 随仓库分发的夹具标注了上游许可 —— `fixtures/external/README.md`
- ✅ README 声明语料不外发及例外 —— `README.md`
- ✅ 审计保留期可配置 —— `TenantPolicy.data_retention_days`

## 缺口清单（下一轮先补这些）

- **[检测能力]** 申报的检测器变体达到判定线
- **[检测能力]** 检测器 v2 未过预注册判定线（AUC 下降）；需另立 v3 预注册
- **[多租户]** 数据库层行级隔离（非仅字符串守门）
