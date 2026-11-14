# 独立审查整改 · 修后证据

与 `before.md` 用**同一份脚本、同一套验收条件**。机器可读结果在
`after/review_checks.json`，逐项台账在 `README.md`。

## 一、同一检查入口：修前 1/12 → 第一轮 14/14 → 第二轮 19/19

```bash
python scripts/review_checks.py --gate --out docs/review-remediation/after
# == 19/19 pass, 0 fail ==   GATE_EXIT=0
```

分母从 12 涨到 19：第一轮加了企业模式端到端、告警指标一致性；
第二轮加了持久作业、审计跨进程、v3 预注册、前端会话、数据治理五项。
每项都对应某个 finding 的**验收条件**，不是「文件存在吗」：

| 检查 | 怎么验的 |
|---|---|
| EG-003/context | 标记文件模拟构建上下文，真跑 `.dockerignore` 匹配，8 个敏感标记全排除 |
| EG-007/retention | 真写 3 条 → 按保留期删 → 重算哈希链 |
| EG-015/SLI | 用构造状态计数验算 `classify()` / `sli()`，429/404 不计入可用率 |
| EG-005/stable-4xx | 真起服务发畸形 JSON 与 2MiB body，连跑 5 次全是 4xx |
| EG-010/health-ddl | `h_health` 里不再构造 `core.Store` |
| EG-013/metric-consistency | 告警 yaml 里 6 个指标名逐个在 `app.py` 找来源 |
| EG-001+002/enterprise | 真起服务、真 token、真跨租户查询 |
| EG-009/jobs | 真服务提交/轮询/幂等/取消/隔离/重启 + 提交与完成时间分离 |
| EG-008/audit-mp | 两个独立进程并发写同一个审计库，验无分叉链 |
| EG-017/v3-prereg | 判定线 sha256 冻结、文档与代码一致、v2 归档未动 |
| EG-020/session | 令牌传递/失效反馈/会话隔离/键盘操作 9 项 |
| EG-022/governance | 合成敏感标记从日志/指标/响应/列表/详情逐出口检查 |
| EG-023/gate | CI 8 项门槛齐全 |

## 二、独立验收脚本

```bash
python scripts/enterprise_smoke.py --out docs/review-remediation/probes   # 9/9
python scripts/check_docker_context.py                                     # 5/5
python scripts/check_wheel_manifest.py dist/*.whl                          # PASS
python scripts/wheel_smoke.py --cli <venv evoguard> --workdir <空目录>      # 10/10
python -m unittest tests.test_api_contract -v                              # 8/8
```

`enterprise_smoke.py` 实测到的关键结果：

```
缺配置时启动被拒绝（AGENTLAB_IDENTITY_MAP / AUDIT_DB 未设）
未绑定 token + 伪造 X-Tenant-Id -> 401
绑定 token 自报 evil，实际 tenant=acme，identity_verified=True
reader 角色 POST /api/run -> 403      executor -> 200
acme runs=1，beta runs=0             紧接 acme 请求后 beta 仍是 beta
acme 审计 4 行，链验证 True
```

`wheel_smoke.py` 在**独立 venv、源码树外的空目录**里跑通
`--help / config / scan / intent / demo / serve`，`serve` 下
`/`, `/api/health`, `/api/meta`, `/api/runs`, `/metrics`, `/api/ready`
全部 200，业务库落在工作目录而非安装目录。

## 三、性能：改前改后并列（这里有个不好听的结论）

| 来源 | 工作负载 | `POST /api/run` P95 | 吞吐 |
|---|---|---|---|
| 独立审查（改前 `793c6d1`） | 8 并发 × 36 次，audit off | **3534.7 ms** | 6.96 rps |
| 本轮（改后） | 8 并发 × 24 次 × 3 轮，audit off | **3500.09 ms**（3 轮中位） | 约 4.3–6 rps |

两组参数不完全一致（36 vs 24 次），**不做百分比对比**。但量级几乎一样：
我这轮把 schema DDL 移出热路径、加了索引，**对写路径瓶颈基本没用**。
瓶颈在「同步执行 + SQLite 写锁 + 每步各自开连接提交」，也就是审查的
EG-009 / EG-010 指的地方 —— 这两项在台账里仍然标 ⬜ / 🟡，没有解决。

读路径的数字也没拿来邀功：审查用 12 并发 × 160 次，本轮用 8 并发 × 24 次，
工作负载不同，不能说「P95 从 X 降到 Y」。要下这个结论，得用同一脚本、
同一参数，在改前的提交上再跑一遍 —— 本轮**没做**，所以不写。

原始样本在 `experiments/loadtest_v1/samples.jsonl`，环境/提交/配置指纹在
`experiments/loadtest_v1/result.json`。压测脚本支持 `--baseline --max-regression`，
但绝对门槛需要业务目标，本轮没有，**不擅自冻结一个数字当 SLA**。

## 四、明确没验的（不能拿静态检查冒充）

- **Docker 镜像层**：本机连不上 Docker Desktop 的 Linux daemon，没有真 build。
  `.dockerignore` 与 COPY 清单只做了规则级模拟验证。
- **CI 是否真的绿**：全部工作是本地的，未创建远程仓库、未推送、未发布制品。
  `.github/workflows/test.yml` 的 Linux/Windows wheel job 与 release-gate
  只是配置好了，**没有在 GitHub 上跑过**。
- **真实浏览器**：前端解耦只验了 `Promise.allSettled` 结构与 `node --check`
  语法；桌面/移动/键盘、慢网络、权限不足的交互没有自动化实测。
- **真实企业环境**：没有 SSO、多节点、真实业务流量、第三方合规验收。
  企业模式的身份源是本地 token 映射表，审批后端是 stub 标记。
- **检测器 v3**：预注册未做，v2 的负结果原样保留，没放宽判定线。

## 五、分数旁边现在写了什么

`evoguard scorecard` 仍是 84.8/100。审查说「仓库自评分数不计入结论」，
第一轮在报告顶部加的独立审查口径是：

> 27 项发现中 ✅ 5 完成 / 🟡 12 部分 / ⬜ 10 未做。不要只看分数。

第二轮台账推进到 ✅6 / 🟡17 / ⬜4，`scorecard` 每次重生成会自动读取台账里的
当前数字（`external_review_summary()`），不需要手改文档。

`docs/enterprise-scorecard.md` 已重生成，stdout 与 `--out` 逐字节一致
（`tests/test_enterprise.py::test_stdout_matches_out_file` 守着）。

## 六、第一轮最终复验（提交 `b39e462` 后，干净 clone）

```
主仓库         : Ran 874 tests ... OK                       / 14/14 gate
干净 clone     : Ran 874 tests ... OK (skipped=41)         / 14/14 gate
干净 clone CLI : evoguard config PASS / scorecard --min 80 PASS
干净 clone 制品 : pip wheel . -> 独立 venv 安装 -> check_wheel_manifest PASS
               -> wheel_smoke 10/10（含 serve 六个路由全 200）
```

`skipped=41` 全部是依赖 `data/external/` 语料的用例（该目录按许可与体积
不入库），不是失败。干净 clone 能独立构建可用制品，说明打包配置不依赖
主工作树里的任何未跟踪文件。

**仍然没有验的**：Docker 镜像层（daemon 不可达）、GitHub CI 真实运行
（本轮全部本地，未创建远程仓库/推送）、Linux 平台的 wheel 冒烟
（本机 Windows；CI 矩阵里配了但没跑）。

## 七、第二轮（2026-11-13）：继续推进未完成的项

第一轮收尾时我说「下一轮该动的是 v3 预注册和持久作业」。这一轮做了：

| 项 | 结果 |
|---|---|
| EG-009 持久作业 | `enterprise/jobs.py` + `/api/jobs*` 五条路由；`tests/test_jobs.py` 10/10、`jobs_smoke.py` 11/11。8 并发提交 P95 **14.1 ms** / 完成 P95 1785 ms，请求线程不再被 3.5 秒卡住 |
| EG-008 审计跨进程 | `BEGIN IMMEDIATE` + 流式 `fetchmany`；`tests/test_audit_multiprocess.py` 2/2（两独立进程、80 条事件、无分叉链、5000 行校验） |
| EG-010 存储性能 | **同脚本 / 同参数 / 各自全新隔离库**的改前改后对比（git worktree `b39e462`）：写 P95 3216→3447 ms、rps 5.5→5.35，两边 availability 1.0 —— **没有实质改善** |
| EG-017 v3 预注册 | `docs/semantic-injection-v3-plan.md` + `v3_gate.py`（8 条判定线 sha256 冻结）+ `tests/test_v3_prereg.py` 7/7；**实验未执行**，v2 负结果归档不动 |
| EG-012 / 020 / 022 / 027 | 详情读取预算、前端企业令牌会话、数据分类 + 合成敏感标记 3/3、缓存 LRU+TTL+临时失败不缓存 7/7 |

### 一次按证据撤回的尝试（不好听但必须写）

给业务库加 `PRAGMA journal_mode=WAL` 后，压测看起来「P95 从 3677 降到 1521 ms」，
但 `availability` 只有 0.875 —— 每轮 3–4 个 `500`。P95 变好纯粹是失败请求
快速返回把分布拉低。traceback 是
`sqlite3.OperationalError: attempt to write a readonly database`。
最小复现：24 并发写 3/24 失败；保持常开连接仍剩 1/24。根因是本架构
每个方法都开关独立连接，最后一个连接关闭时删除 `-wal/-shm` 与并发连接竞争。
**撤回 WAL**，只留 `busy_timeout=30s` 与连接计数可观测；撤回后 24/24 成功，
重跑上面那组对比。完整记录见 `perf-before-after.md`。

还有一个坑：`journal_mode` 是数据库文件头里的**持久设置**，撤代码不会自动
还原已跑过的库。压测脚本因此改成每次新建隔离库 —— 否则历史库状态会混进
结果，改前改后根本不可比。

### 第二轮数字

```
python -m unittest discover -s tests -q   Ran 903 tests ... OK
python scripts/review_checks.py --gate     19/19 pass, GATE_EXIT=0
scripts/jobs_smoke.py                     11/11
tests/test_audit_multiprocess.py           2/2
tests/test_v3_prereg.py                   7/7
tests/test_semantic_cache.py               7/7
tests/test_data_governance.py             3/3
```

台账状态从 ✅5 / 🟡12 / ⬜10 变成 **✅6 / 🟡17 / ⬜4**（剩下 EG-014、018、019、026）。
仍未解决：检测 v2 未过线、隔离不是 RLS、写路径性能、单进程、多副本与真实业务流量。
Docker 镜像层、GitHub CI、真实浏览器与 Linux wheel 依旧**未实测**。

## 八、第三轮（2026-11-13 晚）：把「未做」清零

第二轮结束时还剩 EG-014 / 018 / 019 / 026 四项完全没动。这一轮全部推进到
「部分」，**未做项归零**（✅6 / 🟡21 / ⬜0），但完成项仍是 6 —— 不拿「都动过」
当成「都做完」。

| 项 | 做了什么 | 证据 |
|---|---|---|
| EG-014 下游韧性 | `gateway.ResilientProvider`：熔断 + 有界并发 + 统计，接进 `runtime.answer` 的真实调用路径；服务进程默认开，评测脚本不开 | `tests/test_resilient_provider.py` 6/6 |
| EG-018 语料供应链 | `scripts/verify_corpus.py` + `data/corpus_lock.json`（入库不含语料）；篡改判失败、缺许可/未锁定告警 | `tests/test_corpus_supply_chain.py` 7/7；实跑 9 条告警 |
| EG-019 评测进度 | `progress.json` 原子发布 + run_id + 指纹；`real_model_state()` 优先显式进度，损坏不猜 | `tests/test_real_model_progress.py` 6/6 |
| EG-026 审批 | 租户绑定、效果回执、`reconcile()` 待对账列表；有效期/策略版本/幂等键沿用 | `tests/test_approval_tenant.py` 7/7；原有 16 项不破坏 |

### 这一轮的负结果 / 已知缺口

- **EG-018 的上游 commit 仍是 `git_sha: cache` 占位符**，4 个源缺 license。
  锁清单只能证明「生成产物没被改」，**证明不了上游版本**。`--strict` 会失败。
- **EG-014 的超时**仍是底层 socket 超时，没有强制总预算中断；多副本共享
  熔断状态与限流层没做。
- **EG-019 没有跨进程文件锁**：两个评测进程写同一目录仍会互相覆盖进度；
  完成件没有逐字节摘要校验。
- **EG-026 没有真实外部审批人**：`approved_by` 是本地字符串，`mark_effect`
  要调用方自己填；审查标 design_gap 的核心原因仍然成立。

### 第三轮数字

```
python -m unittest discover -s tests -q   Ran 929 tests ... OK
python scripts/verify_corpus.py            PASS（9 条告警）
python scripts/verify_corpus.py --strict   1（预期：上游未锁定）
```

## 九、第四轮（2026-11-14）：连接池 + WAL 的同负载 A/B

第二轮把 WAL 撤回时写过「修好它需要真正的连接池（EG-010 未完成）」。
这一轮把连接池做出来，并用数据验证这个判断。完整报告在
`store-pool-ab.md`，机器可读在 `perf/pool_ab_*.json`（6 次运行）。

| 配置 | write_run P95（三轮） | 中位 | 吞吐 | availability |
|---|---|---|---|---|
| A 默认（每操作开关连接） | 2341/2969/3411 ms | 2969（重复 3627） | 5.1–5.4 rps | 1.0 |
| B 连接池 | 1948/2277/2103 ms | 2103 | 7.4–8.0 rps | 1.0 |
| C 连接池 + WAL | 678/490/565 ms | 565（重复 693/791） | 25–31 rps | 1.0 |

**每轮都是 24×200，没有 5xx** —— 和上轮「P95 变好但 availability 0.875」
的假象不同，这次吞吐同步涨了约 5 倍。`tests/test_store_pool.py` 专门验了
上轮 `readonly` 的根因：常开连接保住 `-wal/-shm`，24 并发写 0 失败。

开池 + WAL 下的端到端：enterprise 9/9、jobs 11/11（完成 P95 1785→324 ms）、
drill 5/5；全量回归 935 OK。

**默认仍关闭。** 这是语义级默认变更，长时稳定性、WAL 备份/恢复、多进程、
SIGKILL 恢复、开池下的全量回归都没验；按 USER 规矩不替张硕决定。

## 十、第五轮（2026-11-14）：补验五条，并更正上轮推荐

上轮把连接池做完、列出「默认开启前五条没验」。这一轮逐条补验，
结果**推翻了上轮推荐**：原本推荐「池 + WAL」（C 组 565~791 ms），
但 WAL 在补验里暴露了真问题。

| 配置 | 稳定性 | 结论 |
|---|---|---|
| B 只开池 | `probes/store_pool_stability_poolonly.json` **3/3** | ✅ 推荐 |
| C 池+WAL | `probes/store_pool_stability_wal.json` **4/5**，`wal-concurrency` 判红 | ❌ 不要用于生产 |

- **WAL 单进程 24 线程并发写，8 轮里 1 轮 `readonly`**；开池+WAL 跑
  935 项全量回归也会偶发挂 1 项。上轮「6 次压测全程无 5xx」是运气，
  只能说明顺利时能跑多快，不能说明可靠。
- **只开池两次 935 OK、0 ResourceWarning**；10 轮 × 2 进程各写 100 条
  全部 200/200；`os._exit` 强杀后 50/50 行恢复、integrity ok；
  2048 次并发写 `created=8`、堆增量 0.15 MiB。
- **WAL 备份硬证据**：连 `-wal` 一起复制恢复 200/200，只复制 `.db`
  恢复 **0/200**。

补验中修了两个真 bug：`_ConnPool.__del__` 只关空闲队列（原来会击穿
正在借用的连接、造成 readonly）；`__init__` 用跨进程原子锁包住整个
初始化（原来两个进程同时建表/切 WAL 会互相踩）。`test_store_pool.py`
的 flaky WAL 断言改成「只开池稳定」，WAL 失败率移到诊断脚本**故意判红**，
不删测试、不标 expectedFailure。

`review_checks.py` 新增 `EG-010/store-defaults` 门槛：防止以后有人把 WAL
悄悄设成默认，或把「判红」改掉。

**更正后的推荐：只开 `AGENTLAB_STORE_POOL=1`，不要开 WAL，默认仍关闭。**
详见 `store-pool-stability.md` 与 `store-pool-ab.md` 顶部的更正版。

## 十一、第六轮（2026-11-14）：把剩下能在本机验的都补掉

`scripts/store_remaining_checks.py`，产物 `probes/store_remaining_checks.json`，
**4/4 全过**：

| 项 | 结果 |
|---|---|
| 真实 `taskkill /F /T` 强杀进程树（不是 `os._exit` 模拟） | 只开池、池+WAL 两种都恢复 120/120 行，`integrity_check=ok` |
| 60 秒 soak / 8 线程 | 19622 次写，`created` 恒为 8、堆峰值 0.15 MiB、0 错误、0 丢弃 |
| 磁盘写入量（200 次写 × 三配置） | 默认 / 只开池 **122.9 B/写**；池+WAL **8610 B/写（约 70 倍）** |

磁盘量给「不要开 WAL」补了第三个独立理由：除了并发 `readonly` 与
备份丢数据，写放大也是数量级的。

复查 Docker daemon：仍报
`npipe:////./pipe/dockerDesktopLinuxEngine ... 系统找不到指定的文件`，
**EG-003 镜像层依旧未实测**，不写成通过。

`review_checks.py` 加 `EG-010/remaining` 门槛（读产物、不重跑 60 秒），
gate 20 → 21 项。

### 到此为止：本机范围内能验的都验过了

剩下没做的不是「还没轮到」，而是**本机做不了或需要张硕决定**：
小时级 soak（要挂着跑数小时）、多主机共享存储、Docker 镜像层、
GitHub CI 真实运行、真实浏览器与 Linux wheel、v3 检测实验（要上游语料
与第二位调参者）、语料上游 commit/license、真实外部审批人、
以及「把只开池设成默认」这个语义级变更（开池下的全量回归已验）。

## 十二、第七轮（2026-11-14）：10 分钟 soak 挖出一个未解决问题

### 做了两件确认完成的事

1. **最新提交的 wheel 制品复验**（`wheel-latest-verification.md`）：
   本轮最终工作树重建 wheel，manifest PASS；
   `enterprise/jobs.py` / `v3_gate.py` 在制品里；独立 venv 内
   `from evoguard import core` 开池可用；默认与开池两种环境
   `wheel_smoke.py` 各 **10/10**。
   `check_wheel_manifest.py` 加 `SOURCE_SYMBOLS` 防止「文件在、内容旧」，
   并把旧源码 wheel 构造出来验证**能判红**（exit 1），不是恒真摆设。
2. **更正上轮一处表述**：「默认开启后的完整回归没验」写错了 ——
   `AGENTLAB_STORE_POOL=1` 下两次 935 OK 就是开启后的行为，已改为「已验」。

### 10 分钟 soak 判红，追出一个未解决问题

`store_remaining_checks.py --soak-seconds 600`：**98314 次写、
1 次 `database is locked`**，soak 3/4（`probes/store_soak600.json` 如实保留）。

- 对照实验 `lock_contention_probe.py`（240s × 16 线程）：
  默认 9/41558、只开池 26/67697 —— **默认路径本来就有，不是连接池引入的**。
- traceback 定位到 `create_experiment` 的 `INSERT ... execute()`，
  **不是 COMMIT**，所以 `busy_timeout` 挡不住。
- 尝试 1「COMMIT 阶段有限重试 + 计数器 + health 字段 + 5 项单测」：
  180s × 16 线程对照错误率与修复前同量级 → **无效，全部撤回**。
- 尝试 2「`BEGIN IMMEDIATE`」raw sqlite 对照：错误率 0.193% → 0.087%，
  **减半但没归零**，改事务模型风险不匹配 → **未接入**。

完整记录见 `lock-contention-known-issue.md`。这是低概率 500、事务
rollback、非数据损坏；不改变「只开池」的推荐（默认路径同样有）。
**状态：未解决，不写成已修复。**

## 十三、第八轮（2026-11-14）：创新方案解决未解决问题，并改了默认值

用户说「自己找地方优化整改，要创新」。选了上轮亲口标成「未解决」的
`database is locked`，换思路用**SQL 感知的连接代理**做读写分离：

- `_WriteGuardConn`：在 `execute()` 边界识别 SQL，读语句完全不加锁，
  写语句才拿按数据库路径共享的进程级写锁；`commit`/`rollback` 释放。
  **零调用点改动**（不用给几十个方法标 `write=True`）、零依赖。
- `_WriteGate` 带 `acquired`/`waiters`，`/api/health` 可观测。
- 池分支归还**原始连接**而非代理，避免嵌套包装。

可行性实验（raw sqlite，24 线程 60s）：18 次 locked → **0 次**，
吞吐 342 → **368 rps（+7.6%）**，因为省掉了冲突回滚。

产品代码 A/B（90s × 24 线程，`probes/serialize_{off,on}.json`）：

| 配置 | 写次数 | 错误 |
|---|---|---|
| 无池 + 关串行化（旧默认） | 19073 | 5 |
| 开池 + 关串行化 | 30242 | 15 |
| 无池 + 开串行化 | 7410 | 0（**吞吐 -61%，不隐藏**） |
| **开池 + 开串行化（新默认）** | **32287** | **0（比旧默认 +69%）** |

因为无池时写锁把「冷连接 + commit fsync」也串行了，所以按 A/B 证据
**把连接池与写串行化两个默认值一起改开**（`=0` 可退回）。WAL 仍默认关。

- 单线程微基准做了 A/B/A/B 交叉验证：120 → 118 ops/s（约 2% 开销），
  推翻了第一次单向测量得到的「-47%」（系统负载噪声）。
- 944 项回归在「新默认」与「全关」两种配置下都全绿；
  重跑稳定性：推荐配置 3/3、池+WAL 5/5（WAL 并发 readonly 从 1/8 → 0/8，
  但样本有限且备份/写放大两个硬理由仍在，**WAL 结论不变**）；
  重跑剩余检查 4/4，60 秒 soak 20657 次写 0 错误；
  **10 分钟 soak 173247 次写 0 错误**（旧默认 98314 次写/1 错），
  `probes/store_soak600_final.json`。
- `tests/test_single_writer.py` 9 项；`review_checks.py` 加
  `EG-010/single-writer` 门槛，gate 21 → 22 项。

完整方案见 `single-writer-innovation.md`；旧报告
`lock-contention-known-issue.md` 顶部加了「已解决」更正块，
失败与两次无效尝试的过程保留不删。

## 十四、第九轮（2026-11-14）：端到端证据 + 审自己的新代码

上一轮的 A/B 都在 `Store` 层，这轮补三件：

1. **真实 HTTP 压测 A/B**：`loadtest.py` 加 `--store-off` 显式对照，
   同参数各跑两次。新默认 `write_run` P95 **1093.2 / 1067.9 ms**
   （8.0–8.7 rps），全关 **2815.8 / 3247.4 ms**（5.2–5.6 rps），
   每轮 24×200、无 5xx，约 **2.7–3 倍**改善且两次重复稳定。
   `EG-010/e2e` 门槛守着这个收益，gate 22 → 23。
2. **修 `_GATES` 无上限**（审自己方案发现的真问题）：普通 dict 会随
   临时库路径无限累积。**没用 LRU 淘汰** —— 淘汰正在被持有的锁会让
   同路径出现两把锁、串行化失效。改成 `WeakValueDictionary` +
   `Store` 持强引用，最后一个 Store 回收时 gate 自动消失；
   `/api/health` 加 `active_write_gates`。新增 2 项测试，共 11 项。
3. **用数据修正上轮「进程内锁」缺点**：新增
   `cross_process_write_probe.py`，4 进程 × 8 线程 × 45 秒同一库文件：
   新默认 **14691 写 / 0 错**，全关 9564 写 / 8 错。
   单主机多进程**不需要跨进程应用层锁**；仍未验的是多主机共享存储。

946 项回归在新默认与全关两种配置下都全绿，23/23 gate。
详见 `single-writer-innovation.md` §八。
