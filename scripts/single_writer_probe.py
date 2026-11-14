#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「单写者锁」能否根治 `database is locked`，以及吞吐代价。

    python scripts/single_writer_probe.py --seconds 60 --workers 24

前几轮试过 busy_timeout / COMMIT 重试 / BEGIN IMMEDIATE 都压不住。
这个实验换思路：写之前加一把进程级锁，读写分离（读不加锁）。
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

    write_lock = threading.Lock() if mode == "serialized" else None
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
                if write_lock:
                    write_lock.acquire()
                try:
                    c.execute("INSERT INTO t VALUES(?,?)",
                              ("%d_%d" % (k, i), "x"))
                    c.commit()
                finally:
                    if write_lock:
                        write_lock.release()
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
    t0 = time.monotonic()
    for t in ts:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in ts:
        t.join(timeout=10)
    elapsed = time.monotonic() - t0
    return {"mode": mode, "seconds": round(elapsed, 1), "workers": workers,
            "writes": written[0], "rps": round(written[0] / elapsed, 1),
            "errors": sum(errs.values()), "detail": dict(errs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="evo-singlewriter-")
    rows = [run(m, a.seconds, a.workers, root)
            for m in ("baseline", "serialized")]
    for r in rows:
        print("%-12s %6d 次写 (%6.1f rps), %3d 次错误 %s"
              % (r["mode"], r["writes"], r["rps"], r["errors"],
                 r["detail"]))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "single_writer_probe.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump({"config": vars(a), "rows": rows}, f,
                      ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
