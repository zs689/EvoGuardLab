# EG-010 连接池 + WAL：同负载 A/B（2026-11-14）

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

## 决定：**默认仍然关闭**

按 USER.md「改代码最小改动，建议可以给但要点头才改」，也按审查
「生产默认值按验收证据决定」—— 证据支持开，但这是**语义级默认变更**，
我不替你决定。

开启方式（`.env` 或启动环境）：

```bash
AGENTLAB_STORE_POOL=1        # 有界连接池，默认 8 条
AGENTLAB_STORE_POOL_SIZE=8
AGENTLAB_STORE_WAL=1         # 必须和 POOL 一起开；单独开会 readonly
```

`--store-wal` 不带 `--store-pool` 时 `loadtest.py` 会直接报错拒绝，
避免有人只抄一半又踩上轮的坑。

### 改成默认之前还该验的（我没做）

1. **长时稳定性**：这里只跑了短压测，没有小时级内存增长与连接泄漏验证。
2. **备份/恢复**：WAL 模式下 `-wal` 里的未 checkpoint 事务必须一起备份；
   `docs/data-governance.md` 的备份说明还没针对 WAL 更新。
3. **多进程**：池是**进程内**的，多副本各自有池；跨进程 WAL 行为没验。
4. **进程退出**：`Store.close()` 会关池，但服务被 `SIGKILL` 时 WAL 恢复
   是否正常没测。
5. **默认开启后的 929 项回归**：现在 935 项是在池**关闭**下跑的；
   开池跑全量回归没做。

## 状态

EG-010 仍是 🟡：连接池、锁竞争根因、同负载 A/B 证据都补齐了，
但**默认值没改、上面五条没验**。不把「可开启」写成「已默认生效」。
