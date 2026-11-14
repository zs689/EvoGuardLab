# 独立审查整改台账

**审查来源**：`E:/agent/EvoGuardLab_企业与高性能客观审查_2026-11-12.json`
（2026-11-12，revision `793c6d1`，27 项发现：3 P0 / 20 P1 / 4 P2）

**状态定义**（不给自己放水）：

| 标记 | 含义 |
|---|---|
| ✅ | 完成，有**可复验的命令或产物**；不是「代码看起来有这个分支」 |
| 🟡 | 部分完成：核心做了，剩余项明确列出，不算完成 |
| ⬜ | 未做。设计缺口或需要真实企业环境，给出接口/文档位置，**不假装实现** |

**一句先认下来的话**：审查写了「**仓库自评分数不计入结论**」。上一轮我做的
`evoguard scorecard` 84.8/100 是自评，八个维度里有七个是「我写的探针检查
我写的模块」，不构成企业就绪证据。这一轮不拿那个分数当进度，改用
`scripts/review_checks.py` 这种**能返回 0 分、能被证伪**的逐项验收。

**本文件是滚动台账**：第一轮（2026-11-12~13）到 ✅5 / 🟡12 / ⬜10；
第二轮（2026-11-13）继续推进到 ✅6 / 🟡17 / ⬜4。每轮都遵守
「先复现差距 → 再改 → 留修前修后证据」，第二轮的性能对比在
`perf-before-after.md`，其中包含一次**按验收证据撤回**的 WAL 尝试。

---

## 修前 / 修后证据

```bash
python scripts/review_checks.py --out docs/review-remediation/before    # 修前 1/12 pass
python scripts/review_checks.py --gate --out docs/review-remediation/after  # 第一轮 14/14，第二轮 19/19
```

| 检查入口 | 结果 | 产物 |
|---|---|---|
| `scripts/review_checks.py --gate` | **19/19 pass** | `after/review_checks.json` |
| `scripts/enterprise_smoke.py` | 9/9 pass | `probes/enterprise_smoke.json` |
| `scripts/jobs_smoke.py` | 11/11 pass | `probes/jobs_smoke.json`（含提交/完成分离基准） |
| `scripts/wheel_smoke.py` | 10/10 pass | 独立 venv，源码树外空目录 |
| `scripts/check_docker_context.py` | 5/5 pass | 标记文件模拟（**镜像层未实测**） |
| `scripts/check_wheel_manifest.py` | PASS | 对 wheel 制品本身检查 |
| `tests/test_api_contract.py` | 8/8 | 契约与 ROUTES 不漂移 |
| `tests/test_audit_multiprocess.py` | 2/2 | 两个独立进程并发写同一审计库 |
| `tests/test_v3_prereg.py` | 7/7 | v3 判定线指纹冻结、v2 归档未动 |
| `tests/test_jobs.py` / `test_semantic_cache.py` / `test_data_governance.py` | 10/7/3 | 作业状态机、缓存有界、合成敏感标记 |
| `python -m unittest discover -s tests -q` | **935 OK**（干净 clone 同） | 全量回归 |
| `docs/review-remediation/store-pool-ab.md` | 6 次同负载 A/B | `perf/pool_ab_*.json` |
| `docs/review-remediation/store-pool-stability.md` | B 3/3、C 4/5（WAL 判红） | `probes/store_pool_stability_*.json` |

`before.md` 记录了每一条差距的**我自己复现过**的结果。有一条如实写了修前
就是 PASS：审查提到「部分格式错误可能落入 500」，我实测畸形 JSON 返回 400，
没复现；但 2MiB 请求体被完整读入，「无硬上限」成立。不硬凑红。

---

## P0（3 项）

| ID | 状态 | 做了什么 | 证据 | 没做 / 边界 |
|---|---|---|---|---|
| **EG-001** 租户隔离和授权 | 🟡 | 企业模式 token→`(tenant,actor,roles)` 身份绑定（恒定时间比对），**忽略**客户端自报的 `X-Tenant-Id`/`X-Actor`；按租户**分库**（独立 sqlite 文件）；`reader` 不能调用写工具；请求上下文不跨租户泄漏；审计同主体同租户 | `scripts/enterprise_smoke.py` 9/9：伪造头无效、角色拒绝 403、acme runs=1 / beta runs=0、上下文不泄漏、审计链可验 | **不是数据库 RLS**（sqlite 没有），是物理隔离；跨库聚合需企业侧处理。身份源是本地映射表不是 IdP/SSO。缓存键与导出端点不存在，因此无从验证其租户作用域。审批/运维角色矩阵未做全 |
| **EG-002** 企业运行策略 | ✅ | 显式 `demo` / `enterprise` 双模式；企业入口启动期校验 6 项必需配置（token 开关、身份映射、审计库、业务库、审批后端、审计初始化），缺任一项 `SystemExit`；写工具缺 executor 角色 → 403，缺审批后端 → 503 且**不调用写工具** | 同上 `EG-002/fail-closed`；`app.py:validate_enterprise_config()` | 审批后端只是标识字符串，没有真实审批流（见 EG-026） |
| **EG-003** 容器数据暴露面 | 🟡 | `.dockerignore` 黑名单（`.git`/`.env`/`*.db`/语料/`data/tenants`/缓存/审查工作区）；Dockerfile 改**显式 COPY 清单**、非 root(10001)、`VOLUME /app/data`、HEALTHCHECK 走 `/api/live`、STOPSIGNAL + 优雅下线 | `scripts/check_docker_context.py` 5/5：8 个敏感标记全部排除、11 个运行资产保留、无整仓 COPY | **本机 Docker daemon 不可达，没有真 build，镜像层内容未实测**。脚本验证的是两层静态防线，不是 Docker 引擎 |

## 阶段 A 其余项

| ID | 状态 | 做了什么 | 证据 | 没做 / 边界 |
|---|---|---|---|---|
| **EG-004** 独立 wheel CLI | ✅ | wheel 补 `enterprise/` 子包、`evoguard_inspect`、前端/配置/文档/演示脚本 6 类只读资产；CLI 在源码树外能定位已安装包；`config`/`scan`/`intent`/`demo`/`serve` 全可用；checkout-only 命令给明确说明（exit 2，非 traceback） | `scripts/wheel_smoke.py` 10/10（Windows 独立 venv，空目录跑通 `/`、`/api/health`、`/api/meta`、`/api/runs`、`/metrics`、`/api/ready`）；`check_wheel_manifest.py` PASS | CI 里加了 Linux+Windows wheel job，但**未在 GitHub 实跑**（本轮全部本地工作）。Inspector 未作为独立 CLI 冒烟 |
| **EG-005** 请求与资源上限 | 🟡 | 请求体 64KiB 硬上限 → 413（分块排空、内存有界）；500 响应**不再外泄堆栈**，只给稳定错误码 + `X-Request-Id`；畸形输入稳定 4xx | `review_checks` 的 body-limit / stable-4xx，连跑 5 次全 413 | **读写超时、有界工作数、过载队列、RSS 上限**未做；没有结构化 schema 字段预算 |
| **EG-011** 运行数据路径和迁移 | 🟡 | 集中配置 `AGENTLAB_ASSET_ROOT` / `DATA_ROOT` / `DB` / `AUDIT_DB`，默认保持 checkout 兼容；业务库可指向可写卷；按租户分库 | `review_checks` EG-011；wheel serve 实测业务库落在工作目录 | 业务 schema 的**版本迁移、备份/恢复/回滚 runbook** 未做；旧库升级异常配置诊断未做 |
| **EG-015** 性能基线和发布门槛 | 🟡 | 重写压测：四类状态（成功/受控拒绝/服务失败/客户端错误/连接失败）分开报，可用率分母排除 429/404；纳入 `POST /api/run` 写路由；3 轮 × 4 场景；记录 revision、环境、配置；输出原始 `samples.jsonl`；支持 `--baseline --max-regression` | `review_checks` EG-015 用构造计数验算 SLI；真跑一轮见 `experiments/loadtest_v1/result.json` | 固定并发≠固定到达率，未处理 coordinated omission；CPU/峰值 RSS 未采集（零依赖边界）；**绝对门槛未冻结**（没有业务目标，不擅自编数字当 SLA） |
| **EG-017** 检测 v2 未过线 | 🟡 | **v2 负结果原样归档**（`passed=false` 守门）；新写 `docs/semantic-injection-v3-plan.md` 独立预注册：冻结集按 schema 整组划分、新增未见过 schema 验收集与五个切片、以「副作用是否发生」为主指标之一、8 条判定线写死在 `v3_gate.py:GATE` 并用 sha256 冻结 | `tests/test_v3_prereg.py` 7/7；`v2_archive_intact()`；`review_checks` EG-017 | **实验尚未执行**；「由未参与调参的一方复跑」是明确写下的前置条件，本地没有第二位调参者；公开语料仍不可达，`test_unseen_schema` 是确定性合成而非外部分布。不放宽 v2 任何判定线 |
| **EG-018** 语料供应链 | 🟡 | `scripts/verify_corpus.py` + `data/corpus_lock.json`（入库，不含语料本体）：锁每个源的 `cases.jsonl` sha256/字节/行数、license、上游文件数；篡改（改一字节/行数变）判失败；缺许可/上游未锁定记为告警，`--strict` 才判红。`label_sheet` 已排除（是标注页不是语料） | `tests/test_corpus_supply_chain.py` 7/7；实跑：5 个源 `PASS` + 9 条告警（4 源缺 license、全部 commit 未锁定） | **上游 commit 仍是 `git_sha: cache` 占位符，未回填不可变 SHA**；4 个源缺 license/license_url，补齐前不能对外分发；CI 未把它设为硬门槛（干净 clone 无料） |
| **EG-021** 首次体验 | 🟡 | `loadAll()` 拆成「必需元信息」+「可选面板 `Promise.allSettled`」，任一可选请求失败只让对应面板显示空状态；`fillTasks()` 对 `META` 缺失加保护；中英 README 补五分钟离线 demo | `review_checks` EG-021（`node --check` 语法通过） | **真实浏览器**（桌面/移动/键盘、慢网络、权限不足）未实测，本机没有浏览器自动化 |
| **EG-023** CI 客观门槛 | ✅ | 同一检查入口 `review_checks.py --gate`；构建上下文门禁；wheel 清单 + CLI 冒烟；Linux/Windows 双平台 venv；`release-gate` 汇总 job；证据上传；自评分数降级为提示，**不单独放行** | `review_checks` EG-023（8 项门槛齐全）；`.github/workflows/test.yml` | 全部为本地配置，**CI 未在 GitHub 上跑过**；无法声称 CI 绿 |

## 阶段 B

| ID | 状态 | 做了什么 | 证据 | 没做 / 边界 |
|---|---|---|---|---|
| **EG-006** 审计可靠性 | 🟡 | 运行时审计写失败不再 `except: pass`：计数 + `evoguard_audit_failures_total` 指标 + `/api/ready` 变 503；企业模式把审计状态纳入就绪判定 | `app.py:record_audit_failure()/audit_state()`；`deploy/alerts.yml` 新增 page 级告警 | 同事务审计 / 持久 outbox、背压、重试与去重、按 request_id 对账流程**未做** |
| **EG-007** 审计完整性与保留期 | ✅ | 新增 `audit_anchors` 归档锚点（migration v4）：合法清理按连续区间留锚、`verify()` 只在缺口处匹配锚点才放行；支持按租户清理；手工 `DELETE` 仍报 `prev_mismatch`；伪造锚点哈希对不上也不能洗白 | `tests/test_enterprise.py::TestAuditRetention` 5/5；`review_checks` EG-007 | 锚点表与审计库同库 —— 能改库的人理论上可插锚点。生产需把锚点备份到独立权限域（已写进 `SECURITY.md`） |
| **EG-008** 审计并发和迁移 | ✅ | `emit()` 与 `migrate()` 都改用 `BEGIN IMMEDIATE` 跨进程事务（读链尾+插入原子化）；`busy_timeout=30s`；`verify()` 改 `fetchmany(1000)` 流式分批；**按证据撤回 WAL**（多进程共享时 3/10 readonly） | `tests/test_audit_multiprocess.py` 2/2：两独立进程并发启动/写入后版本一致、80 条事件完整、无重复链头、链验证通过；5000 行流式校验；撤回 WAL 后单测 12 次 0 失败、gate 连跑 3 次全绿（`perf-before-after.md` 附） | 仍是单文件 sqlite；多主机共享存储/分布式锁未做 |
| **EG-009** 任务调度与故障恢复 | 🟡 | 新增 `enterprise/jobs.py`：持久作业表（按租户分库）、任务 ID、轮询、取消、重试、**幂等键**、租约；崩溃恢复把过期租约标 `unknown` 且**不自动重放**（副作用可能已发生），人工对账后显式 retry；单 worker 顺序执行；队列容量上限 256；`/api/jobs*` 五条路由，同步 `/api/run` 保留兼容 | `tests/test_jobs.py` 10/10；`scripts/jobs_smoke.py` 11/11（幂等提交 2 次业务库只 1 条 run、租户隔离、重启后记录仍在）；8 并发提交 P95 **14.1 ms** vs 完成 P95 1785 ms | **单进程单 worker**，多副本需共享队列与分布式租约；`runtime.FORCED_SKILL` 仍是进程级全局（用执行锁串行化，未改 runtime）；排队时间未单列报告；同步 `/api/run` 仍在兼容期 |
| **EG-010** 存储性能 | 🟡 | 热路径零 DDL、索引、连接次数可观测；新增**有界连接池** `_ConnPool`（复用/异常丢弃/跨进程初始化锁/默认关）；同脚本同参数、各自全新隔离库的改前改后对比；补做默认开启前的五条稳定性验收 | `store-pool-ab.md`（**已更正**）：A 2969/3627 → **B 只开池 2103 ms（推荐）**；C 池+WAL 顺利时 565~791 ms 但不可靠。`store-pool-stability.md`：B 3/3 全过（2048 次并发写有界、10 轮多进程、强杀恢复、两次 935 OK、0 泄漏）；C 4/5，`wal-concurrency` 8 轮里 1 轮 `readonly` 判红；WAL 备份实测只复制 db 恢复 **0/200** 行 | **默认仍关闭**（语义级变更需点头）；**WAL 判定不可用，不要开**；小时级 soak、多主机共享存储、真实 taskkill/F、默认开启后的回归未做；磁盘写入量未采 |
| **EG-012** 查询与响应体容量 | 🟡 | `limit` clamp 到 200；`started_at` 稳定游标 + `next_cursor`；列表建索引；**详情聚合读取加预算**（四类轨迹各 `LIMIT cap+1`，响应带 `truncated` 与 `detail_limit`） | `review_checks` EG-012；`core.py` 四个 `list_*` 加可选 limit，内部诊断调用不传、语义不变；70 项 core/api 回归全绿 | 技能/经验分页、EXPLAIN 命中索引的大样本验证**未做** |
| **EG-013** 探针与可观测性 | ✅ | `/api/live`（不碰 DB）、`/api/ready`（依赖不满足 503）、`/api/health`（诊断，保留原字段）拆分；探针流量单列 `evoguard_probe_requests_total`，**不进业务 SLI**；告警规则同步并加指标名一致性检查 | `review_checks` EG-013 两项；`deploy/alerts.yml` 新增 probe-scrape / audit-failure 告警 | 真实部署拓扑里的抓取凭证、错误预算与一条完整追踪**未验** |
| **EG-014** 下游韧性 | 🟡 | 新增 `gateway.ResilientProvider`：熔断（失败阈值/冷却/半开）+ 有界并发（超出排队超时直接 `E_OVERLOADED`）+ 调用统计；**接入真实模型调用路径**（`runtime.answer` → `gateway.get_provider()`）。服务进程启动时默认开启（`app.py` setdefault），离线评测**不开** —— 否则熔断会把「模型不行」记成「被自己挡住」，污染实验 | `tests/test_resilient_provider.py` 6/6：熔断打开后不再打下游、冷却半开成功后闭合、并发超限拒绝而非无限排队、get_provider 开关生效 | 超时仍是底层 provider 的 socket 超时，没有强制总预算中断；**共享限流层/多副本熔断状态**未做；半开探测数无独立指标 |
| **EG-016** 生产部署与灾备 | 🟡 | Dockerfile：非 root、只读友好、独立卷、healthcheck、优雅停止；`AGENTLAB_BIND` 可配；`.env.example` 补全套配置 | 同上 EG-003 | 反代 TLS 拓扑、私有依赖、备份/恢复演练、RPO/RTO、证书轮换与回滚 runbook**未做** |
| **EG-019** 实验产物并发读取 | 🟡 | 评测写 `progress.json`（`run_id / status / completed / total / protocol / config_hash / library_hash`），**tmp+fsync+os.replace 原子发布**，`--fresh` 先清旧状态；`/api/real-model` 抽成纯函数 `real_model_state()`，**优先读显式进度**，只有没有进度时才回退 mtime 并标 `source=mtime_legacy`；进度损坏 → `progress_corrupt` 且 not ready，不猜 | `tests/test_real_model_progress.py` 6/6：running 压过更新的 metrics、completed 才放产物、损坏不被当成完成、旧产物标来源 | 没有跨进程文件锁（两个评测进程同时写同一目录仍会互相覆盖进度）；完成件只记录 config/library 指纹，**没有对产物做逐字节摘要校验** |
| **EG-020** 企业前端会话 | 🟡 | 顶栏加令牌输入；所有 `api()` 请求统一带 `Authorization: Bearer`；401/403 给可理解反馈；令牌只存 `sessionStorage`（关标签失效），不进 URL / `localStorage` / DOM 明文；支持空值退出与 Enter 提交 | `review_checks` EG-020（9 项静态检查）；`node --check` 通过 | **真实浏览器登录/刷新/退出、反代认证与角色权限未在部署拓扑实测**；CSRF 保护未做（当前 API 无 cookie 会话） |
| **EG-022** 数据治理与保密 | 🟡 | `docs/data-governance.md`：六类字段分类与保留期、入口最小化、加密/备份部署说明、已知限制；用合成敏感标记 `CANARY_SECRET_9f2c7b1e` 真发请求并从日志/指标/health/列表/详情逐出口验证不泄露 | `tests/test_data_governance.py` 3/3；`service_controls.log()` 白名单 | **租户级删除/导出/保留冻结 HTTP API 未做**（只有按租户保留期清理）；锚点未备份到独立权限域；业务库 `steps.answer/expect` 仍可能含模型输出正文，无字段级脱敏；静态加密与恢复演练未做 |
| **EG-026** 审批与真实副作用恢复 | 🟡 | 审批表加 `tenant_id / effect_status / effect_receipt`（ALTER 幂等迁移）；`approve()` 校验审批人租户与请求一致；`require()` 复用审批时校验租户，跨租户复用直接拒绝；`mark_effect()` 记录外部回执；`reconcile()` 列出「已消费但无回执」的请求供人工对账，**不自动重放**。有效期、策略版本失效、结算幂等键沿用既有实现 | `tests/test_approval_tenant.py` 7/7；原有 `tests/test_approval.py` 16 项不破坏 | **没有真实外部审批人/业务系统回执**：`approved_by` 仍是本地字符串，`mark_effect` 要调用方自己填；没有审批人角色解析（IdP）、没有外部效果查询；多副本下的审批状态共享未做。审查标 design_gap 的核心原因仍然成立 |

## 阶段 C

| ID | 状态 | 做了什么 | 证据 | 没做 / 边界 |
|---|---|---|---|---|
| **EG-024** 未来开源采用 | 🟡 | 中英首页补五分钟离线 demo 与「看什么」；`CONTRIBUTING.md` / `SECURITY.md` / `CHANGELOG.md`；README 去掉写死的 814 计数，改成「以命令输出为准」；新增文档链接 | README.md / README.zh.md 顶部 | 未由**未参与开发的人**从干净环境实测五分钟跑通；CITATION、issue 模板、路线图未做 |
| **EG-025** API 与集成生态 | 🟡 | `docs/api-contract-v1.json`：16 条路由 + 静态路由、请求/响应、13 个稳定错误码、分页、审计、六字段冻结契约、已知缺口；`tests/test_api_contract.py` 8 项防漂移 | 契约测试 8/8 | 最小 Python SDK、成功/失败/限流/轮询/审计查询的**集成示例**、跨版本兼容测试**未做** |
| **EG-027** 模型缓存与资源治理 | 🟡 | `SemanticGuard` 缓存改有界 LRU（默认 4096）+ TTL（3600s）+ 可选 `scope` 作用域隔离；**临时失败 (`unavailable`) 不再入缓存**，下游恢复后真的重试；加载历史 jsonl 时丢弃旧的 unavailable 行；默认缓存键保持不变以免弄脏离线复现证据 | `tests/test_semantic_cache.py` 7/7；`review_checks` 未单列（由 unittest 覆盖） | 同键并发合并、按模型版本命名缓存键、模型请求批处理与峰值内存压测**未做**；`security.py` 的进程内单例仍共享，未接线 `scope` 参数 |

---

## 汇总（不粉饰）

| 状态 | 数量 | ID |
|---|---|---|
| ✅ 完成 | **6** | EG-002, EG-004, EG-007, EG-008, EG-013, EG-023 |
| 🟡 部分 | **21** | EG-001, 003, 005, 006, 009, 010, 011, 012, 014, 015, 016, 017, 018, 019, 020, 021, 022, 024, 025, 026, 027 |
| ⬜ 未做 | **0** | — |
| 合计 | **27** | 与审查 `finding_count` 一致 |

第一轮 ✅5 / 🟡12 / ⬜10；第二轮 ✅6 / 🟡17 / ⬜4；第三轮把剩下 4 项全部推进到「部分」，**未做项归零**。
「✅」的判据是**该项的全部验收条件都能给出可复验证据**。差一条就降到 🟡：
例如 EG-003 的镜像层没实测、EG-024 没让第三方实测五分钟跑通、EG-009
还是单 worker，都不算完成。剩下 4 项没有被写成「预留接口已具备」而混过去。

**结论**：工程形态与可验证据又推进了一批，但审查的核心判断里还有三条没变：
1. 核心检测 v2 **仍未过自己的预注册线**；v3 只完成预注册，实验没跑，
   独立复跑这个前置条件也没满足；
2. 隔离是按租户分库，**不是数据库 RLS**；
3. 单进程、进程内限流、同步兼容路径仍在，**没有多副本与真实业务流量验证**。

第四轮把存储瓶颈往前推了一步，第五轮**更正了第四轮的推荐**：
真连接池让同负载写路由从 2969/3627 ms 降到 2103 ms（只开池，推荐）。
池+WAL 顺利时能到 565~791 ms，但稳定性验收发现单进程 24 并发约 1/8
概率 `readonly`，**判定不可用** —— 初版「6 次全 200」是运气，不能当证据。
五条稳定性已补验：只开池 3/3 全过，WAL 的 `wal-concurrency` 故意判红。
**默认仍关闭**，不替使用者决定。所以仍然不说「企业级」。

## 交接规则遵守情况

| 规则 | 情况 |
|---|---|
| 按 `depends_on` 拆小批提交 | 按批次提交，每批独立可复验 |
| 先复现差距再修改 | `before.md` + `before/review_checks.json` |
| 保留修前/修后证据 | `before/` 与 `after/`、各 `*_smoke.json` |
| 保留离线零依赖 demo / CLI / 归档实验 | `dependencies = []` 未动；`evoguard demo` 在 checkout 与 wheel 均可跑；冻结产物未改 |
| API / 数据库 / 产物格式迁移与兼容 | 审计 schema v3→v4 幂等迁移；`/api/metrics` JSON 契约保留；`docs/api-contract-v1.json` |
| 六字段契约与真实副作用断言 | 契约里冻结，测试守着；`defense_demo` 仍按 `executed` 计量 |
| 重跑实验写独立输出目录、记录环境/指纹/提交/配置 | `loadtest.py` 记录 revision + environment + config + 原始样本；**每次新建隔离临时库**（第二轮修正，否则历史库状态会混进结果） |
| 生产默认值与性能门槛按验收证据决定 | WAL 尝试造成 3/24 `readonly` 500 → 按证据撤回，保留 delete 日志；详见 `perf-before-after.md` |
| 只本地工作，不建远程仓库/推送/发布/外发 | 全部本地；未创建远程仓库、未推送、未发外部消息；`git worktree` 用完已移除 |
