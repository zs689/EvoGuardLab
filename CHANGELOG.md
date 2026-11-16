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

### 第四轮（2026-11-14）：连接池与同负载 A/B

- `core.py` 新增有界连接池 `_ConnPool`（复用、异常丢弃、`check_same_thread=False`、
  默认关闭）；`AGENTLAB_STORE_POOL=1` / `AGENTLAB_STORE_POOL_SIZE` /
  `AGENTLAB_STORE_WAL=1`（WAL 必须和池一起开）。
- `loadtest.py` 加 `--store-pool` / `--store-wal` 开关，同一脚本同参数做 A/B。
- 6 次独立运行：默认 2969/3627 ms → 开池 2103 ms → 池+WAL 565/693/791 ms，
  吞吐 5 → 25~31 rps，全部 200。报告 `docs/review-remediation/store-pool-ab.md`。
- `tests/test_store_pool.py` 6/6，含「WAL + 常开连接 24 并发 0 失败」——
  证实第二轮撤回 WAL 的根因确实是没有常开连接。
- 默认**仍关闭**：长时稳定性、WAL 备份恢复、多进程、SIGKILL 恢复、
  开池下全量回归未验。935 tests OK。

### 第五轮（2026-11-14）：补验稳定性并更正第四轮推荐

**更正**：第四轮推荐「连接池 + WAL」是错的。稳定性验收发现 WAL 单进程
24 并发约 1/8 概率 `readonly`，开池+WAL 跑 935 项全量回归偶发挂 1 项。
初版 6 次压测「全程无 5xx」是运气，不能当稳定性证据。

- 新增 `scripts/store_pool_stability.py`（`--no-wal` 双配置）；
  产物 `probes/store_pool_stability_{poolonly,wal}.json`。
- B 只开池 3/3 全过（2048 次并发写有界、10 轮多进程、强杀恢复、
  两次 935 OK、0 泄漏）；C 4/5，`wal-concurrency` 故意判红。
- WAL 备份实测：连 `-wal` 恢复 200/200，只复制 `.db` 恢复 0/200。
- 修 `_ConnPool.__del__`（只关空闲队列，避免击穿借用中的连接）与
  `__init__` 跨进程原子初始化锁。
- `test_store_pool.py` 的 WAL flaky 断言改为「只开池稳定」；
  `review_checks.py` 加 `EG-010/store-defaults` 门槛。
- 文档更正：`.env.example` / SECURITY / data-governance /
  enterprise-readiness / 台账 EG-010 / store-pool-ab.md 顶部醒目更正。

**推荐：只开 `AGENTLAB_STORE_POOL=1`，不要开 WAL，默认仍关闭。**

### 第六轮（2026-11-14）：补完本机可验的剩余项

- 新增 `scripts/store_remaining_checks.py`（`--soak-seconds`），
  产物 `probes/store_remaining_checks.json`，**4/4**：
  - 真实 Windows `taskkill /F /T` 强杀进程树（只开池 / 池+WAL 都恢复
    120/120 行，integrity ok），不再只是 `os._exit` 模拟。
  - 60 秒 soak / 8 线程共 19622 次写，`created` 恒为 8、
    堆峰值 0.15 MiB、0 错误、0 丢弃。
  - 磁盘写入量：默认与只开池 122.9 B/写，**池+WAL 8610 B/写（约 70 倍）**
    —— 「不要开 WAL」的第三个独立理由。
- 复查 Docker daemon 仍不可达，EG-003 镜像层如实保持未实测。
- `review_checks.py` 加 `EG-010/remaining`（读产物不重跑），gate 20→21。
- 文档同步：store-pool-stability.md §四、台账 EG-010、.env.example、
  SECURITY、enterprise-readiness、after.md §十一。

### 第七轮（2026-11-14）：10 分钟 soak 与一个未解决的已知问题

- **wheel 制品复验**：最新提交重建 wheel，manifest PASS、
  独立 venv 开池可用、默认与开池 wheel_smoke 各 10/10。
  `check_wheel_manifest.py` 加 `SOURCE_SYMBOLS`（防旧源码制品），
  并用构造的旧 wheel 验证可证伪。
- 更正上轮「默认开启后回归未验」的错误表述（其实已跑两次 935 OK）。
- **10 分钟 soak 判红**：98314 次写出现 1 次 `database is locked`。
  对照证实默认路径与连接池都有（0.1–0.4‰），traceback 定位在 INSERT。
  COMMIT 重试无效（已撤回全部代码/测试/health 字段）；
  `BEGIN IMMEDIATE` 只减半错误率（未接入）。
  如实记为**未解决**的已知边界，见
  `docs/review-remediation/lock-contention-known-issue.md`。
- 新增诊断脚本 `lock_contention_probe.py`、`begin_immediate_probe.py`；
  新增 `/api/health` 的 `pool_stats` 字段（连接复用可观测）。

### 第八轮（2026-11-14）：SQL 感知的写串行化（创新 + 默认值变更）

- 新增 `_WriteGuardConn` / `_WriteGate`：在 SQL 边界识别读写，
  读不加锁、写串行化，零调用点改动、零依赖；`/api/health` 加
  `write_gate` / `serialize_writes` 可观测。
- 解决上一轮判定「未解决」的 `database is locked`：
  raw 实验 18 次 → 0 次、吞吐 +7.6%；产品 A/B 旧默认
  19073 写/5 错 → 新默认 32287 写/0 错（+69%）。
- **按 A/B 证据把 `AGENTLAB_STORE_POOL` 与
  `AGENTLAB_STORE_SERIALIZE_WRITES` 默认值改为开**（`=0` 可退回）；
  `fast=True` 离线评测自动跳过；WAL 仍默认关且不推荐。
- 同步修了 A/B 脚本的显式 env（`default` 组必须真无池）、
  `test_store_pool.py` 默认断言、`EG-010/store-defaults` 门槛。
- 新增 `tests/test_single_writer.py` 9 项、
  `scripts/single_writer_probe.py`、`EG-010/single-writer` 门槛；
  gate 21 → 22 项。
- 新默认下 10 分钟 soak **173247 次写 0 错误**（旧默认 98314 次/1 错），
  `probes/store_soak600_final.json` 4/4。
- 报告 `single-writer-innovation.md`；旧 `lock-contention-known-issue.md`
  顶部加「已解决」更正块，失败过程保留。

### 第九轮（2026-11-14）：端到端证据、弱引用闸门、跨进程验证

- `loadtest.py` 加 `--store-off`；真实 HTTP A/B 两次重复：
  新默认 `write_run` P95 1093/1068 ms vs 全关 2816/3247 ms
  （约 2.7–3 倍），每轮 24×200 无 5xx。
  新增 `EG-010/e2e` 门槛，gate 22 → 23。
- **修真问题**：`_GATES` 由普通 dict 改为 `WeakValueDictionary` +
  `Store` 持强引用，避免临时库路径无限累积；没用 LRU 是因为淘汰
  正在持有的锁会让同路径出现两把锁。`/api/health` 加
  `active_write_gates`；`tests/test_single_writer.py` 9 → 11 项。
- 新增 `scripts/cross_process_write_probe.py`：4 进程 × 8 线程 × 45s，
  新默认 14691 写/0 错 vs 全关 9564 写/8 错 → 单主机多进程不需要
  跨进程应用层锁；上轮那条「缺点」按数据修正，多主机共享存储仍未验。
- 946 项回归在开与关两种配置下全绿，23/23 gate。

### 第十轮（2026-11-15）：语料核实、Docker/WSL 探测、SQL 前缀加固

- **修 EG-018 校验器 bug**：`verify_corpus.py` 改读嵌套
  `source`/`sources`，区分 `content_pinned` 与 `commit_verified`；
  新增 `data/corpus_upstream.json`（联网核实的许可/版本/证据 URL/可信度）；
  `tests/test_corpus_upstream.py` 6/6；`--write-lock` 重建锁清单。
  5 源 license 全齐、告警 9→4；InjecAgent commit 有 sha256 印证。
  **`--strict` 仍判红**，没为变绿放宽。
- Docker：启动实测失败（backend 卡在拉 desktop.docker.com modules
  manifest），如实不写「镜像层实测通过」；新增
  `scripts/check_build_context.py` 静态验证 PASS（180 文件/2.2 MiB）。
- WSL：`wsl.exe` 被安全策略黑名单拦截，未绕过；只读探测记录环境状态。
- `_WriteGuardConn._normalize()` 处理 SQL 注释前缀与 `WITH … INSERT`，
  `tests/test_single_writer.py` 11→13 项。
- `what-i-need.md` / `corpus-supply-chain.md` / 台账 EG-018 同步进展。

### 第十一轮（2026-11-15）：v3 冻结集与判定线可复算（实验仍未跑）

- 新增 `scripts/build_v3_frozen_split.py`：确定性生成
  `experiments/semantic_injection_v3/frozen_split.json`（288 条 / 24 模板组
  整组划分 / 五集合 × 五切片 / 未见 schema 与校准集无交集 /
  每集合 sha256 / integrity 锁死 / `--freeze` 拒二次生成 / 篡改可检出）。
- 新增 `tests/test_v3_frozen_split.py` 10 项，含八条判定线逐条越线
  可证伪验证；`GATE` 指纹保持不变。
- **`result.json` 仍不存在**，`test_experiment_not_yet_executed` 保持为红；
  独立复跑前置条件未满足，工具链就绪不等于实验有结论。
- v3 plan 顶部加进展块（§六 判定线未动）、台账 EG-017、
  after.md §十六 同步。

### 第十二轮（2026-11-15）：前端可访问性 + 写闸门并发压测

- 新增 `scripts/check_frontend_a11y.py`：首跑发现 9 个 input/select
  只有 placeholder、无可访问名称（读屏听不到用途）。补 `aria-label`
  后 PASS（10 input / 6 button / 0 问题 / lang 与 :focus 齐全）。
  边界：JS 挂 onclick 的 `tr`/`.nav-item` 键盘可达性需真实浏览器。
  新增 `EG-020/frontend-a11y` 门槛。
- 新增 `scripts/stress_write_gate.py`：200 线程 × 3 轮，
  acquired/waiters/locked 三轮全对，峰值排队 177–179。
  新增 `EG-010/gate-stress` 门槛。
- HTTP A/B 从各 2 次加到**各 5 次**：新默认中位 1242.87 ms
  （1233.7–1359.6）vs 全关 3829.38 ms（3167.2–4304.1），
  十次零重叠、全部 availability 1.0、无 5xx，约 3.1 倍。
  `EG-010/e2e` 门槛改读 10 次汇总；产物 `probes/http_ab_repeat/`。
- **30 分钟连续 soak**：431799 次写 / 0 错误、`created` 恒为 8、
  `discarded=0`、堆 0.161 → 0.216 MiB 无明显增长、360 采样点，
  4/4（`probes/soak1800.json`）。仍未到小时级，但连续时长翻三倍。
- `EG-010/e2e` 门槛改读 10 次汇总；新增 `EG-020/frontend-a11y`、
  `EG-010/gate-stress`，gate 23 → 25 项。

### 第十三轮（2026-11-15 晚）：Docker 镜像层实测，EG-003 转 ✅

- 手动启动 Docker Desktop（未改安全设置/未放行 wsl.exe），
  daemon 29.7.2/linux 可用。境外拉 `python:3.11-slim` 慢，
  改国内源拉取后 tag 成本地名，未改 Dockerfile/Docker 配置。
- **修真 bug**：Dockerfile 漏拷 `ai-service/evoguard_inspect`，
  导致 `pip install -e .` 在容器里失败、镜像根本构建不起来。
  补 COPY；`check_build_context.py` 增加「pyproject 声明覆盖」校验。
- 新增 `scripts/verify_docker_image.py`（参数列表调用，避免
  cmd.exe 单引号造成的假绿）与 `docs/review-remediation/
  docker-image-verification.md`：**6/6 通过** —— 镜像层无敏感数据、
  历史层无整仓 COPY、uid 10001、只读根+卷下 healthy、
  卷/镜像层分离、Linux wheel 安装/import/CLI 全通。
  镜像 `6e0dba1f50a0` / 191MB。
- 台账 EG-003 🟡→✅（✅7/🟡20/⬜0）；EG-004/EG-016 补实测证据。
  独立审查 scorecard 84.8 不变。

### 第十四轮（2026-11-15）：CI 静态校验与小时级 soak

- **修 CI 真缺口**：`test.yml` 的 Docker 静态门禁漏跑
  `check_build_context.py`（挡住 evoguard_inspect 漏拷的脚本），
  改成两个脚本都跑，参数分别 `--out` / `--json`。
- 新增 `scripts/check_ci_references.py`：静态校验 CI 引用的脚本存在、
  参数被 argparse 定义、6 个关键门槛脚本都在 workflow 里；
  修了自身「注释行也算引用」的假绿，可证伪验证通过。
  `EG-023/ci-references` 门槛，gate 26 → 27。
- 更正上轮归类错误：小时级 soak 本机可做，启动
  `--soak-seconds 3600` 连续 60 分钟补验，替代此前最长 30 分钟。
- 长时验证前修了 `store_remaining_checks.py` 的竞态：
  `_dir_bytes()` 遍历时 SQLite 临时 journal 可能消失导致
  `FileNotFoundError`（首次 3600s 跑 12 秒即崩），已加 OSError 保护。
- **60 分钟连续 soak 完成**：979904 次写 / 0 错误 / created 恒 8 /
  discarded 恒 0 / 720 采样点，4/4（`probes/soak3600.json`）。
  堆 0.153→0.482 MiB 缓慢上升（采样列表自身在 tracemalloc 范围内），
  **既不证明也不排除泄漏**，更正上轮「堆无明显增长」的说法。
  要下无泄漏结论需排除采样开销的独立长跑，未做。

### 第十五轮（2026-11-15）：堆归因结论确定 + 稳定性三层约束

- **推翻第十四轮「既不证明也不排除泄漏」**：三条证据定位为
  观测器自身开销（720 采样点实测 0.285 MiB / 60 分钟涨 0.329 MiB），
  **非 Store 泄漏**。对照实验不留采样点组堆 -0.010 MiB。
- `check_soak` 改为采样点写临时 JSONL、内存只留 float 列表、
  tracemalloc 停止后读回（产物格式不变、临时文件删除）。
- 新增运行期硬判据 `heap_growth_mb < 0.15`、
  `tests/test_soak_heap.py`、`EG-010/heap-stability` 门槛。
- 新增 `scripts/heap_attribution_probe.py` 与
  `docs/review-remediation/heap-stability.md`。
- 修后 30 分钟长跑 4/4：436576 次写 0 错，堆 0.137→0.053 MiB
  （-0.084，无上升趋势），`heap_stable=true`；门槛可证伪验证通过。
  965 tests OK / 28-28 gate。

### 第十六轮（2026-11-16）：AgentWatch —— 监测其他 Agent + 强制防护

用户把目标重定义为「监测其他 agent 运行/未运行时的 bug 与漏洞，
并防止人/AI 入侵被监测 agent」。新增独立能力层
`ai-service/evoguard/agent_watch.py`（不改既有 security/runtime/agents 语义）：

- `AgentTarget`：callable / HTTP / 声明式 manifest 三种方式接入**外部** agent；
  协议里 tool_outputs 是不可信文本并进入决策 —— 间接注入链真实存在。
- `static_audit()`：不运行 agent，查 9 类配置/权限/声明漏洞。
- `runtime_probe()`：正常/间接注入/越权/工具故障四类探针，
  判定 hijack / unauthorized / silent_failure 并保留原始 tool_calls。
- `GuardGateway`：**默认 enforce=True**（与 DEFAULT_MODE=audit 相反），
  输入/工具输出/工具调用三处真拦，deny 时不透传 agent 结果。
- `scripts/agent_watch_proxy.py`：HTTP 防护代理，`/invoke` deny 返 403；
  `/bypass` 默认关闭，仅用于对照验证。
- 顺带修红队清单两条已知漏检：`_PRE` 的
  `injection_tool_directive` / `exfil_encoded_blob` 前置子串写错；
  并把 `injection_tool_directive` 正则扩到「执行脚本:」「运行命令：」。
  网关层另用不依赖 `_PRE` 的全正则扫描兜底。

验证：`verify_agent_watch.py --with-http` **8/8**，
含「关网关攻击成功 / 开网关被拦」「直连后端成功 / 经代理 403」对照；
`tests/test_agent_watch.py` **13/13**；新增
`AGENT-WATCH/verification` 门槛，gate 28 → 29。
978 tests OK / 29-29 gate / wheel 含 agent_watch.py。

**边界**：原有 app.py 默认仍是 audit，新能力需显式部署到入口；
规则级非语义级；静态审计非源码 SAST；未接真 LLM 做可疑判定；
单机；误报代价上升。详见 `docs/agent-watch.md` §三。
