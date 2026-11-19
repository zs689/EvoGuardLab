#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""INFRA-CONC-01 验收：真 PG 上跨副本共享审计链。

    E:/agent/_pi_venv/Scripts/python.exe scripts/pg_audit_e2e.py \
        --dsn postgresql://evoguard_app:app@127.0.0.1:15435/evoguard \
        --out docs/review-remediation/probes/pg_audit_e2e.json

两个独立实例模拟两个 API 副本：副本 A 写、副本 B 也写、两边都能验完整链；
篡改一条后任一副本必须验出 hash_mismatch。
"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from enterprise.audit import PostgresAuditStore                 # noqa: E402


def event(rid, who, detail):
    return {"tenant_id": "TEN_A", "request_id": rid, "actor": who,
            "method": "POST", "path": "/api/x", "status": 200,
            "decision": "allow", "detail": detail, "latency_ms": 1.234}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("EVOGUARD_PG_DSN", ""))
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if not a.dsn:
        print("需要 --dsn 或 EVOGUARD_PG_DSN")
        return 2

    a1 = PostgresAuditStore(a.dsn, tenant_id="TEN_A", hmac_key="e2e-key")
    a2 = PostgresAuditStore(a.dsn, tenant_id="TEN_A", hmac_key="e2e-key")
    r1 = a1.emit(event("r1", "alice", "副本A写入"))
    r2 = a2.emit(event("r2", "bob", "副本B写入"))
    ok_a, d_a = a1.verify()
    ok_b, d_b = a2.verify()
    with a1.store.conn() as c:
        c.execute("UPDATE audit_events SET detail='篡改' WHERE id=?", (r2,))
    ok_t, d_t = a2.verify()

    rep = {"backend": "real PostgreSQL, two independent AuditStore instances",
           "checks": {
               "both_replicas_wrote": bool(r1 and r2),
               "replica_a_verifies_full_chain":
                   ok_a and d_a.get("checked") >= 2,
               "replica_b_verifies_full_chain":
                   ok_b and d_b.get("checked") >= 2,
               "tamper_detected": (not ok_t)
                   and d_t.get("reason") == "hash_mismatch"},
           "detail": {"r1": r1, "r2": r2, "a": d_a, "b": d_b,
                      "tampered": d_t}}
    rep["passed"] = all(rep["checks"].values())
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
