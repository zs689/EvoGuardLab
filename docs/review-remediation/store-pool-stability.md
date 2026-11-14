# 连接池默认开启前的稳定性验收（2026-11-14）

这份是对 `store-pool-ab.md` 里「默认开启前五条没验」的补验，
**也更正了那份报告初版的推荐**：我原本推荐「连接池 + WAL」，错了。

## 一、结论先行

| 配置 | 稳定性 | 结论 |
|---|---|---|
| **B：只开连接池** | `probes/store_pool_stability_poolonly.json` **3/3 全过** | ✅ 推荐（仍需点头才改默认） |
| **C：连接池 + WAL** | `probes/store_pool_stability_wal.json` **4/5**，`wal-concurrency` 判红 | ❌ 不要用于生产 |

**WAL 在这套连接语义下不可靠**：单进程 24 线程并发写，8 轮里 1 轮报
`attempt to write a readonly database`；开池+WAL 跑 935 项全量回归也会
偶发挂 1 项。`store-pool-ab.md` 初版 6 次压测「全程无 5xx」是运气，
只能说明顺利时能跑多快，**不能说明可靠**。

## 二、五条逐项结果

| # | 项 | 怎么做 | 结果 |
|---|---|---|---|
| 1 | 连接数 / 内存有界 | 8 线程共 2048 次写，量 `pool_stats()` 与 `tracemalloc` 峰值 | `created=8`、`discarded=0`、堆峰值增量 0.15 MiB。不随操作次数线性增长 |
| 2 | WAL 备份 / 恢复 | 写 200 条不 checkpoint，分别复制 `db+wal` 与只复制 `db` 后重开 | 连 `-wal`：**200/200**；只复制 db：**0/200**。硬证据：备份必须连 WAL 文件 |
| 3 | 多进程 | 10 轮 × 2 个独立进程各写 100 条，量失败率 | 加整体初始化锁 + 常开连接后 **10/10 轮全 200/200**（此前冷启动配置约 10% 报 readonly） |
| 4 | 强杀恢复 | 子进程 `os._exit` 不 `close()`，主进程重开验数据 | 50/50 行恢复，`PRAGMA integrity_check=ok` |
| 5 | 开池全量回归 | `AGENTLAB_STORE_POOL=1` 跑 `unittest discover` 两次 | 两次均 **935 OK、0 ResourceWarning**；`POOL+WAL` 会偶发 1 项失败 |

命令：

```bash
python scripts/store_pool_stability.py --no-wal \
  --name store_pool_stability_poolonly --out docs/review-remediation/probes
python scripts/store_pool_stability.py \
  --name store_pool_stability_wal --out docs/review-remediation/probes
AGENTLAB_STORE_POOL=1 python -m unittest discover -s tests -q
```

## 三、补验过程中发现并修掉的两个真 bug

1. **`_ConnPool.__del__` 太激进**：初版调用完整 `close()`，会把 `_all` 里
   **正在被线程借用**的连接也关掉。GC 时机不可控，多线程下正好击穿 WAL
   的 `-shm`，复现 `readonly`。改成只关空闲队列里的连接，借出的等归还时
   由 `put()` 因 `_closed` 关掉。修后开池全量回归的 14 条
   `ResourceWarning` 归零，且不再击穿。
2. **多进程同时初始化 WAL**：两个进程同时执行 `init()` 的 DDL 与
   `PRAGMA journal_mode=WAL` 会互相踩（2 进程各写 100 条只落 100 条）。
   加了 `_interprocess_lock()`（`O_CREAT|O_EXCL` 原子锁，30s 陈旧锁自清）
   包住**整个初始化**，并且只在库还不是 WAL 时才切换模式。

一个测试上的诚实处理：`tests/test_store_pool.py` 原来那条
`test_wal_is_stable_with_persistent_connections` 是 flaky 源（约 1/8 失败），
放在套件里会让全量回归变成「假绿」。**没有把它删掉或标 expectedFailure**，
而是改成明确只测「只开池 24 并发 0 错误」，WAL 的失败率移到诊断脚本里
`wal-concurrency` 项**故意判红**，数字留在 JSON 里。

## 四、仍未验的（不假装做完）

- 小时级 soak test（这里最长是 2048 次操作 / 8 线程，几十秒）。
- 多主机共享存储；池是进程内的，多副本各自有池。
- 真正的 `taskkill /F`（用 `os._exit` 模拟，同样不刷 WAL，但不等价于
  Windows 强杀的句柄清理）。
- WAL 恢复演练没做（而且结论已是「不要开 WAL」）。
- 默认开启后的 929 项回归：现在只在「显式开池」下跑过全量，默认路径没变。

## 五、推荐

**只开连接池，不开 WAL，默认仍关闭**：

```bash
AGENTLAB_STORE_POOL=1
AGENTLAB_STORE_POOL_SIZE=8
# 不要设 AGENTLAB_STORE_WAL
```

B 组同负载写路由 2103 ms / 7.4–8.0 rps（默认 2969–3627 ms / 5.1–5.4 rps），
没有 WAL 那些 readonly 风险。改默认值是语义级变更，按 USER 规矩不替
张硕决定。台账 EG-010 保持 🟡。
