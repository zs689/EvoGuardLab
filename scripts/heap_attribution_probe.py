#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定位长时 soak 的堆增长：是观测器开销，还是 Store 真泄漏？

    python scripts/heap_attribution_probe.py --seconds 150 --json <path>

两组用相同负载，唯一差别是**是否把采样字典留在内存**：
- sampled 组：复刻 check_soak，每 5 秒 append 一个含 pool 的 dict；
- bare 组：只累加整数计数器，不保存采样数据。

结束时用 tracemalloc.take_snapshot().compare_to() 按行给出
「这段时间哪一行多分配了内存」—— 这是决定性证据，不靠量级猜测。
"""
import argparse
import io
import json
import os
import sys
import threading
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                # noqa: E402


def run(mode, seconds, workers, root):
    os.environ["AGENTLAB_STORE_POOL"] = "1"
    d = os.path.join(root, mode)
    os.makedirs(d, exist_ok=True)
    s = core.Store(os.path.join(d, "a.db"))
    stop = threading.Event()
    errs, written = [], [0]
    samples = []                 # 只有 sampled 组会累积
    heaps, ticks = [], []
    lock = threading.Lock()

    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    snap0 = tracemalloc.take_snapshot()

    def worker(k):
        i = 0
        while not stop.is_set():
            try:
                s.create_experiment("%s_%d_%d" % (mode, k, i), "order",
                                    "A1001", "none", 0, 7, "D")
                with lock:
                    written[0] += 1
                i += 1
            except Exception as exc:        # noqa: BLE001
                errs.append(repr(exc))
                stop.set()

    ts = [threading.Thread(target=worker, args=(k,), daemon=True)
          for k in range(workers)]
    for t in ts:
        t.start()
    t0 = time.monotonic()
    n = max(1, seconds // 5)
    for _ in range(n):
        time.sleep(5)
        cur = tracemalloc.get_traced_memory()[0]
        heap_mb = round((cur - base) / 1048576.0, 3)
        heaps.append(heap_mb)
        ticks.append(round(time.monotonic() - t0, 1))
        if mode == "sampled":
            # 与 check_soak 完全一致的采样结构
            samples.append({"t_s": ticks[-1], "rows": written[0],
                            "pool": s.pool_stats(), "heap_mb": heap_mb,
                            "db_bytes": 0})
    stop.set()
    for t in ts:
        t.join(timeout=10)
    snap1 = tracemalloc.take_snapshot()
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    s.close()

    diff = snap1.compare_to(snap0, "lineno")
    top = []
    for stat in diff[:12]:
        fr = stat.traceback[0]
        top.append({"file": os.path.basename(fr.filename),
                    "line": fr.lineno,
                    "size_diff_kb": round(stat.size_diff / 1024.0, 1),
                    "count_diff": stat.count_diff})

    own = sum(x["size_diff_kb"] for x in top
              if x["file"] == os.path.basename(__file__))
    return {
        "mode": mode, "seconds": seconds, "workers": workers,
        "writes": written[0], "errors": len(errs),
        "samples_kept": len(samples),
        "heap_first_mb": heaps[0] if heaps else None,
        "heap_last_mb": heaps[-1] if heaps else None,
        "heap_growth_mb": round(heaps[-1] - heaps[0], 3) if heaps else None,
        "heap_peak_mb": round(peak / 1048576.0, 3),
        "top_allocations": top,
        "probe_own_growth_kb": round(own, 1),
        "heap_series": heaps,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=150)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", default="")
    import tempfile
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="evo-heapattr-")
    rows = [run(m, a.seconds, a.workers, root) for m in ("sampled", "bare")]
    for r in rows:
        print("%-8s 采样点=%3d 堆 %.3f -> %.3f MiB (增长 %+.3f) 探针自身增长 %.1f KiB"
              % (r["mode"], r["samples_kept"], r["heap_first_mb"],
                 r["heap_last_mb"], r["heap_growth_mb"],
                 r["probe_own_growth_kb"]))
        print("  top 分配行:", [(x["file"], x["line"],
                              x["size_diff_kb"]) for x in
                             r["top_allocations"][:4]])
    report = {"version": 1, "config": vars(a), "rows": rows,
              "method": "对照 + tracemalloc 按行 compare_to；不靠量级猜测"}
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
