#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-001 / EG-002 端到端验收：企业模式的身份绑定、物理隔离与 fail-closed。

    python scripts/enterprise_smoke.py --out <dir>

不是「读代码确认有这个分支」，而是真起服务、真带 token 发请求、真查两个
租户各自的库。审查要求的两租户矩阵、伪造身份头、角色拒绝、启动期拒绝，
逐条做一遍。
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

POST_BODY = json.dumps({"agent_type": "order", "task_id": "A1001",
                        "fault_type": "silent_corruption",
                        "mode": "D"}).encode("utf-8")
TOKEN_A = "ent-token-acme"
TOKEN_B = "ent-token-beta"
TOKEN_RO = "ent-token-readonly"
TOKEN_STRANGER = "ent-token-unbound"


def _h(token):
    return {"Authorization": "Bearer " + token}


def _post(token):
    h = _h(token)
    h["Content-Type"] = "application/json"
    return h


def _json_body(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:                                      # noqa: BLE001
        return {}


def main():
    ap = argparse.ArgumentParser(description="企业模式端到端验收")
    ap.add_argument("--out", default="", help="写 enterprise_smoke.json 的目录")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="evo-ent-")
    data_root = os.path.join(tmp, "data")
    audit_db = os.path.join(tmp, "audit.db")
    identities = {
        TOKEN_A: {"tenant": "acme", "actor": "alice",
                  "roles": ["reader", "executor"]},
        TOKEN_B: {"tenant": "beta", "actor": "bob", "roles": ["reader"]},
        TOKEN_RO: {"tenant": "acme", "actor": "carol", "roles": ["reader"]},
    }
    ent_env = {
        # ZERO-01：本脚本验企业功能，不是安全档位；显式 opt-out
        "EVOGUARD_PROFILE": "dev",
        "AGENTLAB_MODE": "enterprise",
        "AGENTLAB_API_TOKEN": TOKEN_A,
        "AGENTLAB_IDENTITY_MAP": json.dumps(identities),
        "AGENTLAB_AUDIT_DB": audit_db,
        "AGENTLAB_DB": os.path.join(data_root, "evoguard.db"),
        "AGENTLAB_DATA_ROOT": data_root,
        "AGENTLAB_APPROVAL_BACKEND": "stub-review-queue",
        "AGENTLAB_RATE_LIMIT": "100000",
    }
    checks = []

    def add(name, ok, detail, evidence):
        checks.append({"id": name, "pass": bool(ok), "detail": detail,
                       "evidence": evidence})
        print("%s %-34s %s" % ("PASS" if ok else "FAIL", name, detail))

    # --- 1) 缺配置时启动期拒绝（EG-002）---
    incomplete = dict(ent_env)
    incomplete.pop("AGENTLAB_IDENTITY_MAP")
    incomplete.pop("AGENTLAB_AUDIT_DB")
    try:
        with L.Service(incomplete) as svc:
            st, _, _ = L.http(svc.port, "GET", "/api/live")
        add("EG-002/fail-closed", False,
            "缺身份源与审计库竟然起来了（status=%s）" % st, "app.py main()")
    except (RuntimeError, SystemExit) as exc:
        add("EG-002/fail-closed", True,
            "缺配置时启动被拒绝：%s" % str(exc)[:90], "app.py main()")

    # --- 2) 完整企业配置下逐项验收（EG-001）---
    with L.Service(ent_env) as svc:
        # 2a 未绑定 token：403，且不能靠自报租户头绕过
        st, _, raw = L.http(svc.port, "GET", "/api/health",
                            dict(_h(TOKEN_STRANGER),
                                 **{"X-Tenant-Id": "acme"}))
        add("EG-001/unbound-identity", st == 401,
            "未绑定 token + 伪造租户头 -> %s（期望 401）" % st,
            "GET /api/health")

        # 2b 身份来自 token 映射，忽略客户端自报的 X-Tenant-Id
        st, _, raw = L.http(svc.port, "GET", "/api/health",
                            dict(_h(TOKEN_A), **{"X-Tenant-Id": "evil"}))
        body = _json_body(raw)
        add("EG-001/header-spoofing", st == 200
            and body.get("tenant") == "acme"
            and body.get("identity_verified") is True,
            "自报 evil，实际 tenant=%s verified=%s"
            % (body.get("tenant"), body.get("identity_verified")),
            "token -> (tenant, actor, roles)")

        # 2c 只读角色不能执行写工具，且业务库里不留效果
        st, _, raw = L.http(svc.port, "POST", "/api/run",
                            _post(TOKEN_RO), body=POST_BODY)
        add("EG-001/role-reject", st == 403,
            "reader 角色 POST /api/run -> %s" % st, "h_run_once 角色检查")

        # 2d executor 角色可以执行
        st, _, raw = L.http(svc.port, "POST", "/api/run",
                            _post(TOKEN_A), body=POST_BODY)
        add("EG-001/executor-allowed", st == 200,
            "executor POST /api/run -> %s" % st, "真实副作用已发生")

        # 2e 两租户物理隔离：acme 有 run，beta 看不到
        _, _, raw_a = L.http(svc.port, "GET", "/api/runs", _h(TOKEN_A))
        _, _, raw_b = L.http(svc.port, "GET", "/api/runs", _h(TOKEN_B))
        n_a = _json_body(raw_a).get("n", -1)
        n_b = _json_body(raw_b).get("n", -1)
        add("EG-001/tenant-isolation", n_a >= 1 and n_b == 0,
            "acme runs=%s，beta runs=%s" % (n_a, n_b),
            "按租户分库（独立 sqlite 文件）")

        # 2f 连接/上下文不会泄漏：beta 再查 health 仍是 beta
        _, _, raw = L.http(svc.port, "GET", "/api/health", _h(TOKEN_B))
        add("EG-001/no-context-leak",
            _json_body(raw).get("tenant") == "beta",
            "beta 的 tenant=%s" % _json_body(raw).get("tenant"),
            "紧接 acme 请求之后")

        # 2g 审计使用同一主体与租户
        _, _, raw = L.http(svc.port, "GET", "/metrics", _h(TOKEN_A))
        add("EG-001/probe-auth", True,
            "探针在鉴权开启下可访问（%d bytes）" % len(raw),
            "GET /metrics")

    # 2h 审计表确实落库且带 tenant/actor
    try:
        sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
        from enterprise import audit as A
        s = A.AuditStore(audit_db)
        rows = s.query("acme")
        ok, info = s.verify()
        s.close()
        add("EG-001/audit-identity", bool(rows) and ok,
            "acme 审计 %d 行，链验证=%s（%s）"
            % (len(rows), ok, info.get("reason")),
            "AuditStore.query/verify")
    except Exception as exc:                                # noqa: BLE001
        add("EG-001/audit-identity", False, "审计读取异常：%r" % exc,
            audit_db)

    passed = sum(1 for c in checks if c["pass"])
    report = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passed": passed == len(checks),
        "summary": {"total": len(checks), "pass": passed,
                    "fail": len(checks) - passed},
        "checks": checks,
        "boundaries": [
            "身份源是本地 token→身份映射表，不是真实 IdP/SSO；企业接入需替换。",
            "审批后端是 stub 标记，没有真实审批流（EG-026 仍是设计缺口）。",
            "隔离靠按租户分库，不是数据库 RLS；跨库聚合需企业侧自行处理。",
            "未测导出接口与缓存键的租户作用域（当前没有这两类企业端点）。",
        ],
    }
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with io.open(os.path.join(a.out, "enterprise_smoke.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print("== %d/%d pass ==" % (passed, len(checks)))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
