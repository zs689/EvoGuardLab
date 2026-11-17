#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WS-06：租户隔离双进程压测（SQLite 等价 RLS 语义）。

**不是真 PostgreSQL**：本机无 PG。验证的是「强制 tenant_id 过滤」逻辑在
两个并发进程下仍成立，并输出可部署的 RLS_DDL 供真库使用。
    python scripts/rls_stress.py --out docs/review-remediation/probes/rls_stress.json
"""
import argparse
import io
import json
import multiprocessing as mp
import os
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys                                                   # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
import pg_rls as P                                           # noqa: E402


def _probe(tenant, path, out):
    s = P.TenantScopedStore(path)
    r = {"tenant": tenant,
         "own": s.get(tenant, "a1" if tenant == "A" else "b1"),
         "cross_read": s.get(tenant, "b1" if tenant == "A" else "a1"),
         "visible_rows": s.list(tenant)}
    s.close()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False)


def run():
    d = tempfile.mkdtemp(prefix="evo-rls-stress-")
    path = os.path.join(d, "rls.db")
    seed = P.TenantScopedStore(path)
    seed.put("A", "a1", "A-secret")
    seed.put("B", "b1", "B-secret")
    physical = seed.count_all_unsafe()
    seed.close()
    outs = [os.path.join(d, "A.json"), os.path.join(d, "B.json")]
    ps = [mp.Process(target=_probe, args=("A", path, outs[0])),
          mp.Process(target=_probe, args=("B", path, outs[1]))]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=30)
    rows = []
    for f in outs:
        with open(f, encoding="utf-8") as fh:
            rows.append(json.load(fh))
    ok = (physical == 2 and len(rows) == 2
          and all(r["cross_read"] is None for r in rows)
          and all(len(r["visible_rows"]) == 1 for r in rows))
    shutil.rmtree(d, ignore_errors=True)
    return {"backend": "SQLite forced tenant filter (NOT real PostgreSQL)",
            "processes": 2, "physical_rows": physical,
            "probes": rows, "ok": bool(ok),
            "note": "PostgreSQL RLS 未在真库跑；RLS_DDL 可供部署"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rep = run()
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
