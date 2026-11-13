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

### 第二轮（2026-11-13，同日续）

继续推进审查里第一轮没做完的项。逐项台账见 `docs/review-remediation/README.md`。

- **EG-009** `enterprise/jobs.py` + `/api/jobs*` 五条路由：任务 ID / 轮询 /
  取消 / 幂等键 / 租约；崩溃恢复标 `unknown` 且**不自动重放**（副作用可能
  已发生），人工对账后显式 retry；队列有界、单 worker。同步 `/api/run` 保留兼容。
- **EG-008** 审计 `emit()` / `migrate()` 改 `BEGIN IMMEDIATE` 跨进程事务，
  `verify()` 改流式 `fetchmany`；两独立进程并发写验收 2/2。
- **EG-010** 做了同脚本、同参数、各自全新隔离库的改前改后对比
  （`perf-before-after.md`）：写 P95 3216→3447 ms，**无实质改善**。
  尝试 WAL 造成 3/24 `readonly` 500，**按证据撤回**；压测改每次新建隔离库，
  因为 `journal_mode` 是数据库文件头的持久设置。
- **EG-017** `docs/semantic-injection-v3-plan.md` 独立预注册：8 条判定线
  写死在 `v3_gate.py` 并 sha256 冻结；v2 负结果归档不动。**实验尚未执行**。
- **EG-012/020/022/027** 详情读取预算、前端企业令牌会话（sessionStorage +
  Bearer + 401/403 反馈）、数据分类文档与合成敏感标记验收、语义缓存
  LRU + TTL + 临时失败不缓存。
- 新增测试：test_jobs(10) / test_audit_multiprocess(2) / test_v3_prereg(7) /
  test_semantic_cache(7) / test_data_governance(3)；
  `review_checks.py --gate` 从 14 项扩到 **19 项**。

验收：903 tests OK、19/19 gate、jobs_smoke 11/11。
台账 ✅5/🟡12/⬜10 → **✅6/🟡17/⬜4**（剩 EG-014/018/019/026）。

### 第三轮（2026-11-13 晚）

把台账里最后 4 项「未做」全部推进到「部分」，未做项归零（✅6 / 🟡21 / ⬜0）。

- **EG-014** `gateway.ResilientProvider`：熔断 + 有界并发 + 调用统计，
  接进 `runtime.answer` 的真实调用路径；服务进程默认开、离线评测不开
  （避免熔断污染真实失败率）。6 项测试。
- **EG-018** `scripts/verify_corpus.py` + `data/corpus_lock.json`：
  锁 `cases.jsonl` 的 sha256/行数/许可；篡改判失败；缺许可与上游未锁定
  记为告警，`--strict` 判红。7 项测试；实跑 9 条告警（4 源缺 license、
  全部 commit 未锁定 —— 如实不补假值）。
- **EG-019** 评测写 `progress.json`（run_id + 状态 + 指纹，tmp+fsync+
  os.replace 原子发布）；`/api/real-model` 优先显式进度，损坏标
  `progress_corrupt` 不猜。6 项测试。
- **EG-026** 审批加 `tenant_id / effect_status / effect_receipt`；
  跨租户批准与复用直接拒绝；`mark_effect()` 记外部回执；`reconcile()`
  列「已消费但无回执」供人工对账，不自动重放。7 项测试，原有 16 项不破坏。

验收：929 tests OK。仍未解决：上游 commit 未锁、审批没有真实外部审批人、
超时无强制预算、评测进度无跨进程锁、写路径性能、单进程、多副本。
