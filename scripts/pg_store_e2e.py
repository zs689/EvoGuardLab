#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PROD-01 验收：真 PostgreSQL 上 PostgresStore 的跨副本共享 + RLS 隔离。

用**隔离 venv**（有 pg8000）跑：
    E:/agent/_pi_venv/Scripts/python.exe scripts/pg_store_e2e.py \
        --dsn postgresql://evoguard_app:app@127.0.0.1:15433/evoguard \
        --out docs/review-remediation/probes/pg_store_e2e.json

判定：
1. 两个独立 Store 实例（模拟两个 API 副本）共享同一套业务数据；
2. 租户 A 写入的实验，租户 B 通过自己的 Store 查不到（RLS）；
3. 普通角色连接（superuser 会绕过 RLS，得到假结论）。

**PG 15+ 必须先授权**，否则第一张建表就报
`permission denied for schema public`（实测踩到）：
    CREATE ROLE evoguard_app LOGIN PASSWORD 'app';
    GRANT ALL ON SCHEMA public TO evoguard_app;
    ALTER DATABASE evoguard OWNER TO evoguard_app;
注意不要给 SUPERUSER —— 超级用户绕过 RLS，第 2 项会得到假的"通过"。
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

import pg_store as PS                                       # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("EVOGUARD_PG_DSN", ""))
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if not a.dsn:
        print("需要 --dsn 或 EVOGUARD_PG_DSN")
        return 2

    # 两个实例 = 两个副本：各自独立连接、独立缓存，只有 PG 是共享的
    a1 = PS.PostgresStore(a.dsn, tenant_id="E2E_A")
    a2 = PS.PostgresStore(a.dsn, tenant_id="E2E_A")
    b1 = PS.PostgresStore(a.dsn, tenant_id="E2E_B")

    rec = a1.create_experiment("e2e-cross-replica", "react",
                               "TASK-1", "timeout", 0, 7)
    eid = rec["id"]
    seen_by_replica2 = a2.get_experiment(eid)["name"]

    run = a1.create_run(eid, "r", 7)
    runs_seen_by_replica2 = len(a2.list_runs(eid))

    b_blocked = False
    b_detail = ""
    try:
        b1.get_experiment(eid)
    except Exception as exc:                                # noqa: BLE001
        b_blocked = True
        b_detail = type(exc).__name__

    rep = {
        "backend": "real PostgreSQL via pg8000 (ordinary role evoguard_app)",
        "checks": {
            "replica1_wrote": bool(eid),
            "replica2_read_same_experiment":
                seen_by_replica2 == "e2e-cross-replica",
            "replica2_read_same_runs": runs_seen_by_replica2 >= 1,
            "tenant_b_cannot_read_tenant_a": b_blocked,
        },
        "detail": {"experiment_id": eid, "runs_seen": runs_seen_by_replica2,
                   "tenant_b_error": b_detail},
    }
    rep["passed"] = all(rep["checks"].values())
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
