# -*- coding: utf-8 -*-
"""企业形态最小实现与预留接口（纯标准库，零运行时依赖）。

每个模块都遵循同一条原则：**能真做的就真做，不能自证的就留清晰接口**。

| 模块 | 真实现 | 预留接口 | 明确不做 |
|---|---|---|---|
| tenancy | 租户解析、每租户配额、SQL 隔离守门 | TenantResolver（JWT/SSO/API key） | 物理隔离 / 跨库分片 |
| observability | 结构化日志 sink、Prometheus 指标、span 追踪 | LogSink / MetricsExporter / TraceExporter | 日志聚合后端、OTel Collector |
| audit | 审计表、schema 版本与迁移、保留清理、逐行哈希链可验篡改 | AuditSink（外接 SIEM） | 集中式审计存储 |
| resilience | 熔断、超时预算、SLO 计算、故障演练 | AlertSink（PagerDuty/Webhook） | 真实值班流程 |
| tls_proxy | TLSContext 构建、代理头信任解析 | 证书轮换 / ACME | 应用内证书生命周期 |
| scorecard | 按证据打分、输出缺口清单 | 权重与阈值可配 | 第三方认证 |

**这些都不是合规认证**。`docs/enterprise-readiness.md` 写明每项的边界。

子模块按需导入（`from enterprise import tenancy`），避免任一模块出错拖垮整包。
"""

__all__ = ["tenancy", "observability", "audit", "resilience", "tls_proxy",
           "scorecard"]
