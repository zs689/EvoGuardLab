# 已知问题：并发写偶发 `database is locked`（2026-11-14）

> ## ✅ 后续更正（同日）：**已解决**
>
> 本文初版判定「未解决、根治需架构级决定」。后来换了思路：用一层
> **SQL 感知的连接代理**把写串行化、读保持并发，零调用点改动、零依赖，
> 产品代码 A/B 显示错误归零且吞吐 +69%。
> **方案与证据见 `single-writer-innovation.md`**；两个默认值（连接池、
> 写串行化）已按证据改为默认开。
>
> 下面保留完整的失败与无效尝试过程，不删、不改写成「一开始就想到」。

**原始状态：未解决。** 本文记录从 10 分钟 soak 里挖出问题的全过程，
两次修复尝试（`COMMIT` 重试、`BEGIN IMMEDIATE`）都无效并已撤回。

## 一、怎么发现的

`store_remaining_checks.py --soak-seconds 600`（10 分钟 / 8 线程）跑出
**98314 次写、1 次 `OperationalError: database is locked`**，soak 判 3/4。
原始产物 `probes/store_soak600.json`（如实保留判红，没改成绿）。

## 二、是不是连接池引入的？不是

`lock_contention_probe.py` 让两种连接语义跑**完全相同**的负载
（240 秒 / 16 线程），产物 `probes/lock_contention_before.json`：

| 模式 | 写次数 | locked 次数 | 比率 |
|---|---|---|---|
| 默认（每操作开关连接） | 41558 | 9 | 0.217‰ |
| 只开连接池 | 67697 | 26 | 0.384‰ |

**默认路径本来就有这个错**，不是第四轮连接池带来的。池模式写入量更大，
绝对次数多是自然的；比率同量级。这个结论很重要：它不改变
「只开池」的推荐，也不该拿来当「连接池有 bug」的证据。

## 三、错误到底在哪一行

给诊断脚本加了 traceback 记录（`probes/lock_contention_traces.json`）：

```
File "<repo>/ai-service/evoguard/core.py", line 454, in create_experiment
    c.execute("INSERT INTO experiments(...) VALUES(...)", ...)
sqlite3.OperationalError: database is locked
```

**在 `INSERT` 的 `execute()` 阶段，不是 COMMIT。** SQLite 在隐式 BEGIN
拿不到 RESERVED 锁时，为避免死锁会绕过 busy handler 直接返回
SQLITE_BUSY —— 所以 `PRAGMA busy_timeout=30000` 挡不住这一类。

## 四、两次无效尝试（都已撤回，不留半成品）

| 尝试 | 做法 | 结果 | 处置 |
|---|---|---|---|
| COMMIT 阶段重试 | 加 `_commit_with_retry()`（事务已执行完、只重试 COMMIT）、`commit_retries` 计数器、`/api/health` 字段、5 项单测 | 180 秒 / 16 线程对照：default 5/28653、pool 23/50000，错误率与修复前同量级（`probes/lock_contention_retry_attempt.json`） | **全部撤回**。打错了位置，留着就是「文档写了但代码没用」 |
| `BEGIN IMMEDIATE` | raw sqlite 对照，显式在事务开始就取 RESERVED 锁（60 秒 / 24 线程，`probes/begin_immediate_probe.json`） | implicit 21/10905（0.193%）→ begin_immediate 18/20587（0.087%），**减半但没归零** | **未接入产品代码**。改事务模型影响所有读写方法，收益不匹配风险 |

## 五、影响与边界

- 表现是**低概率 500**，不是数据损坏：`conn()` 异常路径会 rollback，
  失败的事务没有部分落盘。`integrity_check` 在所有强杀/恢复实验里都是 ok。
- 触发条件：本机 Windows、单进程、多连接并发写、**delete 日志**。
  8 线程 10 分钟 1 次；16~24 线程时升到 0.1–0.4‰。
- 与 WAL 无关：WAL 已因 `readonly` 判定不可用，不能拿它当解法。
- 不影响「只开连接池，不要开 WAL，默认仍关闭」的推荐 —— 默认路径同样有，
  池没有让它变差。

## 六、当时认为要根治得做什么（后来被新方案绕过）

> 下面三条是初版列出的「架构级」路径。**实际采用的方案不在这三条里**：
> 用 SQL 感知代理在应用层串行化写，既没换库、没全局串行化读，
> 也不用逐方法重放。见 `single-writer-innovation.md`。

1. **按方法粒度做事务重放**：`contextlib.contextmanager` 不能重放调用方的
   `with` 体，只能逐个写方法包重试；多语句事务还要保证幂等，改动面大。
2. **串行化所有写**：一个进程级写锁能压住，但会吃掉并发吞吐，
   与 EG-010 的性能目标冲突。
3. **换数据库**：Postgres 的行锁语义不同；项目 SQL 保持 PG 兼容写法，
   但引入真实依赖违背「零安装」约束。

当时按 USER「最小改动」规矩没有擅自做。后续换思路解决了，
**当前状态：已解决（默认开启），见顶部更正块。**

## 七、复现

```bash
python scripts/lock_contention_probe.py --seconds 240 --workers 16 --out <dir>
python scripts/begin_immediate_probe.py --seconds 60 --workers 24 --out <dir>
python scripts/store_remaining_checks.py --soak-seconds 600 --out <dir>
```
