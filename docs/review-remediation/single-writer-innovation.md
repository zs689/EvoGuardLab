# 创新方案：SQL 感知的写串行化（2026-11-14）

上一轮我把 `database is locked` 判成「未解决」，并写了「根治要串行化写、
换库或按方法事务重放，属架构决定」。这轮换了个角度，**用一层 SQL 感知的
连接代理把写串行化落地了，没有改任何调用点，也没有引入依赖**。

## 一、创新点是什么

不再调 SQLite 参数（`busy_timeout` / COMMIT 重试 / `BEGIN IMMEDIATE` 都试过，
无效或只减半），而是在应用层做**读写分离**：

| 组件 | 作用 |
|---|---|
| `_WriteGuardConn` | 代理 `sqlite3.Connection`。`execute()` 时看 SQL 前缀：读语句**完全不加锁**，写语句才拿锁；`commit()` / `rollback()` 释放锁 |
| `_WriteGate` | 按**数据库绝对路径**共享的进程级写锁（SQLite 锁是文件级的，不能挂在 Store 实例上），带 `acquired` / `waiters` 计数 |
| `conn()` | 在 `yield` 前包一层代理；池分支归还**原始连接**（不是代理），避免嵌套包装 |

为什么算创新：
1. **零调用点改动**：`core.Store` 几十个方法不用逐个标 `write=True`，
   在 SQL 边界自动判定，不会漏也不会随重构漂移。
2. **读不加锁**：不是简单粗暴给所有 `conn()` 上锁，读路径保持完全并发。
3. **可证伪、可观测、可关闭**：`/api/health` 的 `write_gate` 给累计写事务数
   与排队线程数；`AGENTLAB_STORE_SERIALIZE_WRITES=0` 可退回旧行为。
4. **按证据改了默认值**：连接池与写串行化一起默认开（下面 §三）。

## 二、可行性实验（先证伪，再写产品代码）

`scripts/single_writer_probe.py`，raw sqlite，24 线程 60 秒：

| 模式 | 写入 | 吞吐 | `database is locked` |
|---|---|---|---|
| baseline | 21279 | 342.0 rps | **18 次** |
| 单写者锁 | 22113 | **368.1 rps** | **0 次** |

反直觉但合理：串行化省掉了锁冲突与回滚重试，吞吐反而 **+7.6%**。

## 三、产品代码上的 A/B

`scripts/lock_contention_probe.py`，90 秒 × 24 线程，
产物 `probes/serialize_off.json` / `serialize_on.json`：

| 配置 | 写次数 | 错误 | 结论 |
|---|---|---|---|
| 无池 + 关串行化（旧默认） | 19073 | 5 | 基线 |
| 开池 + 关串行化 | 30242 | 15 | 吞吐高但错误更多 |
| 无池 + 开串行化 | 7410 | **0** | 错误归零但**吞吐 -61%** |
| **开池 + 开串行化（新默认）** | **32287** | **0** | **错误归零、吞吐比旧默认 +69%** |

**没有隐藏那个 -61%**：无池时每次 `conn()` 都新建连接，写锁把「冷连接 +
commit fsync」也串行了，所以慢。这正是把两个默认值**一起**改开的原因 ——
池让连接常热，串行化才不付吞吐代价。

单线程微基准也做了 A/B/A/B 交叉验证（避免顺序效应）：
关 120 / 开 118 ops/s，**开销约 2%**。第一次单向测量得到的「-47%」是
系统负载噪声，交叉验证推翻后没有写进结论。

## 四、默认值变更（本轮最大改动）

按审查 handoff_rules「生产默认值按验收证据决定」，把两个开关默认改为开：

```python
pool_flag       = os.environ.get("AGENTLAB_STORE_POOL") or "1"
serialize_flag  = os.environ.get("AGENTLAB_STORE_SERIALIZE_WRITES") or "1"
```

- `fast=True`（离线评测，单线程）自动跳过两者，实验数据不受影响。
- WAL 仍默认关，且仍不推荐（见 §六）。
- 关闭方式：`AGENTLAB_STORE_POOL=0` / `AGENTLAB_STORE_SERIALIZE_WRITES=0`。

支撑证据：944 项回归在默认（新）与全关两种配置下都全绿；
池+串行化下 10 轮 × 2 进程全 200/200、真实 `taskkill /F` 恢复 120/120、
60 秒 soak 约 2 万次写 0 错误；wheel 制品冒烟 10/10。

## 五、对既有结论的影响

- **`lock-contention-known-issue.md` 的「未解决」已被本方案解决**，
  那份文档保留两次无效尝试的过程，顶部加了更正块。
- 重跑 `store_pool_stability.py`：推荐配置 3/3；池+WAL 5/5，
  其中 `wal-concurrency` 从 1/8 变成 **0/8**（写串行化顺带压住了 WAL 的
  readonly）。**但样本只有 8 轮，不改 WAL 结论**。
- 重跑 `store_remaining_checks.py`：4/4，60 秒 soak 20657 次写 0 错误。

## 六、WAL 仍然不推荐（三个独立理由里还剩两个）

1. 备份只复制 `.db` 恢复 **0/200 行**，必须连 `-wal` 一起；
2. 磁盘写放大约 **70 倍**（8610 vs 122.9 B/写）；
3. ~~并发 `readonly`~~ 被写串行化压到 0/8，但样本有限，不构成翻案。

## 七、仍未验的

- 小时级 soak（本轮最长 **10 分钟**）：新默认下 `173247` 次写、**0 错误**、
  `created` 恒为 8、堆峰值 0.23 MiB，`probes/store_soak600_final.json` 4/4。
  旧默认同样时长是 98314 次写 / 1 次错。
- 多主机共享存储（NFS/CIFS 上的同一个库文件）没验。
  **单主机多进程已验**：见 §八，进程内锁已够，不需要跨进程应用层锁。
- Docker 镜像层、GitHub CI、真实浏览器、Linux wheel —— 环境限制。
- v3 检测实验、语料上游 commit/license、真实外部审批人 —— 需要上游信息
  与第二位调参者。


## 八、后续补验（同日第九轮）

### 1. 端到端 HTTP 压测（此前所有 A/B 都只在 Store 层）

`loadtest.py` 加 `--store-off` 显式对照；同参数
`--requests 24 --workers 8 --rounds 3`，各跑两次：

| 运行 | 新默认 `write_run` P95 | 全关（旧行为） | 每轮状态 |
|---|---|---|---|
| 1 | **1093.2 ms**（8.0–8.7 rps） | 2815.8 ms（5.2–5.6 rps） | 24×200 |
| 2 | **1067.9 ms**（7.7–8.3 rps） | 3247.4 ms（5.2–5.3 rps） | 24×200 |

真实请求路径约 **2.7–3 倍**改善，两次重复稳定、无 5xx。
产物 `probes/http_ab_{on,on2,off,off2}.json`；
`EG-010/e2e` 门槛守着这个收益（gate 22 → 23）。

### 2. 修掉自己方案里的一个真问题：`_GATES` 无上限

审查新代码时发现 `_GATES` 是普通 dict，临时库路径会无限累积
（944 项测试会创建大量临时 Store；长时服务按租户/作业动态建库同理）。

**没有用 LRU 淘汰**：淘汰一把**正在被持有**的锁，会让后来的 Store
给同一路径建一把新锁，两把锁并存、串行化直接失效。

改用 `weakref.WeakValueDictionary` + `Store` 持强引用：
- 只要有任一 Store 活着，同路径所有 Store 拿到**同一把**锁；
- 最后一个 Store 被 GC 时 gate 自动消失；
- `/api/health` 加 `active_write_gates` 可观测。

`tests/test_single_writer.py` 加了 2 项（共享性、回收性），共 11 项。
实测：20 个 Store → 20 个 gate → 全部回收后 **0**。

### 3. 用数据修正上一轮的「进程内锁」缺点

上一轮把「`_WriteGate` 是进程内锁，多副本各锁各的」列为缺点，
但没量过。`scripts/cross_process_write_probe.py`：
**4 进程 × 8 线程 × 45 秒**，同一库文件。

| 配置 | 写入 | `database is locked` |
|---|---|---|
| 新默认（池 + 进程内串行化） | **14691** | **0** |
| 全关 | 9564 | 8 |

进程内串行化减少了每个进程内部的连接竞争，间接把跨进程文件锁的
争用窗口也压下去了；SQLite 的 busy handler 在**跨进程**场景本就生效
（同进程锁升级才是例外）。结论：**单主机多进程不需要跨进程应用层锁**，
那条缺点在本机规模下不成立。产物
`probes/cross_process_write_probe.json`。

仍未验的是**多主机共享存储**（NFS/CIFS 上的同一库文件），那需要
另一套机器，不能靠本机多进程代替。
