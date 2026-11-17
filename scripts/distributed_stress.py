#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WS-06-T1 验收：真 · 双副本并发压测（Redis 5.0.9，本机可用）。

    python scripts/distributed_stress.py --out docs/.../distributed_stress.json

脚本自己启动一个临时 redis-server（随机端口），跑完 terminate ——
不依赖外部服务常驻、不在用户机器留下进程。

三个判定（**跨副本**才有意义，单进程内存态做不到）：
1. 限流配额：两个进程各抢 N 次，总量不得超过 limit（否则配额翻倍）；
2. 作业消费：两个 worker 抢 M 个作业，每个恰好被消费一次（不重复、不丢）；
3. 熔断状态：A 进程记录失败，B 进程必须立刻看到 open。
"""
import argparse
import io
import json
import multiprocessing as mp
import os
import shutil
import socket
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import sys                                                   # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import redis_backend as RB                                   # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_redis():
    if not shutil.which("redis-server"):
        return None, "redis-server 不在 PATH"
    d = tempfile.mkdtemp(prefix="evo-redis-stress-")
    port = _free_port()
    log = open(os.path.join(d, "redis.log"), "wb")
    try:
        p = subprocess.Popen(
            ["redis-server", "--port", str(port), "--save", "",
             "--appendonly", "no", "--dir", d, "--bind", "127.0.0.1"],
            stdout=log, stderr=subprocess.STDOUT)
    except OSError as exc:
        return None, repr(exc)
    url = "redis://127.0.0.1:%d/0" % port
    for _ in range(50):
        try:
            if RB.health(url):
                return (p, url, d, log), ""
        except RB.RedisError:
            pass
        time.sleep(0.1)
    p.terminate()
    return None, "Redis 启动超时"


def _stop_redis(bundle):
    if not bundle:
        return
    p, _url, d, log = bundle
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    try:
        log.close()
    except Exception:
        pass
    shutil.rmtree(d, ignore_errors=True)


# ---- 子进程任务 ----
def _rate_worker(url, n, out, tag):
    lim = RB.RedisLimiter(url=url)
    ok = 0
    for _ in range(n):
        allowed, _wait = lim.check("shared-actor", limit=20, window=60)
        ok += int(bool(allowed))
    out.put((tag, ok))
    lim.resp.close()


def _job_worker(url, out, tag):
    q = RB.RedisJobQueue(url=url)
    got = []
    for _ in range(50):
        job = q.claim(lease=60.0)
        if not job:
            break
        got.append(job["id"])
        q.ack(job)
    out.put((tag, got))
    q.resp.close()


def _breaker_worker(url, out, tag, fail=True):
    b = RB.RedisCircuitBreaker(url=url)
    if fail:
        # A：写 5 次失败让熔断打开
        for _ in range(5):
            b.record("shared", True, threshold=5, cooldown=60)
    # B：只读（fail=False 时），验证能跨进程看到 A 写的状态
    out.put((tag, b.is_open("shared")))
    b.resp.close()


def run(url):
    report = {}

    # 1) 跨副本限流
    q = mp.Queue()
    procs = [mp.Process(target=_rate_worker, args=(url, 30, q, "A")),
             mp.Process(target=_rate_worker, args=(url, 30, q, "B"))]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    got = [q.get(timeout=5) for _ in procs]
    total = sum(x[1] for x in got)
    report["rate_limit"] = {
        "per_process": dict(got), "total_allowed": total, "limit": 20,
        "ok": total == 20,
        "note": "跨副本总和必须等于 limit；大于即配额翻倍"}

    # 2) 跨副本作业抢占
    q2 = mp.Queue()
    queue = RB.RedisJobQueue(url=url)
    for i in range(40):
        queue.enqueue(json.dumps({"id": "job-%02d" % i}))
    procs = [mp.Process(target=_job_worker, args=(url, q2, "A")),
             mp.Process(target=_job_worker, args=(url, q2, "B"))]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    got2 = [q2.get(timeout=5) for _ in procs]
    ids = [j for _, js in got2 for j in js]
    report["job_queue"] = {
        "claimed": len(ids), "unique": len(set(ids)), "expected": 40,
        "per_worker": {k: len(v) for k, v in got2},
        "ok": len(ids) == 40 and len(set(ids)) == 40,
        "note": "每个作业恰好被一个 worker 消费一次"}
    queue.resp.close()

    # 3) 跨副本熔断共享：A 写 5 次失败，B 立刻应看到 open
    q3 = mp.Queue()
    pA = mp.Process(target=_breaker_worker,
                    args=(url, q3, "A", True))
    pA.start(); pA.join(timeout=60)
    a_open = q3.get(timeout=5)[1]
    pB = mp.Process(target=_breaker_worker,
                    args=(url, q3, "B", False))
    pB.start(); pB.join(timeout=60)
    b_sees = q3.get(timeout=5)[1]
    report["circuit_breaker"] = {
        "writer_sees_open": a_open, "other_process_sees_open": b_sees,
        "ok": bool(a_open and b_sees),
        "note": "B 进程不写任何失败，只读到 A 的状态才算共享"}
    report["passed"] = all(v["ok"] for v in report.values())
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--url", default="", help="用现有 Redis 跑（不自动起）")
    a = ap.parse_args()

    owned = None
    url = a.url
    if not url:
        owned, err = _start_redis()
        if not owned:
            print("无法启动 Redis：%s" % err)
            return 1
        url = owned[1]
    try:
        rep = run(url)
    finally:
        if owned:
            _stop_redis(owned)
    rep["backend"] = "redis-5.x (RESP over stdlib socket)"
    rep["processes"] = 2
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
