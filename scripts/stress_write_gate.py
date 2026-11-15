#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""写闸门并发压测：计数与排队在高并发下不能漂。

    python scripts/stress_write_gate.py --threads 200 --json <path>
"""
import argparse
import io
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                # noqa: E402


def run(threads, hold_ms):
    gate = core._WriteGate()
    errors = []
    peak_waiters = [0]
    probe_stop = threading.Event()

    def probe():
        while not probe_stop.is_set():
            with gate._counter:
                peak_waiters[0] = max(peak_waiters[0], gate.waiters)
            time.sleep(0.001)

    pt = threading.Thread(target=probe, daemon=True)
    pt.start()

    def worker(k):
        try:
            gate.acquire()
            try:
                time.sleep(hold_ms / 1000.0)
            finally:
                gate.release()
        except Exception as exc:            # noqa: BLE001
            errors.append("%d: %r" % (k, exc))

    ts = [threading.Thread(target=worker, args=(k,))
          for k in range(threads)]
    t0 = time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.monotonic() - t0
    probe_stop.set()
    pt.join(timeout=1)
    st = gate.stats()
    ok = (not errors and st["acquired"] == threads
          and st["waiters"] == 0 and not st["locked"]
          and peak_waiters[0] > 0)
    return {
        "threads": threads, "hold_ms": hold_ms,
        "elapsed_s": round(elapsed, 3),
        "acquired": st["acquired"], "waiters": st["waiters"],
        "locked": st["locked"], "peak_waiters": peak_waiters[0],
        "errors": errors[:5], "passed": ok,
        "note": "peak_waiters>0 证明确实发生排队，不是全部顺序执行",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=200)
    ap.add_argument("--hold-ms", type=float, default=0.2)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    rows = [run(a.threads, a.hold_ms) for _ in range(3)]
    ok = all(r["passed"] for r in rows)
    report = {"version": 1, "passed": ok, "repeats": 3, "rows": rows}
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
            f.write("\n")
    for i, r in enumerate(rows, 1):
        print("  run%d acquired=%d waiters=%d peak_waiters=%d %.2fs %s"
              % (i, r["acquired"], r["waiters"], r["peak_waiters"],
                 r["elapsed_s"], "OK" if r["passed"] else "FAIL"))
    print("== %s ==" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
