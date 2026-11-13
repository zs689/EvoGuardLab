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

---

## 修前 / 修后证据

```bash
python scripts/review_checks.py --out docs/review-remediation/before   # 修前 1/12 pass
python scripts/review_checks.py --gate --out docs/review-remediation/after  # 修后全绿
```

| 检查入口 | 结果 | 产物 |
|---|---|---|
| `scripts/review_checks.py --gate` | 14/14 pass | `after/review_checks.json` |
| `scripts/enterprise_smoke.py` | 9/9 pass | `probes/enterprise_smoke.json` |
| `scripts/wheel_smoke.py` | 10/10 pass | 独立 venv，源码树外空目录 |
| `scripts/check_docker_context.py` | 5/5 pass | 标记文件模拟（**镜像层未实测**） |
| `scripts/check_wheel_manifest.py` | PASS | 对 wheel 制品本身检查 |
| `tests/test_api_contract.py` | 8/8 | 契约与 ROUTES 不漂移 |
| `python -m unittest discover -s tests -q` | 见 `docs/upgrade-report.md` 最终数字 | 全量回归 |

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
| **EG-017** 检测 v2 未过线 | ⬜ | **如实保留负结果**，没放宽判定线、没改 test、没重写结论 | `experiments/semantic_injection_v2/result.json`、`docs/enterprise-readiness.md` §四 | v3 预注册、独立任务分布/模型漂移切片、第三方复跑**都没做**。这是科学问题，不能靠加代码糊过去 |
| **EG-018** 语料供应链 | ⬜ | — | — | 预先锁定上游不可变版本/预期哈希/逐资产许可清单未做 |
| **EG-021** 首次体验 | 🟡 | `loadAll()` 拆成「必需元信息」+「可选面板 `Promise.allSettled`」，任一可选请求失败只让对应面板显示空状态；`fillTasks()` 对 `META` 缺失加保护；中英 README 补五分钟离线 demo | `review_checks` EG-021（`node --check` 语法通过） | **真实浏览器**（桌面/移动/键盘、慢网络、权限不足）未实测，本机没有浏览器自动化 |
| **EG-023** CI 客观门槛 | ✅ | 同一检查入口 `review_checks.py --gate`；构建上下文门禁；wheel 清单 + CLI 冒烟；Linux/Windows 双平台 venv；`release-gate` 汇总 job；证据上传；自评分数降级为提示，**不单独放行** | `review_checks` EG-023（8 项门槛齐全）；`.github/workflows/test.yml` | 全部为本地配置，**CI 未在 GitHub 上跑过**；无法声称 CI 绿 |

## 阶段 B

| ID | 状态 | 做了什么 | 证据 | 没做 / 边界 |
|---|---|---|---|---|
| **EG-006** 审计可靠性 | 🟡 | 运行时审计写失败不再 `except: pass`：计数 + `evoguard_audit_failures_total` 指标 + `/api/ready` 变 503；企业模式把审计状态纳入就绪判定 | `app.py:record_audit_failure()/audit_state()`；`deploy/alerts.yml` 新增 page 级告警 | 同事务审计 / 持久 outbox、背压、重试与去重、按 request_id 对账流程**未做** |
| **EG-007** 审计完整性与保留期 | ✅ | 新增 `audit_anchors` 归档锚点（migration v4）：合法清理按连续区间留锚、`verify()` 只在缺口处匹配锚点才放行；支持按租户清理；手工 `DELETE` 仍报 `prev_mismatch`；伪造锚点哈希对不上也不能洗白 | `tests/test_enterprise.py::TestAuditRetention` 5/5；`review_checks` EG-007 | 锚点表与审计库同库 —— 能改库的人理论上可插锚点。生产需把锚点备份到独立权限域（已写进 `SECURITY.md`） |
| **EG-008** 审计并发和迁移 | ⬜ | — | — | 跨进程原子链头、独立迁移锁、流式校验/有界内存**未做**；仍是单进程假设 |
| **EG-009** 任务调度与故障恢复 | ⬜ | — | — | 持久作业 ID / 轮询 / 取消 / 重试 / 幂等键 / 租约**未做**。`POST /api/run` 仍同步阻塞（审查实测 8 并发 P95 3.5s） |
| **EG-010** 存储性能 | 🟡 | `get_store()` 按路径缓存已初始化实例，热路径不再每次跑 schema DDL；`skills/experience` 建表移出请求路径；`runs.started_at` 加索引避免 `SCAN + TEMP B-TREE` | `review_checks` EG-010；`/api/live` 不碰 DB | 连接池、SQLite `journal_mode` 调优、锁等待剖析、改前改后同负载 P95 对比**未做**（审查要求「瓶颈占比尚需剖析」，这里不冒充做了） |
| **EG-012** 查询与响应体容量 | 🟡 | `limit` clamp 到 `MAX_PAGE_SIZE=200`；`started_at` 稳定游标分页 + `next_cursor`；双字段稳定排序；列表建索引 | `review_checks` EG-012；契约里的分页说明 | 详情聚合读取预算、技能/经验分页、EXPLAIN 命中索引的大样本验证**未做** |
| **EG-013** 探针与可观测性 | ✅ | `/api/live`（不碰 DB）、`/api/ready`（依赖不满足 503）、`/api/health`（诊断，保留原字段）拆分；探针流量单列 `evoguard_probe_requests_total`，**不进业务 SLI**；告警规则同步并加指标名一致性检查 | `review_checks` EG-013 两项；`deploy/alerts.yml` 新增 probe-scrape / audit-failure 告警 | 真实部署拓扑里的抓取凭证、错误预算与一条完整追踪**未验** |
| **EG-014** 下游韧性 | ⬜ | — | — | 熔断/超时未进业务调用路径；限流仍进程内；半开探测数、共享限流层**未做** |
| **EG-016** 生产部署与灾备 | 🟡 | Dockerfile：非 root、只读友好、独立卷、healthcheck、优雅停止；`AGENTLAB_BIND` 可配；`.env.example` 补全套配置 | 同上 EG-003 | 反代 TLS 拓扑、私有依赖、备份/恢复演练、RPO/RTO、证书轮换与回滚 runbook**未做** |
| **EG-019** 实验产物并发读取 | ⬜ | — | — | run ID、显式进度计数、原子发布清单、完成件摘要校验**未做**；`h_real_model` 仍按 mtime 推断并全量扫 JSONL |
| **EG-020** 企业前端会话 | ⬜ | — | — | 开启企业鉴权后浏览器无法按现有流程登录；会话/反代认证、失效与登出、CSRF 保护**未做** |
| **EG-022** 数据治理与保密 | ⬜ | — | — | 字段分类/保留期/敏感值最小化、租户级删除导出、保留冻结、加密配置、合成敏感标记验证**未做**。只在 `SECURITY.md` 列了已知边界 |
| **EG-026** 审批与真实副作用恢复 | ⬜ | — | — | 租户级审批人绑定、审批有效期、策略版本、执行幂等键、外部效果回执、崩溃对账**未做**（审查标为 design_gap） |

## 阶段 C

| ID | 状态 | 做了什么 | 证据 | 没做 / 边界 |
|---|---|---|---|---|
| **EG-024** 未来开源采用 | 🟡 | 中英首页补五分钟离线 demo 与「看什么」；`CONTRIBUTING.md` / `SECURITY.md` / `CHANGELOG.md`；README 去掉写死的 814 计数，改成「以命令输出为准」；新增文档链接 | README.md / README.zh.md 顶部 | 未由**未参与开发的人**从干净环境实测五分钟跑通；CITATION、issue 模板、路线图未做 |
| **EG-025** API 与集成生态 | 🟡 | `docs/api-contract-v1.json`：16 条路由 + 静态路由、请求/响应、13 个稳定错误码、分页、审计、六字段冻结契约、已知缺口；`tests/test_api_contract.py` 8 项防漂移 | 契约测试 8/8 | 最小 Python SDK、成功/失败/限流/轮询/审计查询的**集成示例**、跨版本兼容测试**未做** |
| **EG-027** 模型缓存与资源治理 | ⬜ | — | — | 缓存 TTL/容量/同键合并/临时失败隔离、按模型版本与授权作用域命名**未做** |

---

## 汇总（不粉饰）

| 状态 | 数量 | ID |
|---|---|---|
| ✅ 完成 | **5** | EG-002, EG-004, EG-007, EG-013, EG-023 |
| 🟡 部分 | **12** | EG-001, 003, 005, 006, 010, 011, 012, 015, 016, 021, 024, 025 |
| ⬜ 未做 | **10** | EG-008, 009, 014, 017, 018, 019, 020, 022, 026, 027 |
| 合计 | **27** | 与审查 `finding_count` 一致 |

「✅」的判据是**该项的全部验收条件都能给出可复验证据**。差一条就降到 🟡：
例如 EG-003 的镜像层没实测、EG-024 没让第三方实测五分钟跑通，都不算完成。
没做的 10 项没有被写成「预留接口已具备」而混过去。

**结论**：工程形态与可验证据补齐了一大截，但审查的三个核心判断没变：
1. 核心检测 v2 **仍未过自己的预注册线**；
2. 隔离是应用层 + 按租户分库，**不是数据库 RLS**；
3. 单进程同步执行、进程内限流，**没做多副本与真实业务流量验证**。

所以仍然不说「企业级」。这轮能说的是：**审查指出的问题里，能在本机复现并修掉的，
都修了并留了修前修后证据；修不了的，逐条写清了为什么。**

## 交接规则遵守情况

| 规则 | 情况 |
|---|---|
| 按 `depends_on` 拆小批提交 | 按批次提交，每批独立可复验 |
| 先复现差距再修改 | `before.md` + `before/review_checks.json` |
| 保留修前/修后证据 | `before/` 与 `after/`、各 `*_smoke.json` |
| 保留离线零依赖 demo / CLI / 归档实验 | `dependencies = []` 未动；`evoguard demo` 在 checkout 与 wheel 均可跑；冻结产物未改 |
| API / 数据库 / 产物格式迁移与兼容 | 审计 schema v3→v4 幂等迁移；`/api/metrics` JSON 契约保留；`docs/api-contract-v1.json` |
| 六字段契约与真实副作用断言 | 契约里冻结，测试守着；`defense_demo` 仍按 `executed` 计量 |
| 重跑实验写独立输出目录、记录环境/指纹/提交/配置 | `loadtest.py` 记录 revision + environment + config + 原始样本 |
| 只本地工作，不建远程仓库/推送/发布/外发 | 全部本地；未创建远程仓库、未推送、未发外部消息 |
