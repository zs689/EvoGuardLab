#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨进程并发写：`_WriteGate` 是进程内的，多副本到底会不会出错？

    python scripts/cross_process_write_probe.py --seconds 45 --procs 4 --threads 8

对比「新默认（池+进程内串行化）」与「全关」两组，量 `database is locked`。
SQLite 的 busy handler 在**跨进程**场景是生效的（不像同进程锁升级那样
被绕过），所以预期两组都不会有错；用数据确认，不靠推理。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVO = os.path.join(ROOT, "ai-service", "evoguard")

CHILD = r"""
import json, os, sys, threading, time
sys.path.insert(0, sys.argv[1])
import core
path, pid, secs, threads = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
s = core.Store(path)
deadline = time.monotonic() + secs
writes = [0]; errs = []; lock = threading.Lock()
def worker(k):
    i = 0
    while time.monotonic() < deadline:
        try:
            s.create_experiment("%s_%d_%d" % (pid, k, i), "order", "A1001",
                                "none", 0, 7, "D")
            with lock: writes[0] += 1
        except Exception as exc:
            with lock: errs.append(type(exc).__name__ + ": " + str(exc)[:70])
        i += 1
ts = [threading.Thread(target=worker, args=(k,)) for k in range(threads)]
for t in ts: t.start()
for t in ts: t.join()
s.close()
print("RESULT=" + json.dumps({"pid": pid, "writes": writes[0],
                             "errors": len(errs), "detail": errs[:2]}))
"""


def run(mode, seconds, procs, threads, root):
    path = os.path.join(root, mode + ".db")
    seed_env = dict(os.environ)
    if mode == "off":
        seed_env["AGENTLAB_STORE_POOL"] = "0"
        seed_env["AGENTLAB_STORE_SERIALIZE_WRITES"] = "0"
    else:
        seed_env.pop("AGENTLAB_STORE_POOL", None)
        seed_env.pop("AGENTLAB_STORE_SERIALIZE_WRITES", None)
    # 先建库，避免 4 个进程同时初始化
    subprocess.run([sys.executable, "-c",
                    "import sys;sys.path.insert(0,sys.argv[1]);"
                    "import core;core.Store(sys.argv[2]).close()",
                    EVO, path], env=seed_env, check=True,
                   capture_output=True)
    ps = [subprocess.Popen(
        [sys.executable, "-c", CHILD, EVO, path, "p%d" % i,
         str(seconds), str(threads)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=seed_env) for i in range(procs)]
    rows = []
    for p in ps:
        out, err = p.communicate(timeout=seconds + 90)
        line = next((l for l in out.splitlines()
                     if l.startswith("RESULT=")), "")
        rows.append(json.loads(line[7:]) if line else
                    {"pid": "?", "writes": 0, "errors": -1,
                     "detail": [err[-200:]]})
    return {"mode": mode, "procs": procs, "threads": threads,
            "seconds": seconds,
            "writes": sum(r["writes"] for r in rows),
            "errors": sum(max(0, r["errors"]) for r in rows),
            "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="evo-crossproc-")
    rows = [run(m, a.seconds, a.procs, a.threads, root)
            for m in ("default", "off")]
    for r in rows:
        print("%-8s %d 进程 x %d 线程 / %ds：%d 次写，%d 次错误 %s"
              % (r["mode"], r["procs"], r["threads"], r["seconds"],
                 r["writes"], r["errors"],
                 [x["detail"] for x in r["rows"] if x["errors"]][:1]))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "cross_process_write_probe.json"),
                     "w", encoding="utf-8", newline="\n") as f:
            json.dump({"config": vars(a), "rows": rows}, f,
                      ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
