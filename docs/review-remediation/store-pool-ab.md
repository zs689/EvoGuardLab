# EG-010 连接池 + WAL：同负载 A/B（2026-11-14）

> ## ⚠️ 更正（同日后续稳定性验证）
>
> **我在这份报告初版里推荐「连接池 + WAL」（C 组），这个推荐是错的。**
> 补做稳定性验证后发现：WAL 在这套连接语义下**单进程 24 线程并发写
> 就有约 1/8 概率报 `attempt to write a readonly database`**
> （`probes/store_pool_stability_wal.json` 的 `wal-concurrency` 项判红）。
> 初版 6 次压测「全程无 5xx」是运气，不能当稳定性证据。
>
> **修正后的推荐：只开连接池（B 组，2103 ms / 7.4–8.0 rps），不要开 WAL。**
> B 组稳定性 `probes/store_pool_stability_poolonly.json` 3/3 全过；
> 开池+WAL 下 935 项全量回归还会偶发挂 1 项，只开池两次全绿。
> 详见 `docs/review-remediation/store-pool-stability.md`。
>
> 下面 §结果 的 A/B/C 数字**保留原样不删**（那些运行确实测到了），
> 但 C 组只能说明「顺利时能跑多快」，**不能说明它可靠**。
>
> **再更正（同日第八轮）**：默认配置后来又变了 —— 用 SQL 感知的写串行化
> 解决了并发 `database is locked`，并按 A/B 把「连接池 + 串行化」
> **改为默认开启**（旧默认 19073 写/5 错 → 新默认 32287 写/0 错）。
> 见 `single-writer-innovation.md`。WAL 仍不推荐。

上轮我把 WAL 撤回了，理由是「24 并发 3/24 `readonly`」，并写下
「修好它需要真正的连接池（EG-010 未完成）」。这一轮把连接池做了，
再用**同一份脚本、同一参数**验证这个判断对不对。

## 方法

| 项 | 值 |
|---|---|
| 脚本 | `scripts/loadtest.py`（同一份，只加两个开关） |
| 参数 | `--requests 24 --workers 8 --rounds 3`，四次场景一致 |
| 数据 | 每次运行新建隔离临时库（`loadtest.py` 已改成这样） |
| 环境 | Python 3.13.14 / SQLite 3.53.1 / 同机 Windows / 回环 HTTP / mock 模型 |
| 配置 | A=默认（每操作开关连接、delete 日志）；B=`AGENTLAB_STORE_POOL=1`；C=B + `AGENTLAB_STORE_WAL=1` |
| 重复 | A×2、B×1、C×3，共 6 次独立运行 |

机器可读：`perf/pool_ab_{A,A2,B,C,C2,C3}.json`。

## 结果（`write_run` = POST /api/run，8 并发）

| 运行 | 配置 | 三轮 P95 (ms) | 中位 P95 | 吞吐 rps | availability |
|---|---|---|---|---|---|
| A | 默认 | 2341 / 2969 / 3411 | **2969** | 5.1–5.4 | 1.0 |
| A2 | 默认（重复） | — | **3627** | — | 1.0 |
| B | 连接池 | 1948 / 2277 / 2103 | **2103** | 7.4–8.0 | 1.0 |
| C | 连接池 + WAL | 678 / 490 / 565 | **565** | 25–31 | 1.0 |
| C2 | 同上（重复） | — | **693** | — | 1.0 |
| C3 | 同上（重复） | — | **791** | — | 1.0 |

读路径（read_health / read_runs / read_metrics）三组都在 20–50 ms，
差异在噪声内，不作为结论。

**每一轮的状态计数都是 24×200，没有 500 / 连接失败。** 这和上轮
「P95 变好但 availability 0.875」的假象不同 —— 这次是吞吐同步涨了
5 倍，不是请求快速失败把分布拉低。

## 为什么这次 WAL 没有 `readonly`

`tests/test_store_pool.py::test_wal_is_stable_with_persistent_connections`
专门验了这件事：连接池让连接**常开**，最后一个连接关闭时不再 checkpoint
并删除 `-wal/-shm`，并发打开的连接就不会拿到失效的共享文件。
24 并发写 0 失败。上轮撤回的判断是对的，缺的确实是连接池。

池的另一个收益是消除「一次实验十几步 = 十几次 connect/close」。
B 组（只开池、不开 WAL）就已经从 2969 降到 2103 ms。

## 端到端（开池 + WAL）

```
enterprise_smoke.py   9/9
jobs_smoke.py        11/11   提交 P95 7.9 ms / 完成 P95 323.8 ms
                             （上轮同脚本：14.1 / 1785 ms）
drill.py              5/5    产物在 perf/drill_pool.json
python -m unittest discover -s tests -q   Ran 935 ... OK
```

## 决定（已更正两次）：**连接池 + 写串行化默认开，不要开 WAL**

> 本段初版写「默认仍然关闭」，第八轮按 A/B 证据把两个默认值改开了，
> 详见 `single-writer-innovation.md` §四。下面是当时的推荐演变，保留不删。

当时按 USER.md「改代码最小改动，建议可以给但要点头才改」，默认关闭，
推荐配置从 C 改成 B：

```bash
AGENTLAB_STORE_POOL=1        # 推荐：有界连接池，默认 8 条
AGENTLAB_STORE_POOL_SIZE=8
# AGENTLAB_STORE_WAL=1       # 不要开：24 并发约 1/8 概率 readonly
```

`--store-wal` 不带 `--store-pool` 时 `loadtest.py` 会直接报错拒绝。
C 组那 3 次「565/693/791 ms」只能说明顺利时能跑多快，不能说明可靠。

### 五条「改成默认前该验的」——已补验，结果见 `store-pool-stability.md`

| # | 项 | 结果 |
|---|---|---|
| 1 | 长时/有界 | ✅ 2048 次并发写，`created=8`、堆增量 0.15 MiB、0 丢弃 |
| 2 | WAL 备份/恢复 | ✅ 连 `-wal` 一起复制恢复 200/200；**只复制 db 恢复 0/200** |
| 3 | 多进程 | ✅ 加整体 init 锁 + 常开连接后，10 轮 × 2 进程全 200/200 |
| 4 | 强杀恢复 | ✅ `os._exit` 不 close 后重开 50/50 行，integrity ok |
| 5 | 开池全量回归 | ✅ 只开池两次 935 OK、0 泄漏；**开 WAL 会偶发挂 1 项** |

补验过程还修了两个真问题：池的 `__del__` 只关空闲队列（否则多线程下
击穿正在借用的连接）、`__init__` 用跨进程原子锁包住整个初始化。
`tests/test_store_pool.py` 的 WAL 断言改成「只开池稳定」，
WAL 的 flaky 移到诊断脚本里**如实判红**，不再让全量回归假绿。

## 状态

EG-010 仍是 🟡：连接池、根因、A/B 与五条稳定性证据都补齐了，
推荐配置明确为「只开池」。但**默认值没改**（语义级变更需要点头），
WAL 明确不可用。不把「可开启」写成「已默认生效」。
