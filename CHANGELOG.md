# Changelog

本地变更记录。**未发布**：没有远程仓库、没有 tag、没有制品发布。
版本取自 `pyproject.toml` 单一来源（`app.py:_read_version()` 读它）。

## 0.6.0 · 本地未发布

### 独立审查整改（2026-11-12 ~ 13）

依据 `EvoGuardLab_企业与高性能客观审查_2026-11-12.json`（revision `793c6d1`）
的 27 项发现整改。逐项状态见 `docs/review-remediation/README.md`。

**修掉的硬问题**

- `EG-001/002` 企业模式：token→身份绑定（忽略伪造的 `X-Tenant-Id`）、
  按租户分库做物理隔离、角色拒绝、启动期 6 项 fail-closed。
- `EG-003` 容器：新增 `.dockerignore`，`Dockerfile` 改显式 COPY 清单、
  非 root、独立卷、healthcheck、优雅停止。
- `EG-004` 制品：wheel 补 `enterprise/` 子包、Inspector、前端/配置/演示资产；
  CLI 在源码树外可用；checkout-only 命令给明确说明而非 traceback。
- `EG-005` 请求体 64KiB 硬上限 + 413；500 响应不再外泄堆栈。
- `EG-007` 审计链：新增归档锚点，合法保留期清理后仍可验证，手工删除仍被抓。
- `EG-010/011/012/013` 热路径零 DDL、业务库路径可配、游标分页 + 上限 +
  索引、存活/就绪/诊断探针拆分且探针不进业务 SLI。
- `EG-015` 压测：四类状态分开报、纳入写路由、多轮 + 原始样本 + 环境指纹。
- `EG-021` 前端：可选面板用 `allSettled` 解耦，空库/404 不再阻塞整个看板。
- `EG-023` CI：逐项硬门槛、构建上下文门禁、Linux+Windows wheel 制品验收、
  汇总 release-gate；自评分数降级为提示，不单独放行。

**同时修掉的自己引入的 bug（本轮内引入、本轮内修掉）**

- 大 body 只排前 64KiB，客户端有时拿 413、有时拿连接重置（同一检查两次
  结果不同）；改为分块排空全文、内存仍有界。
- 企业模式多 token 身份映射与「单一 `AGENTLAB_API_TOKEN` 鉴权」冲突，
  除主 token 外全部 401；改为用身份映射表的键做恒定时间比对白名单。
- 批量替换 `core.Store(DB)` 时子串匹配误伤了 `get_store()` 内部缩进，
  已恢复并加语法/测试验证。
- `pyproject.toml` 的 `package-dir` 写成跨行行内表，TOML 非法；改单行。
- 告警规则引用的指标名与改动后的探针口径不一致，已同步并加一致性检查。

**新增交付物**

- `scripts/review_checks.py`（同一检查入口）、`check_docker_context.py`、
  `check_wheel_manifest.py`、`wheel_smoke.py`、`enterprise_smoke.py`、
  `loadtest.py`（重写）。
- `docs/api-contract-v1.json` + 防漂移测试 `tests/test_api_contract.py`。
- `docs/review-remediation/`（修前/修后证据与 27 项台账）。
- `CONTRIBUTING.md` / `SECURITY.md` / 本文件。

### 更早（0.6.0 之前）

- 预注册 E1–E4 与 v2 三组对照；v2 **总判定未通过**，负结果照记。
- 双人标注 kappa 0.414（主口径 60 条）、注入档 0.688。
- clean clone 回归修复（`.gitattributes` 锁 LF、语料依赖显式 skip）。
- 服务层 OWASP 对齐控制：鉴权/限流/结构化日志/stop switch/配置校验。
- 企业形态六个模块与 84.8/100 自评 —— **审查明确「自评分数不计入结论」**，
  这次整改不再拿它当进度。

## 版本号规则

`MAJOR.MINOR.PATCH`：改接口契约或六字段语义 → MAJOR；新增向后兼容能力
→ MINOR；只修 bug / 文档 → PATCH。当前全部改动仍归在 0.6.0 本地开发态。
