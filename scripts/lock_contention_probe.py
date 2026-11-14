#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""判断 `database is locked` 是连接池引入的，还是 SQLite 多线程固有行为。

    python scripts/lock_contention_probe.py --seconds 240 --workers 16 --out <dir>

10 分钟 soak 在 98314 次写里见过 1 次 `database is locked`。这个脚本让
「默认每操作开关连接」与「常开连接池」跑完全相同的负载，用错误率对比。
"""
import argparse
import collections
import io
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core          # noqa: E402


def _set_env(mode):
    # 池默认已改为开，"default" 组必须显式关掉才是旧行为对照。
    # 不碰 SERIALIZE_WRITES：让外部 A/B 能控制它。
    os.environ["AGENTLAB_STORE_POOL"] = "1" if mode == "pool" else "0"
    os.environ.pop("AGENTLAB_STORE_WAL", None)
    if mode == "pool":
        os.environ["AGENTLAB_STORE_POOL_SIZE"] = "8"


def run(mode, seconds, workers, root):
    _set_env(mode)
    d = os.path.join(root, mode)
    os.makedirs(d, exist_ok=True)
    s = core.Store(os.path.join(d, "a.db"))
    errs = collections.Counter()
    traces = []
    written = [0]
    lock = threading.Lock()
    stop = threading.Event()

    def worker(k):
        i = 0
        while not stop.is_set():
            try:
                s.create_experiment("p%d_%d" % (k, i), "order", "A1001",
                                    "none", 0, 7, "D")
                with lock:
                    written[0] += 1
            except Exception as exc:        # noqa: BLE001
                import traceback
                with lock:
                    errs[type(exc).__name__ + ": " + str(exc)[:70]] += 1
                    if len(traces) < 3:
                        traces.append("".join(traceback.format_exception(
                            type(exc), exc, exc.__traceback__))[-1400:])
            i += 1

    ts = [threading.Thread(target=worker, args=(k,), daemon=True)
          for k in range(workers)]
    t0 = time.monotonic()
    for t in ts:
        t.start()
    timeline = []
    for _ in range(max(1, seconds // 30)):
        time.sleep(30)
        timeline.append({"t_s": round(time.monotonic() - t0, 1),
                         "writes": written[0],
                         "errors": sum(errs.values()),
                         "pool": s.pool_stats()})
    stop.set()
    for t in ts:
        t.join(timeout=10)
    st = s.pool_stats()
    s.close()
    return {
        "mode": mode, "seconds": seconds, "workers": workers,
        "serialize_writes": os.environ.get(
            "AGENTLAB_STORE_SERIALIZE_WRITES", "1"),
        "writes": written[0], "error_count": sum(errs.values()),
        "errors": dict(errs), "error_traces": traces,
        "timeline": timeline, "final_pool": st,
    }


def main():
    ap = argparse.ArgumentParser(description="锁冲突对照实验")
    ap.add_argument("--seconds", type=int, default=240)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="evo-lockprobe-")
    rows = [run(m, a.seconds, a.workers, root) for m in ("default", "pool")]
    report = {
        "version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config": {"seconds_per_mode": a.seconds, "workers": a.workers},
        "rows": rows,
        "note": "相同负载下比较两种连接语义；错误率极低，短跑可能为 0，"
                "不能据此宣称「无锁冲突」。",
    }
    for r in rows:
        print("%-8s %d 次写, %d 次错误 %s"
              % (r["mode"], r["writes"], r["error_count"],
                 list(r["errors"])[:3]))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "lock_contention_probe.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
