#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-010 连接池「默认开启前五条」的稳定性验收。

    python scripts/store_pool_stability.py --out <dir>

只验证，不改默认值。四组实验各自真跑，结果写 JSON。
"""
import argparse
import io
import json
import os
import shutil
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


def _fresh_env(wal=True):
    os.environ["AGENTLAB_STORE_POOL"] = "1"
    os.environ["AGENTLAB_STORE_POOL_SIZE"] = "8"
    if wal:
        os.environ["AGENTLAB_STORE_WAL"] = "1"
    else:
        os.environ.pop("AGENTLAB_STORE_WAL", None)


def check_bounded(root):
    """连接数与 Python 堆不能随操作次数线性增长。"""
    path = os.path.join(root, "bounded.db")
    s = core.Store(path)
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]

    def worker(n, offset):
        for i in range(n):
            s.create_experiment("e%d_%d" % (offset, i), "order", "A1001",
                                "none", 0, 7, "D")

    for phase, n in ((1, 512), (2, 1536)):
        ts = [threading.Thread(target=worker, args=(n // 8, k))
              for k in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if phase == 1:
            after_first = tracemalloc.get_traced_memory()[0]
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    st = s.pool_stats()
    n_total = s.health()["experiments"]
    s.close()
    growth = peak - base
    expected_rows = 512 + 1536             # 都能被 8 整除，别把余数弄丢
    ok = (st["created"] <= 32 and st["discarded"] == 0
          and n_total == expected_rows and growth < 8 * 1024 * 1024)
    return {
        "id": "bounded", "pass": ok,
        "detail": ("%d 次并发写：created=%s discarded=%s idle=%s，"
                   "Python 堆峰值增量 %.2f MiB"
                   % (expected_rows, st["created"], st["discarded"],
                      st["idle"], growth / 1048576.0)),
        "evidence": {"pool": st, "rows": n_total,
                     "heap_growth_bytes": growth,
                     "heap_after_first_phase": after_first - base},
    }


def check_backup(root, wal=True):
    """WAL 备份必须连 -wal 一起；只复制 .db 会丢未 checkpoint 的事务。"""
    if not wal:
        return {"id": "wal-backup", "pass": True, "skipped": True,
                "detail": "只开池（无 WAL），本项不适用", "evidence": {}}
    src = os.path.join(root, "src.db")
    s = core.Store(src)
    for i in range(200):
        s.create_experiment("b%d" % i, "order", "A1001", "none", 0, 7, "D")
    # 不 close、不 checkpoint，让事务留在 -wal 里
    files = [f for f in os.listdir(root)
             if f.startswith("src.db")]
    has_wal = any(f.endswith("-wal") for f in files)

    def restore(copy_wal):
        d = os.path.join(root, "restore_wal" if copy_wal else "restore_db")
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, "src.db")
        shutil.copy2(src, dst)
        if copy_wal:
            for suffix in ("-wal", "-shm"):
                p = src + suffix
                if os.path.isfile(p):
                    shutil.copy2(p, dst + suffix)
        c = core.Store(dst)
        n = c.health()["experiments"]
        integ = c.journal_mode() if hasattr(c, "journal_mode") else "n/a"
        c.close()
        return n

    with_wal, without_wal = restore(True), restore(False)
    s.close()
    ok = with_wal == 200 and without_wal < 200
    return {
        "id": "wal-backup", "pass": bool(ok),
        "detail": ("复制 db+wal 恢复 %d/200 行；只复制 db 恢复 %d/200 行"
                   % (with_wal, without_wal)),
        "evidence": {"wal_file_present": has_wal, "files": files,
                     "with_wal": with_wal, "db_only": without_wal,
                     "conclusion": "WAL 未 checkpoint 的事务只在 -wal 里，"
                                   "备份必须一起复制"},
    }


CHILD_MULTI = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
import core
s = core.Store(sys.argv[2])
for i in range(100):
    s.create_experiment("%s-%d" % (sys.argv[3], i), "order", "A1001",
                        "none", 0, 7, "D")
s.close()
print("ok")
"""

CHILD_KILL = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
import core
s = core.Store(sys.argv[2])
for i in range(50):
    s.create_experiment("kill-%d" % i, "order", "A1001", "none", 0, 7, "D")
# 故意不 close：模拟被强杀
os._exit(0)
"""


def _mp_round(root, i):
    """一轮：常驻 holder + 两个独立进程各写 100 条（保留真实写入量，
    不把竞态窗口人为压小）。"""
    path = os.path.join(root, "mp%d.db" % i)
    holder = core.Store(path)
    procs = [subprocess.Popen(
        [sys.executable, "-c", CHILD_MULTI, EVO, path, "p%d" % k],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for k in range(2)]
    outs = [p.communicate(timeout=90) for p in procs]
    rcs = [p.returncode for p in procs]
    holder.close()
    s = core.Store(path)
    n = s.health()["experiments"]
    with s.conn() as c:
        integ = c.execute("PRAGMA integrity_check").fetchone()[0]
    s.close()
    errs = [o[1][-200:].replace("\n", " ") for o in outs if o[1]]
    return {"rcs": rcs, "rows": n, "integrity": integ, "stderr": errs}


def check_multiprocess(root):
    """量失败率，不用单次二值判定碰运气。

    实测：无论是否加跨进程初始化锁、是否让 holder 常驻，Windows 上
    两个进程冷启动同一个 WAL 库仍有约 10% 概率报 readonly。所以这里
    跑 10 轮，把失败率写进报告 —— **这是未通过项，不是偶尔的噪声**。
    """
    rounds = [_mp_round(root, i) for i in range(10)]
    bad = [r for r in rounds
           if any(x != 0 for x in r["rcs"]) or r["rows"] != 200
           or r["integrity"] != "ok"]
    n_fail = len(bad)
    ok = n_fail == 0
    return {
        "id": "multiprocess", "pass": bool(ok),
        "detail": ("10 轮 × 2 进程各写 100 条：%d 轮失败（%s）"
                   % (n_fail,
                      bad[0]["stderr"][0][-120:] if bad and bad[0]["stderr"]
                      else "行数/RC 不符" if bad else "全部 200/200")),
        "evidence": {"rounds": 10, "failed_rounds": n_fail,
                     "failed_detail": bad[:3],
                     "note": "跨进程 WAL 冷启动 flaky；默认不开启的硬理由"},
    }


def check_sigkill(root):
    path = os.path.join(root, "kill.db")
    seed = core.Store(path)
    seed.close()
    p = subprocess.Popen(
        [sys.executable, "-c", CHILD_KILL, EVO, path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(timeout=90)
    s = core.Store(path)
    n = s.health()["experiments"]
    with s.conn() as c:
        integ = c.execute("PRAGMA integrity_check").fetchone()[0]
    s.close()
    ok = p.returncode == 0 and n == 50 and integ == "ok"
    return {
        "id": "sigkill-recovery", "pass": bool(ok),
        "detail": ("子进程 os._exit 强退（不 close）后重开：%d/50 行，"
                   "integrity=%s" % (n, integ)),
        "evidence": {"returncode": p.returncode, "rows": n,
                     "integrity": integ, "stderr": err[-300:]},
    }


def check_wal_concurrency(root, wal=True, rounds=8):
    """单进程 24 线程并发写 WAL：量 readonly 失败率。

    实测约 1/8 概率失败。这一项**故意判红**：不把它写成「偶发噪声」，
    WAL 在这套连接语义下就是不可靠，是默认不开启的硬理由。
    """
    if not wal:
        return {"id": "wal-concurrency", "pass": True, "skipped": True,
                "detail": "只开池（无 WAL），本项不适用", "evidence": {}}
    bad, samples = 0, []
    for r in range(rounds):
        d = os.path.join(root, "walc%d" % r)
        os.makedirs(d, exist_ok=True)
        s = core.Store(os.path.join(d, "a.db"))
        with s.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
        errs = []

        def one(i):
            try:
                s.create_experiment("w%d" % i, "order", "A1001",
                                    "none", 0, 7, "D")
            except Exception as exc:        # noqa: BLE001
                errs.append(type(exc).__name__ + ": " + str(exc)[:80])

        ts = [threading.Thread(target=one, args=(i,)) for i in range(24)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        n = s.health()["experiments"]
        s.close()
        if errs or n != 24:
            bad += 1
        samples.append({"round": r, "errors": len(errs), "rows": n,
                        "first": errs[:1]})
    return {
        "id": "wal-concurrency", "pass": bad == 0,
        "detail": ("%d 轮 × 24 并发 WAL 写：%d 轮出现 readonly/缺行"
                   % (rounds, bad)),
        "evidence": {"rounds": rounds, "failed_rounds": bad,
                     "samples": samples,
                     "note": "故意判红：WAL 在当前连接语义下不可靠"},
    }


def main():
    ap = argparse.ArgumentParser(description="连接池稳定性验收")
    ap.add_argument("--out", default="")
    ap.add_argument("--no-wal", action="store_true",
                    help="只开连接池、不开 WAL（推荐配置）")
    ap.add_argument("--name", default="store_pool_stability",
                    help="输出 JSON 文件名（不含扩展名）")
    a = ap.parse_args()
    wal = not a.no_wal
    _fresh_env(wal)
    root = tempfile.mkdtemp(prefix="evo-poolstab-")
    checks = []
    for fn in (check_bounded,
               lambda r: check_backup(r, wal),
               check_multiprocess,
               check_sigkill,
               lambda r: check_wal_concurrency(r, wal)):
        try:
            r = fn(root)
        except Exception as exc:              # noqa: BLE001
            r = {"id": getattr(fn, "__name__", "lambda"), "pass": False,
                 "detail": "异常：%r" % exc, "evidence": {}}
        checks.append(r)
        print("%s %-22s %s"
              % ("SKIP" if r.get("skipped") else
                 ("PASS" if r["pass"] else "FAIL"),
                 r["id"], r["detail"]))
    graded = [c for c in checks if not c.get("skipped")]
    passed = sum(1 for c in graded if c["pass"])
    report = {
        "version": 2, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "env": {"AGENTLAB_STORE_POOL": "1",
                "AGENTLAB_STORE_WAL": "1" if wal else "0",
                "pool_size": 8},
        "passed": passed == len(graded),
        "summary": {"total": len(graded), "pass": passed,
                    "fail": len(graded) - passed,
                    "skipped": len(checks) - len(graded)},
        "checks": checks,
        "boundaries": [
            "本机 Windows 单主机；多主机共享存储未验。",
            "SIGKILL 用 os._exit 模拟，不是真的 taskkill /F，同样不刷 WAL。",
            "长时验证是 2048 次操作 / 8 线程，不是小时级 soak test。",
            "wal-concurrency 故意判红：WAL 在这套连接语义下会偶发 readonly。",
        ],
    }
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, a.name + ".json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print("== %d/%d pass ==" % (passed, len(graded)))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
