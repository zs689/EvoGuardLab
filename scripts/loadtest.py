#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对本地真服务做一轮压测，产出 SLO 的实测底数。

    python scripts/loadtest.py --requests 600 --workers 16

产物：experiments/loadtest_v1/result.json

为什么需要它：`resilience.SLO.evaluate()` 的算术是对的，但那只证明公式没错。
没压过就说「我们 P95 小于 500ms」是编的 —— 这里给出的是**这台机器、这个
配置、这一轮**的实测值，换机器要重跑。

已知边界（如实写进产物）：
  - 全程回环，不经过反向代理、TLS、真实网络 —— 测的是应用层处理时间下限。
  - 指标是进程内内存，单进程语义。
  - 被测路由是 /api/health 与 /api/metrics，不是重型业务路径。
"""
import argparse
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import live_client as L                                    # noqa: E402

TOKEN = "loadtest-token-0123456789"


def run_round(port, path, headers, total, workers):
    lat, codes = [], []
    lock = threading.Lock()
    # 余数要发出去：total//workers*workers 会把 600 打成 592，
    # 于是「请求数」这一列和你传的参数对不上，还不好发现。
    base, extra = divmod(total, workers)

    def worker(n):
        local_lat, local_codes = [], []
        for _ in range(n):
            t0 = time.perf_counter()
            st, _, _ = L.http(port, "GET", path, headers)
            local_lat.append((time.perf_counter() - t0) * 1000.0)
            local_codes.append(st)
        with lock:
            lat.extend(local_lat)
            codes.extend(local_codes)

    ts = [threading.Thread(target=worker, args=(base + (1 if i < extra else 0),))
          for i in range(workers)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0
    return lat, codes, wall


def main():
    ap = argparse.ArgumentParser(description="本地压测，产出 SLO 实测底数")
    ap.add_argument("--requests", type=int, default=600)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--out", default=os.path.join(
        ROOT, "experiments", "loadtest_v1", "result.json"))
    a = ap.parse_args()

    auth = {"Authorization": "Bearer " + TOKEN}
    rounds = []
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN,
                    "AGENTLAB_RATE_LIMIT": "100000"}) as svc:
        for path in ("/api/health", "/api/metrics"):
            lat, codes, wall = run_round(svc.port, path, auth,
                                         a.requests, a.workers)
            n = len(codes)
            ok = sum(1 for c in codes if 200 <= c < 500)
            rounds.append({
                "path": path,
                "requests": n,
                "requested": a.requests,
                "wall_s": round(wall, 3),
                "rps": round(n / wall, 1) if wall > 0 else None,
                "ok": ok,
                "connect_failures": sum(1 for c in codes if c == 0),
                "availability": round(ok / n, 5) if n else None,
                "latency_ms": {
                    "p50": round(L.percentile(lat, 50), 2),
                    "p95": round(L.percentile(lat, 95), 2),
                    "p99": round(L.percentile(lat, 99), 2),
                    "max": round(max(lat), 2) if lat else None,
                },
                "status_counts": {str(c): codes.count(c)
                                  for c in sorted(set(codes))},
            })

    # 用实测数字去过 SLO，而不是先定结论再找数
    from enterprise.resilience import SLO
    slo_avail = SLO("availability", 0.99)
    evals = []
    for r in rounds:
        n = r["requests"]
        good = r["ok"]
        e = slo_avail.evaluate(n, good)
        e["path"] = r["path"]
        evals.append(e)

    worst_p95 = max((r["latency_ms"]["p95"] or 0) for r in rounds) if rounds else 0
    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target": {"host": "127.0.0.1", "requests_per_round": a.requests,
                   "workers": a.workers, "tls": False, "proxy": False},
        "rounds": rounds,
        "slo": {"availability_target": 0.99, "evaluations": evals,
                "worst_p95_ms": worst_p95},
        "boundaries": [
            "全程回环：不含 TLS、反向代理、真实网络抖动，是应用层处理时间的下限。",
            "指标为进程内内存，单进程语义；多 worker 需按 instance 分别抓取。",
            "被测路由为 /api/health 与 /api/metrics，不代表重型业务路径。",
            "本文件由 --requests/--workers 决定，换机器或换参数需重跑。",
        ],
        "verdict": ("availability 达到 99% 目标" if all(e["met"] for e in evals)
                    else "availability 未达到 99% 目标（如实记录，未调参凑数）"),
    }
    L.write_json(a.out, payload)

    print("== 压测结果（%d 请求 / %d 并发）==" % (a.requests, a.workers))
    for r in rounds:
        print("  %-14s rps=%-7s p50=%-6s p95=%-6s p99=%-6s %s"
              % (r["path"], r["rps"], r["latency_ms"]["p50"],
                 r["latency_ms"]["p95"], r["latency_ms"]["p99"],
                 r["status_counts"]))
    print("  SLO(availability 99%%): %s" % payload["verdict"])
    print("[√] %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
