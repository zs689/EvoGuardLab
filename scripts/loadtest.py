#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""固定工作负载压测：区分成功 / 受控拒绝 / 服务失败 / 客户端错误。

    python scripts/loadtest.py --requests 24 --workers 8 --rounds 3
    python scripts/loadtest.py --baseline base.json --max-regression 0.15

产物：experiments/loadtest_v1/result.json（汇总）+
      experiments/loadtest_v1/samples.jsonl（原始延迟样本，可重算）

为什么重写：上一版把 `200 <= status < 500` 全算成功 —— 401、404、429
都被计进「可用率」，只测两个读路由，却对外称「SLO 有实测底数」。
独立审查实测的 `POST /api/run` 8 并发 P95 是 3534.7 ms，那个路径根本
没被覆盖。这一版补上写路由，并把四类状态分开报，谁都不许藏。

已知边界（如实写进产物）：
  - 固定并发数不是固定到达率；没有 coordinated omission 控制。
  - 全程回环、单进程、mock 模型；不含 TLS/反代/真实网络。
  - CPU 与峰值 RSS 未采集（零第三方依赖边界，Windows 无 resource 模块）。
  - 没有真实企业流量，绝对门槛需由业务目标给定，本脚本不擅自冻结数字。
"""
import argparse
import io
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import live_client as L                                    # noqa: E402

TOKEN = "loadtest-token-0123456789"
POST_BODY = json.dumps({"agent_type": "order", "task_id": "A1001",
                        "fault_type": "silent_corruption",
                        "mode": "D"}).encode("utf-8")

# 契约允许的受控拒绝：鉴权失败、限流、停止开关、租户配额。
CONTROLLED_REJECT = frozenset((401, 403, 429, 503))


def classify(status):
    """四类互斥。受控拒绝单列，不算成功也不算服务失败。"""
    if status == 0:
        return "connect_failure"
    if 200 <= status < 300:
        return "success"
    if status in CONTROLLED_REJECT:
        return "controlled_reject"
    if 500 <= status < 600:
        return "server_failure"
    return "client_error"


def sli(counts):
    """可用率分母只含「本该成功」的请求；受控拒绝与客户端错误单列。

    分母 = success + server_failure + connect_failure。
    把 429 算进可用率是错的；但把 429 完全不报也是错的 —— 它必须出现在
    status_counts 与 controlled_reject 里，供人判断是不是限流配太紧。
    """
    denom = (counts["success"] + counts["server_failure"]
             + counts["connect_failure"])
    return round(counts["success"] / denom, 6) if denom else None


def run_scenario(port, name, method, path, headers, body, n, workers):
    lat, codes = [], []
    lock = threading.Lock()
    base, extra = divmod(n, workers)

    def worker(count):
        local_lat, local_codes = [], []
        for _ in range(count):
            t0 = time.perf_counter()
            st, _, _ = L.http(port, method, path, headers, body=body)
            local_lat.append(round((time.perf_counter() - t0) * 1000.0, 3))
            local_codes.append(st)
        with lock:
            lat.extend(local_lat)
            codes.extend(local_codes)

    ts = [threading.Thread(target=worker,
                           args=(base + (1 if i < extra else 0),))
          for i in range(workers)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0

    counts = {k: 0 for k in ("success", "controlled_reject",
                             "server_failure", "client_error",
                             "connect_failure")}
    for c in codes:
        counts[classify(c)] += 1
    return {
        "name": name, "method": method, "path": path,
        "requests": len(codes), "workers": workers,
        "wall_s": round(wall, 3),
        "rps": round(len(codes) / wall, 2) if wall > 0 else None,
        "counts": counts,
        "availability": sli(counts),
        "latency_ms": {
            "p50": L.percentile(lat, 50),
            "p95": L.percentile(lat, 95),
            "p99": L.percentile(lat, 99),
            "max": max(lat) if lat else None,
        },
        "status_counts": {str(c): codes.count(c)
                          for c in sorted(set(codes))},
    }, lat


def _median(values):
    s = sorted(values)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def environment():
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "sqlite": sqlite3.sqlite_version,
        "processor": platform.processor() or "unknown",
    }


def main():
    ap = argparse.ArgumentParser(description="固定工作负载压测（四类状态分开报）")
    ap.add_argument("--requests", type=int, default=24,
                    help="每个场景每轮的请求数")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(
        ROOT, "experiments", "loadtest_v1", "result.json"))
    ap.add_argument("--baseline", default="", help="对比用基线 result.json")
    ap.add_argument("--max-regression", type=float, default=0.0,
                    help="允许的 P95 回归比例；0 表示只报告不守门")
    a = ap.parse_args()

    auth = {"Authorization": "Bearer " + TOKEN}
    post = dict(auth)
    post["Content-Type"] = "application/json"
    scenarios = [
        ("read_health", "GET", "/api/health", auth, None),
        ("read_runs", "GET", "/api/runs?limit=60", auth, None),
        ("write_run", "POST", "/api/run", post, POST_BODY),
        ("read_metrics", "GET", "/api/metrics", auth, None),
    ]

    rounds, samples = [], []
    # 审查 handoff_rules：重跑实验写入**独立输出目录**。
    # 以前压测直接用仓库内 data/evoguard.db，历史数据量、日志模式、
    # 甚至别的测试残留都会混进结果，改前改后根本不可比。
    work = tempfile.mkdtemp(prefix="evo-loadtest-")
    data_root = os.path.join(work, "data")
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN,
                    "AGENTLAB_RATE_LIMIT": "1000000",
                    "AGENTLAB_DATA_ROOT": data_root,
                    "AGENTLAB_DB": os.path.join(data_root, "evoguard.db")}) as svc:
        for rnd in range(1, a.rounds + 1):
            items = []
            for name, method, path, headers, body in scenarios:
                rep, lat = run_scenario(svc.port, name, method, path,
                                        headers, body, a.requests, a.workers)
                items.append(rep)
                for v in lat:
                    samples.append({"round": rnd, "scenario": name, "ms": v})
                print("  round %d %-13s rps=%-7s p95=%-8s avail=%s %s"
                      % (rnd, name, rep["rps"], rep["latency_ms"]["p95"],
                         rep["availability"], rep["status_counts"]))
            rounds.append({"round": rnd, "scenarios": items})

    # 跨轮聚合：每轮 P95 的中位数，避免单轮抖动被当成结论
    agg = {}
    for name, *_ in scenarios:
        p95s = [s["latency_ms"]["p95"] for r in rounds
                for s in r["scenarios"] if s["name"] == name
                and s["latency_ms"]["p95"] is not None]
        avails = [s["availability"] for r in rounds
                  for s in r["scenarios"] if s["name"] == name
                  and s["availability"] is not None]
        agg[name] = {
            "p95_median_ms": round(_median(p95s), 2) if p95s else None,
            "p95_all_ms": p95s,
            "availability_min": min(avails) if avails else None,
            "rounds": len(p95s),
        }

    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
    payload = {
        "version": 2,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "revision": revision or "unknown",
        "environment": environment(),
        "config": {
            "requests_per_scenario": a.requests,
            "workers": a.workers, "rounds": a.rounds,
            "auth": "enabled (fixed local test token)",
            "rate_limit": "1000000", "audit": "off",
            "transport": "loopback HTTP, no TLS, no proxy",
            "arrival": "fixed concurrency (not fixed rate)",
            "data_dir": "<isolated temp dir, freshly created per run>",
            "journal_mode": "<fresh database, default>",
        },
        "sli_definition": {
            "success": "2xx",
            "controlled_reject": "401/403/429/503 — 单列，不计入成功率也不计失败",
            "server_failure": "5xx（受控 503 除外）",
            "client_error": "其它 4xx（含 404）",
            "connect_failure": "连接失败/超时（status=0）",
            "availability": "success / (success + server_failure + connect_failure)",
        },
        "rounds": rounds,
        "aggregated": agg,
        "boundaries": [
            "固定并发数，不是固定到达率；未做 coordinated omission 控制。",
            "全程回环、单进程、mock 模型；不含 TLS/反向代理/真实网络抖动。",
            "CPU 与峰值 RSS 未采集（零第三方依赖边界；Windows 无 resource 模块）。",
            "绝对性能门槛未冻结：没有业务目标，不擅自编一个数字当 SLA。",
            "wheel/容器/多副本下的表现未测。",
        ],
    }

    verdict = "未设 --baseline，本轮只报告不判定回归"
    if a.baseline and os.path.isfile(a.baseline):
        try:
            with io.open(a.baseline, encoding="utf-8") as f:
                base = json.load(f)
            bad = []
            for name, cur in agg.items():
                old = (base.get("aggregated", {}).get(name, {})
                       .get("p95_median_ms"))
                new = cur["p95_median_ms"]
                if (old and new and a.max_regression > 0
                        and new > old * (1 + a.max_regression)):
                    bad.append("%s: %s -> %s ms (+%.0f%%)"
                               % (name, old, new, 100 * (new / old - 1)))
            verdict = ("基线对比：%d 项超出允许回归 %.0f%% —— %s"
                       % (len(bad), a.max_regression * 100, "; ".join(bad))
                       if bad else "基线对比：无超出允许回归的场景")
            payload["baseline_regressions"] = bad
        except (OSError, ValueError) as exc:
            verdict = "基线读取失败：%r（未判定）" % exc
    payload["verdict"] = verdict

    L.write_json(a.out, payload)
    # 原始样本单独写 jsonl，便于别人重算百分位
    sp = os.path.join(os.path.dirname(a.out), "samples.jsonl")
    with io.open(sp, "w", encoding="utf-8", newline="\n") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n")

    print("== %d 轮 × %d 场景 × %d 请求 / %d 并发 =="
          % (a.rounds, len(scenarios), a.requests, a.workers))
    for name, cur in agg.items():
        print("  %-13s p95(中位)=%-8s ms  availability(min)=%s"
              % (name, cur["p95_median_ms"], cur["availability_min"]))
    print("  %s" % verdict)
    print("[√] %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
