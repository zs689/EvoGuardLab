#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：`BEGIN IMMEDIATE` 能否压住 INSERT 阶段的 `database is locked`。

    python scripts/begin_immediate_probe.py --seconds 60 --workers 24

不碰产品代码，直接用 raw sqlite3 对比两种事务模式。
"""
import argparse
import collections
import io
import json
import os
import sqlite3
import tempfile
import threading
import time


def run(mode, seconds, workers, root):
    path = os.path.join(root, mode + ".db")
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE IF NOT EXISTS t(id TEXT PRIMARY KEY, v TEXT)")
    seed.commit()
    seed.close()

    errs = collections.Counter()
    written = [0]
    lock = threading.Lock()
    stop = threading.Event()

    def worker(k):
        c = sqlite3.connect(path, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
        i = 0
        while not stop.is_set():
            try:
                if mode == "begin_immediate":
                    c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT INTO t VALUES(?,?)", ("%d_%d" % (k, i), "x"))
                c.commit()
                with lock:
                    written[0] += 1
            except sqlite3.OperationalError as exc:
                try:
                    c.rollback()
                except Exception:
                    pass
                with lock:
                    errs[type(exc).__name__ + ": " + str(exc)[:60]] += 1
            i += 1
        c.close()

    ts = [threading.Thread(target=worker, args=(k,), daemon=True)
          for k in range(workers)]
    for t in ts:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in ts:
        t.join(timeout=10)
    return {"mode": mode, "seconds": seconds, "workers": workers,
            "writes": written[0], "errors": sum(errs.values()),
            "detail": dict(errs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="evo-beginprobe-")
    rows = [run(m, a.seconds, a.workers, root)
            for m in ("implicit", "begin_immediate")]
    for r in rows:
        print("%-17s %6d 次写, %3d 次错误 %s"
              % (r["mode"], r["writes"], r["errors"], r["detail"]))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "begin_immediate_probe.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump({"config": vars(a), "rows": rows}, f,
                      ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
