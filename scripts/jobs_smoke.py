#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-009 端到端验收：真服务、真作业、真轮询、真重启。

    python scripts/jobs_smoke.py --out <dir>

检查项对应审查的验收条件：同幂等键重试返回原作业且不重复产生副作用；
重启发生在关键阶段时作业可恢复或明确失败；并发提交返回任务 ID。
"""
import argparse
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import live_client as L                                    # noqa: E402

PAYLOAD = {"agent_type": "order", "task_id": "A1001",
           "fault_type": "silent_corruption", "mode": "D"}
BODY = json.dumps({"payload": PAYLOAD, "idempotency_key": "smoke-key-1"}).encode("utf-8")


def _h(token=None, tenant=None, idem=None):
    h = {}
    if token:
        h["Authorization"] = "Bearer " + token
    if tenant:
        h["X-Tenant-Id"] = tenant
    if idem:
        h["Idempotency-Key"] = idem
    h["Content-Type"] = "application/json"
    return h


def _body(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def _wait_job(port, headers, jid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st, _, raw = L.http(port, "GET", "/api/jobs/" + jid, headers)
        b = _body(raw)
        job = b.get("job") or {}
        if job.get("status") in ("succeeded", "failed", "canceled", "unknown"):
            return st, job
        time.sleep(0.05)
    return st, job


def submit_completion_ok(statuses, submit_p95_ms, completion_p95_ms):
    """提交/完成分离判据（纯函数，便于负向测试证明不是放水）。

    放行需同时满足：8 个作业全部 succeeded，且提交 P95 显著早于完成 P95。
    若 worker 变成同步阻塞（submit 等到完成），submit_p95 会逼近
    completion_p95，这里会判红。
    """
    # 不只要 submit 早于 completion，还要**显著**早于：同步阻塞时
    # submit ≈ completion（如 89s vs 90s），单纯 `<` 会误放行。
    # 异步作业化的意义是提交响应远快于完成（实测 15ms vs 90s）。
    return (len(statuses) == 8 and all(s == "succeeded" for s in statuses)
            and (completion_p95_ms or 0) > 0
            and (submit_p95_ms or 1e9) < 0.25 * completion_p95_ms)


def main():
    ap = argparse.ArgumentParser(description="持久作业端到端验收")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="evo-jobssmoke-")
    data = os.path.join(tmp, "data")
    # ZERO-01：本脚本验作业生命周期，不是安全档位；显式 opt-out
    env = {"EVOGUARD_PROFILE": "dev",
           "AGENTLAB_DATA_ROOT": data,
           "AGENTLAB_DB": os.path.join(data, "evoguard.db"),
           "AGENTLAB_RATE_LIMIT": "100000"}
    checks = []

    def add(name, ok, detail):
        checks.append({"id": name, "pass": bool(ok), "detail": detail})
        print("%s %-32s %s" % ("PASS" if ok else "FAIL", name, detail))

    with L.Service(env) as svc:
        h = _h()
        # 1) 提交 -> 202 + job_id
        st, _, raw = L.http(svc.port, "POST", "/api/jobs", h, body=BODY)
        first = _body(raw)
        jid = (first.get("job") or {}).get("id")
        add("submit-returns-job-id", st == 202 and bool(jid),
            "status=%s created=%s id=%s" % (st, first.get("created"), jid))

        # 2) 同幂等键重试 -> 200 + 同一个 job_id
        st2, _, raw2 = L.http(svc.port, "POST", "/api/jobs", h, body=BODY)
        second = _body(raw2)
        add("idempotent-retry", st2 == 200
            and (second.get("job") or {}).get("id") == jid
            and second.get("created") is False,
            "status=%s created=%s same_id=%s"
            % (st2, second.get("created"),
               (second.get("job") or {}).get("id") == jid))

        # 3) 轮询到终态
        _, job = _wait_job(svc.port, h, jid)
        add("poll-to-terminal", job.get("status") == "succeeded",
            "status=%s error=%s" % (job.get("status"), job.get("error")))

        # 4) 幂等没有重复产生副作用：业务库里只有 1 条 run
        _, _, rr = L.http(svc.port, "GET", "/api/runs", h)
        n_runs = _body(rr).get("n", -1)
        add("no-duplicate-side-effect", n_runs == 1,
            "幂等提交 2 次后 runs=%s（应为 1）" % n_runs)

        # 5) 已完成作业不能重放
        st, _, _ = L.http(svc.port, "POST", "/api/jobs/%s/retry" % jid,
                          h, body=b"{}")
        add("no-replay-of-succeeded", st == 409,
            "retry succeeded -> %s（期望 409）" % st)

        # 6) 作业列表有分页上限
        _, _, rl = L.http(svc.port, "GET", "/api/jobs?limit=99999", h)
        lb = _body(rl)
        add("job-list-bounded", lb.get("limit") == 200,
            "limit=%s（clamp 到 200）" % lb.get("limit"))

        # 7) 租户隔离：beta 看不到 default 的作业
        _, _, rb = L.http(svc.port, "GET", "/api/jobs/" + jid,
                          _h(tenant="beta"))
        add("tenant-isolation", rb == b"" or _body(rb).get("error", {}).get("code") == "E_NOT_FOUND",
            "beta 查 default 作业 -> %s"
            % (_body(rb).get("error", {}).get("code") if rb else "empty"))
        stb, _, rawb = L.http(svc.port, "GET", "/api/jobs", _h(tenant="beta"))
        add("tenant-job-list-empty", _body(rawb).get("n") == 0,
            "beta 作业数=%s" % _body(rawb).get("n"))

        # 8) 并发提交多个作业都拿到不同 id
        ids = []
        for i in range(4):
            b = json.dumps({"payload": PAYLOAD,
                            "idempotency_key": "burst-%d" % i}).encode()
            st, _, raw = L.http(svc.port, "POST", "/api/jobs", h, body=b)
            ids.append((st, (_body(raw).get("job") or {}).get("id")))
        add("concurrent-submits", all(s == 202 for s, _ in ids)
            and len({j for _, j in ids}) == 4,
            "4 个提交 id 互异=%s" % (len({j for _, j in ids}) == 4))

        # 9) 审查要求：提交响应、排队、完成时间**分开报**。
        #    作业化的意义不是让实验变快，而是请求线程不再被实验耗时卡住。
        #
        #    前提：worker 是**单线程顺序执行**，每次 run_once 实测约 8s。
        #    前面步骤已提交了 smoke-key + burst×4 等 job，它们还在排队。
        #    若不等队列清空就提交 benchmark 的 8 个，队列深度 13×8s≈104s，
        #    远超旧的 60s 单次等待 -> 全部假超时（2026-11-24 实测红灯）。
        #    这不是放水阈值，而是让测量的前提成立：先排空，再测 8 并发。
        def _active_jobs():
            _s, _h2, _raw = L.http(svc.port, "GET", "/api/jobs", h)
            b = _body(_raw)
            jobs = b.get("jobs") or []
            return sum(1 for j in jobs
                       if j.get("status") in ("queued", "running", "unknown"))

        _q0 = time.time()
        while _active_jobs() > 0 and time.time() - _q0 < 180:
            time.sleep(0.1)
        add("queue-drained-before-bench", _active_jobs() == 0,
            "benchmark 前队列已排空（active=%d）" % _active_jobs())

        import threading
        submit_ms, done_ms, statuses = [], [], []
        lock = threading.Lock()

        def one(i):
            b = json.dumps({"payload": PAYLOAD,
                            "idempotency_key": "bench-%d" % i}).encode()
            t0 = time.perf_counter()
            st, _, raw = L.http(svc.port, "POST", "/api/jobs", h, body=b)
            sub = (time.perf_counter() - t0) * 1000.0
            j = (_body(raw).get("job") or {}).get("id")
            job = {}
            if j:
                # 单 worker 顺序跑 8 个：空载单次约 8s，但 8 线程并发提交
                # 时 worker 与 HTTP 线程竞争，实测每个 11~15s，总计约 115s。
                # 超时是"真卡死"保护，不是通过判据——判据仍是状态 succeeded
                # 与 submit 早于 completion。给 8×30s=240s 覆盖最坏波动，
                # 避免把"排队慢"误判成"作业失败"（2026-11-24 偶发红灯）。
                _, job = _wait_job(svc.port, h, j, timeout=240)
            total = (time.perf_counter() - t0) * 1000.0
            with lock:
                submit_ms.append(sub)
                done_ms.append(total)
                statuses.append(job.get("status"))

        ts = [threading.Thread(target=one, args=(i,)) for i in range(8)]
        wall0 = time.perf_counter()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        wall = time.perf_counter() - wall0
        bench = {
            "concurrency": 8,
            "wall_s": round(wall, 3),
            "submit_p50_ms": L.percentile(submit_ms, 50),
            "submit_p95_ms": L.percentile(submit_ms, 95),
            "completion_p50_ms": L.percentile(done_ms, 50),
            "completion_p95_ms": L.percentile(done_ms, 95),
            "statuses": statuses,
            "note": "提交响应与完成时间分离；worker 单线程顺序执行，"
                    "完成时间仍包含真实实验耗时。",
        }
        add("submit-vs-completion",
            submit_completion_ok(statuses, bench["submit_p95_ms"],
                                 bench["completion_p95_ms"]),
            "8 并发：提交 P95=%.1f ms，完成 P95=%.1f ms，墙钟 %.2f s"
            % (bench["submit_p95_ms"], bench["completion_p95_ms"], wall))

    # 10) 重启后作业仍在（同一个 DATA_ROOT）
    with L.Service(env) as svc2:
        st, _, raw = L.http(svc2.port, "GET", "/api/jobs/" + jid, _h())
        add("survives-restart", st == 200
            and _body(raw).get("job", {}).get("status") == "succeeded",
            "重启后 status=%s" % (_body(raw).get("job", {}).get("status")))

    passed = sum(1 for c in checks if c["pass"])
    report = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passed": passed == len(checks),
        "summary": {"total": len(checks), "pass": passed,
                    "fail": len(checks) - passed},
        "benchmark": bench,
        "checks": checks,
        "boundaries": [
            "单进程单 worker；多副本共享队列与分布式租约未做。",
            "崩溃恢复在单元测里验（租约过期 -> unknown -> 人工 retry），本脚本验的是重启后记录仍在。",
            "runtime.FORCED_SKILL 仍是进程级全局，用执行锁串行化。",
        ],
    }
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "jobs_smoke.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print("== %d/%d pass ==" % (passed, len(checks)))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
