#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补上「默认开启前」剩下三条本机可验的缺口。

    python scripts/store_remaining_checks.py --out <dir> [--soak-seconds 60]

1. 真实 taskkill /F 强杀（不是 os._exit 模拟）后的数据恢复
2. 较长 soak：连接数 / 堆是否随时间增长
3. 三种存储配置下的磁盘写入量
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVO = os.path.join(ROOT, "ai-service", "evoguard")
sys.path.insert(0, EVO)

import core          # noqa: E402


def _set_env(mode):
    # 显式设值：池默认开之后，"default" 组要显式关才是旧行为
    os.environ["AGENTLAB_STORE_POOL"] = (
        "1" if mode in ("pool", "pool_wal") else "0")
    os.environ.pop("AGENTLAB_STORE_WAL", None)
    if mode in ("pool", "pool_wal"):
        os.environ["AGENTLAB_STORE_POOL"] = "1"
        os.environ["AGENTLAB_STORE_POOL_SIZE"] = "8"
    if mode == "pool_wal":
        os.environ["AGENTLAB_STORE_WAL"] = "1"


def _dir_bytes(d):
    total = 0
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            total += os.path.getsize(p)
    return total


CHILD_HOLD = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
import core
s = core.Store(sys.argv[2])
for i in range(120):
    s.create_experiment("hold-%d" % i, "order", "A1001", "none", 0, 7, "D")
# 故意不 close，让连接/WAL 保持打开，等主进程强杀
sys.stdout.write("READY\n")
sys.stdout.flush()
time.sleep(120)
"""


def check_real_kill(root, mode="pool"):
    """Windows taskkill /F /T：真强杀，不是 os._exit。"""
    _set_env(mode)
    path = os.path.join(root, "kill_%s.db" % mode)
    p = subprocess.Popen(
        [sys.executable, "-c", CHILD_HOLD, EVO, path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        bufsize=1)
    try:
        line = p.stdout.readline().strip()
        if line != "READY":
            return {"id": "real-kill-" + mode, "pass": False,
                    "detail": "子进程没就绪：%r" % line, "evidence": {}}
        # 等一拍确保 120 条已落盘，再强杀整个进程树
        time.sleep(0.3)
        kill = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(p.pid)],
            capture_output=True)
        p.wait(timeout=15)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)

    s = core.Store(path)
    n = s.health()["experiments"]
    with s.conn() as c:
        integ = c.execute("PRAGMA integrity_check").fetchone()[0]
    s.close()
    ok = n == 120 and integ == "ok"
    return {
        "id": "real-kill-" + mode, "pass": bool(ok),
        "detail": ("taskkill /F /T 强杀后重开：%d/120 行，integrity=%s"
                   % (n, integ)),
        "evidence": {"mode": mode, "kill_rc": kill.returncode,
                     "kill_stdout": (kill.stdout or b"").decode(
                         "utf-8", errors="replace").strip()[:200],
                     "kill_stderr": (kill.stderr or b"").decode(
                         "utf-8", errors="replace").strip()[:200],
                     "rows": n, "integrity": integ},
    }


def check_soak(root, seconds=60, workers=8):
    """持续写，每 5 秒采样连接数与 Python 堆，看是否随时间增长。"""
    _set_env("pool")
    d = os.path.join(root, "soak")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "a.db")
    s = core.Store(path)
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    stop = threading.Event()
    errs, samples, written = [], [], [0]
    lock = threading.Lock()

    def worker(k):
        i = 0
        while not stop.is_set():
            try:
                s.create_experiment("s%d_%d" % (k, i), "order", "A1001",
                                    "none", 0, 7, "D")
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
    for _ in range(max(1, seconds // 5)):
        time.sleep(5)
        cur = tracemalloc.get_traced_memory()[0]
        samples.append({"t_s": round(time.monotonic() - t0, 1),
                        "rows": written[0],
                        "pool": s.pool_stats(),
                        "heap_mb": round((cur - base) / 1048576.0, 3),
                        "db_bytes": _dir_bytes(d)})
    stop.set()
    for t in ts:
        t.join(timeout=10)
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    st = s.pool_stats()
    s.close()

    created = [x["pool"]["created"] for x in samples]
    heaps = [x["heap_mb"] for x in samples]
    ok = (not errs and st["created"] <= 32 and st["discarded"] == 0
          and max(heaps) < 8.0 and max(created) <= 32)
    return {
        "id": "soak", "pass": bool(ok),
        "detail": ("%ds / %d 线程共 %d 次写：created %s→%s，"
                   "堆峰值 %.2f MiB，错误 %d"
                   % (seconds, workers, written[0],
                      created[0] if created else "-",
                      created[-1] if created else "-",
                      peak / 1048576.0, len(errs))),
        "evidence": {"seconds": seconds, "workers": workers,
                     "writes": written[0], "samples": samples,
                     "errors": errs[:3], "final_pool": st},
    }


def check_disk_bytes(root, n=200):
    """同负载下三种配置的落盘字节数。"""
    rows = []
    for mode in ("default", "pool", "pool_wal"):
        _set_env(mode)
        d = os.path.join(root, "disk_" + mode)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "a.db")
        s = core.Store(path)
        before = _dir_bytes(d)
        for i in range(n):
            s.create_experiment("d%d" % i, "order", "A1001",
                                "none", 0, 7, "D")
        during = _dir_bytes(d)
        s.close()
        after = _dir_bytes(d)
        rows.append({"mode": mode, "before": before, "during": during,
                     "after": after, "per_write": round(
                         (during - before) / float(n), 1)})
    ok = all(r["during"] >= r["before"] and r["per_write"] > 0
             for r in rows)
    return {
        "id": "disk-bytes", "pass": bool(ok),
        "detail": "; ".join("%s %.1f B/写（close 后 %d B）"
                            % (r["mode"], r["per_write"], r["after"])
                            for r in rows),
        "evidence": {"writes_per_mode": n, "rows": rows,
                     "note": "pool_wal 的 -wal 在 close 时 checkpoint；"
                             "只开池不留 WAL 文件"},
    }


def main():
    ap = argparse.ArgumentParser(description="剩余三条本机可验缺口")
    ap.add_argument("--out", default="")
    ap.add_argument("--soak-seconds", type=int, default=60)
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="evo-remain-")
    checks = [
        check_real_kill(root, "pool"),
        check_real_kill(root, "pool_wal"),
        check_soak(root, a.soak_seconds),
        check_disk_bytes(root),
    ]
    for c in checks:
        print("%s %-20s %s" % ("PASS" if c["pass"] else "FAIL",
                               c["id"], c["detail"]))
    passed = sum(1 for c in checks if c["pass"])
    report = {
        "version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passed": passed == len(checks),
        "summary": {"total": len(checks), "pass": passed,
                    "fail": len(checks) - passed},
        "checks": checks,
        "boundaries": [
            "多主机共享存储做不了（本机单主机）；池仍是进程内语义。",
            "soak 是数十秒级，不是小时级；磁盘量为本地文件系统字节数，"
            "不含 fsync/块对齐开销。",
            "Docker daemon 不可达，EG-003 镜像层仍未实测。",
        ],
    }
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "store_remaining_checks.json"),
                     "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print("== %d/%d pass ==" % (passed, len(checks)))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
