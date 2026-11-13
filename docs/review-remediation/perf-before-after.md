# EG-010 存储性能：同负载改前改后对比

方法遵循审查 handoff_rules「重跑实验写入独立输出目录，记录环境、数据指纹、
提交和配置」以及 EG-010 验收「在**相同业务工作负载**比较改前改后」。

## 对比设置

| 项 | 值 |
|---|---|
| 改前 | `git worktree add --detach <dir> b39e462`（上一轮最终提交） |
| 改后 | 当前工作树（含本轮未提交改动；`git rev-parse HEAD` 仍是 `b39e462`，所以产物里的 revision 字段**不能**用来区分两边） |
| 脚本 | 同一份 `scripts/loadtest.py` + `live_client.py`（复制到 worktree 覆盖） |
| 参数 | `--requests 24 --workers 8 --rounds 3` |
| 数据 | 每次运行新建隔离临时目录，`AGENTLAB_DATA_ROOT` / `AGENTLAB_DB` 都指向它 |
| 环境 | Python 3.13.14 / SQLite 3.53.1 / 同机 Windows / 回环 HTTP / mock 模型 |
| 场景 | `read_health`、`read_runs`、`write_run`（POST /api/run）、`read_metrics` |

机器可读原始结果：`perf/before.json`、`perf/after.json`；延迟原始样本在各自
运行时的 `samples.jsonl`（未随仓库分发，可由同命令重建）。

## 结果（availability 两边都是 1.0，无 5xx）

| 场景 | 改前 P95(3 轮中位) | 改后 P95 | 变化 | 改前 P95 三轮 | 改后 P95 三轮 |
|---|---|---|---|---|---|
| read_health | 26.44 ms | 23.13 ms | −12.5% | 65.1 / 16.5 / 26.4 | 60.4 / 23.1 / 21.4 |
| read_runs | 14.60 ms | 23.26 ms | +59.3% | 14.6 / 16.6 / 13.3 | 24.7 / 15.0 / 23.3 |
| **write_run** | **3216.41 ms** | **3446.68 ms** | **+7.2%** | 2149 / 3223 / 3216 | 2970 / 3704 / 3447 |
| read_metrics | 22.96 ms | 21.77 ms | −5.2% | 26.3 / 23.0 / 22.4 | 19.5 / 21.8 / 24.8 |

写路由吞吐（rps）：改前 `5.95 / 5.49 / 5.20`，改后 `5.38 / 5.34 / 5.35`。

**结论：没有实质性能改善。** 读路径的几个百分点都在三轮波动范围内；
写路径甚至略慢（+7.2%），但同样落在噪声里（改前单轮最低 2149 ms、
改后单轮最高 3704 ms）。不把这些数字包装成「优化生效」。

## 中途一次失败的尝试（完整记录，不藏）

我先给业务库加了 `PRAGMA journal_mode=WAL` + `busy_timeout=30s`，想改善
并发写。第一次改后压测看起来「P95 从 3677 降到 1521 ms」，但
**availability 只有 0.875**：每轮恰好 3–4 个 `500`。P95 变好纯粹是失败请求
快速返回把分布拉低了 —— 这是最容易骗自己的一类数字。

抓 traceback 得到：

```
sqlite3.OperationalError: attempt to write a readonly database
  at core.create_experiment
```

最小复现（`core.Store(path)` 后 24 线程并发 `create_experiment`）：
WAL 下 `21/24` 成功、`3/24` readonly；目录里没有 `-wal/-shm` 文件。
保持一条常开连接后变成 `23/24`，仍剩 1 个。根因是**本架构每个方法都开关
一条独立连接**，最后一个连接关闭时会 checkpoint 并删除 `-wal/-shm`，
与并发打开的连接竞争。要根治得做真正的连接池（EG-010 未完成）。

**决策：按验收证据撤回 WAL**，只保留无害的 `busy_timeout=30s` 与连接次数
可观测（`Store.conn_opened`，`/api/health` 返回）。撤回后 24/24 成功，
于是重跑上面那组对比。

还有一个坑：`journal_mode` 是**数据库文件头里的持久设置**，撤掉代码不会
自动还原已经跑过的库。仓库里那个本地 `data/evoguard.db` 曾被切成 WAL，
需要显式 `PRAGMA journal_mode=DELETE` 才复原。这也是为什么压测脚本改成
每次新建隔离库 —— 否则历史库的状态会混进结果，两边根本不可比。

## 真正的改善在另一条路径上

EG-009 的持久作业把「提交」与「执行」拆开。`scripts/jobs_smoke.py` 实测
8 并发：`POST /api/jobs` 提交 P95 **14.1 ms**，作业完成 P95 **1785 ms**。
请求线程不再被 3.5 秒卡住 —— 但这个对比是**不同端点**，不是同一工作负载
的严格 A/B，只说明交互形态变了，不声称吞吐翻倍。

## 状态

EG-010 仍是 🟡：热路径零 DDL、索引、连接次数可观测已完成；**连接池、
锁等待剖析、磁盘写入量、断电/重启持久性验证**未做。WAL 尝试失败并撤回，
不写进「已优化」。

## 附：审计库遇到同一类问题（EG-008）

`AuditStore` 原本也开了 `PRAGMA journal_mode=WAL`。做跨进程并发验收时，
完整 gate 里偶发 `attempt to write a readonly database`；单独量
`tests/test_audit_multiprocess.py` 10 次有 **3 次失败**。

撤回 WAL、只留 `BEGIN IMMEDIATE` + `busy_timeout=30s` 后 10 次 **0 失败**。
但放进完整 gate 仍偶发一次（测试耗时从 2.5 s 涨到 13.6 s，资源竞争改变了
时序）。进一步定位到第二个竞态：**两个子进程同时创建一个尚不存在的
数据库文件**（Windows 文件共享）。让主进程先建空库文件（不建表，迁移仍由
两个进程并发执行）后，单测 12 次 0 失败、完整 gate 连跑 3 次 19/19。

这两处都是「按验收证据决定默认值」：EG-008 要的是跨进程事务边界，
由 `BEGIN IMMEDIATE` 提供，不需要 WAL；真实部署里数据库也是先建好、
多个实例同时启动，不存在「同时创建」。`scorecard` 的审计探针随之改成
验 `BEGIN IMMEDIATE` + busy_timeout，并明确断言**没有**启用会引发
readonly 的 WAL —— 不再给一个会出故障的配置发满分。
