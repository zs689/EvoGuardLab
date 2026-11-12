# 服务层工程基线（2026-11-12）

> 这一层补的是**工程基线**，不是「已达到企业级」的证明。
> 全局结论见 `docs/upgrade-report.md`。

## 补了什么、对应哪条公开控制

实现在 `ai-service/evoguard/service_controls.py`（纯标准库、可独立单测），
接线在 `ai-service/api/app.py` 的 `Handler._guarded()` —— 每个请求的统一入口。

| 控制 | 实现 | 外部依据 |
|---|---|---|
| Bearer token 鉴权 | `authorize()`，用 `hmac.compare_digest`，绝不用 `==` | OWASP AI Agent Security Cheat Sheet（身份 / 最小权限）；Agentic Top 10 **ASI03** |
| 滑动窗口限流 | `check_rate()`，`threading.Lock` 保护、窗口过期清理 | Cheat Sheet 的 rate limits；**ASI02 / ASI08**（级联失败的熔断） |
| stop switch | `kill_switch(method)`，开启时只挡写操作、只读仍可用 | **ASI10**（rogue agents 的停止开关与活动日志） |
| 结构化审计日志 | `log()` 输出单行 JSON：`ts / request_id / method / path / status / actor / decision` | Cheat Sheet「记录决策而不只是消息」；**ASI10** |
| 配置校验 | `validate_config()` 检查数值环境与阈值文件，供启动期 fail-closed | Cheat Sheet 的 fail-closed |
| 版本单一来源 | `app.py` 从 `pyproject.toml` 读版本，消除 0.3.0 / 0.6.0 漂移 | 发布一致性 |
| 部署入口 | 根目录 `Dockerfile`（开发/演示镜像） | — |

每个响应都带 `request_id`；守卫失败返回 401 / 429 / 503 的 JSON 体，
成功与失败都留一条结构化日志。**token 与请求正文永不写进日志**。

## 默认关闭鉴权的理由与风险（必须读）

不设置 `AGENTLAB_API_TOKEN` 时鉴权**关闭**，请求照常放行。这是为了保持
`tests/test_api_smoke.py` 与「本地 `python ai-service/api/app.py` 直接打开看板」
的向后兼容，也是研究原型的合理默认。但这意味着：

- **默认态下 11 个路由仍然全开放**，`POST /api/run` 无需身份即可触发实验；
- 服务只绑定 `127.0.0.1`，靠本机回环而不是鉴权兜底；
- 一旦把它放到任何共享网络，必须显式启用鉴权 + TLS 反向代理。

`describe()` 会明确返回 `auth_enabled: false` 与 warning，不静默。

## 启用方式

```bash
export AGENTLAB_API_TOKEN='<一个真实的随机 token>'   # 设了才启用鉴权
export AGENTLAB_RATE_LIMIT=60                        # 每窗口请求数，默认 60
export AGENTLAB_RATE_WINDOW=60                       # 窗口秒数，默认 60
export AGENTLAB_KILL_SWITCH=0                        # 1/true/on 时拒绝所有写操作
python ai-service/api/app.py

# 调用方
curl -H "Authorization: Bearer <token>" -H "X-Actor: alice" \
     http://127.0.0.1:8770/api/health
```

`AGENTLAB_LOG_QUIET=1` 可关闭 stderr 的结构化日志（本地调试用）。
**不要把 token 写进仓库、截图或对话**；`.env` 已被 `.gitignore` 排除。

## 明确没做的事

- TLS 终止、反向代理、WSGI/ASGI、容器编排与健康检查契约；
- 多租户隔离与真实身份体系（当前单 SQLite 文件、无 tenant 字段）；
- 集中式日志 / metrics / tracing（现在只写 stderr）；
- 持久化审计表、schema 版本与迁移；
- 真实密钥后端（token 目前来自环境变量）；
- SLA、容量压测、限流策略演练、值班与事故流程；
- **真实业务流量上的检测器验证**（`experiments/semantic_injection_v2/report.md`
  的预注册判定是「未通过」）。

这些不是「加几行代码」就能自证的，所以本文件不宣称企业级。
