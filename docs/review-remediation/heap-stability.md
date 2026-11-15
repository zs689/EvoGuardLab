# 堆增长归因与稳定性保证（2026-11-15）

## 结论

60 分钟 soak 里堆从 0.153 → 0.482 MiB（+0.329 MiB）**不是 Store 泄漏**，
而是**观测器自己把 720 个采样点留在 `tracemalloc` 追踪的堆里**。
已定位、已修、已加硬判据；修后短跑堆不再随时间累积。

此前文档写的「既不证明也不排除泄漏」**已作废**——那是证据不足时的保守表述，
现有三条独立证据可以下确定结论。

## 一、三条证据

### 1. 对照实验（`probes/heap_attribution.json`）

`scripts/heap_attribution_probe.py`，两组相同负载（8 线程 × 150 秒），
唯一差别是**是否把采样字典留在内存**：

| 组 | 留存的采样点 | 堆首→尾 | 增长 |
|---|---|---|---|
| sampled | 30 | 0.137 → 0.155 MiB | **+0.018** |
| bare | 0 | 0.139 → 0.129 MiB | **-0.010** |

`tracemalloc.take_snapshot().compare_to()` 按行给出的最大分配是
`core.py:242`（`_WriteGuardConn` 里设置 `_held` 附近，连接代理对象的
固定开销，约 100 KiB）——**两组都有、量级相当、不随时间累积**。
不采样组堆是负增长，说明 Store 侧没有持续分配。

### 2. 720 个采样点的内存占用（直接实测）

| 采样点数 | 占用 |
|---|---|
| 30 | 0.011 MiB |
| 180 | 0.057 MiB |
| 360 | 0.124 MiB |
| **720** | **0.285 MiB** |

60 分钟 soak 恰好 720 个采样点，实测堆涨 **0.329 MiB**；
0.285 / 0.329 ≈ **87%**，剩余约 0.044 MiB 是 GC 波动与临时对象。
线性关系清楚，量级对得上。

### 3. 修后短跑（60 秒）

同样的 8 线程负载，堆 **0.145 → 0.065 MiB（-0.080，回落）**，
不再随采样点累积。

## 二、根因

原 `check_soak` 在 `tracemalloc.start()` **之后**执行：

```python
samples.append({"t_s": ..., "rows": ..., "pool": s.pool_stats(),
                "heap_mb": ..., "db_bytes": ...})
```

每个采样点是一个 dict 加一个 `pool` 子 dict，全程被 `samples` 列表强引用，
既不会被 GC 回收、又落在 tracemalloc 追踪范围内。于是「采样点越多 →
堆越大」，看起来像泄漏。30 分钟时只有 +0.055 MiB 不明显，60 分钟就
显出来了 —— 这也是为什么短跑一直没暴露。

## 三、修复（观测器与被测对象解耦）

`scripts/store_remaining_checks.py::check_soak`：

- 每 5 秒把采样点 `json.dumps` 后写入临时 JSONL 并 `flush`；
  临时字符串引用计数归零即释放，**不驻留堆**；
- 内存只保留 `heaps`（float 列表，720 个约 6 KB）；
- `tracemalloc.stop()` **之后**才读回 JSONL 组装 `samples` 写入产物，
  **产物格式不变**（仍有 `samples` 数组）；
- 临时 JSONL 用完删除，回归测试守着。

## 四、稳定性怎么「保证」（不是一句结论，是三层约束）

1. **运行期硬判据**：`check_soak` 计算
   `heap_growth_mb = heaps[-1] - heaps[0]`，要求 `< 0.15 MiB`，
   并把 `heap_stable` / `observer_excluded` / 首尾值写进 evidence；
   不满足就判 FAIL（不是只告警）。
2. **回归测试**：`tests/test_soak_heap.py` 短跑 10 秒，断言
   观测器已排除、堆稳定、采样点格式兼容、临时文件已清理。
3. **证据门槛**：`review_checks.py` 的 `EG-010/heap-stability`
   读长跑产物，要求 `heap_stable=true`、`observer_excluded=true`、
   增长 < 0.15 MiB、采样点 ≥ 100，否则 gate 判红。

## 五、修后长跑（`probes/soak_heap_stable.json`，30 分钟，4/4）

1800 秒 / 8 线程共 **436576 次写、0 错误**，`created` 恒 8、
`discarded` 恒 0，360 个采样点。堆 **0.137 → 0.053 MiB（增长 -0.084）**，
全程在 0.044–0.185 MiB 之间波动、无上升趋势，`heap_stable=true`、
`observer_excluded=true`。

对照修前同长度（30 分钟）的 +0.055 MiB，以及修前 60 分钟的
+0.329 MiB —— 观测器污染消除后，堆不再随时间累积。

`EG-010/heap-stability` 门槛已做可证伪验证：把产物里的
`heap_growth_mb` 改成 1.25 即判 FAIL，还原后 PASS。

### 三层约束的实测结果

| 层 | 结果 |
|---|---|
| 运行期硬判据 | `heap_growth_mb=-0.084 < 0.15` → `heap_stable=true` |
| 回归测试 | `tests/test_soak_heap.py` 1/1 |
| 证据门槛 | `EG-010/heap-stability` PASS，gate 27 → 28 |

全量回归 965 tests OK / 28-28 gate。

## 六、仍然诚实的边界

- 这证明的是**「Python 堆里没有随请求线性增长的对象」**；
  不覆盖 SQLite 页缓存、OS 文件缓存、C 扩展自己 malloc 的内存
  （tracemalloc 看不见）。
- `_WriteGate` 的弱引用表按 Store 生命周期回收；
  若服务长期持有大量不同路径的 Store，`active_write_gates` 可从
  `/api/health` 观测，但没有做「上万租户路径」的压力验证。
- 结论来自本机 Windows + CPython 3.13；换运行时需重测。
