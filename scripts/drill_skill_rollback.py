#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自进化安全阀演练：让「退化 -> 自动回滚」在真实数据上跑一次。

为什么单独做演练：库里 skill_rollback_audit 记录数是 0 —— 回滚代码有单测，
但**从未在真实技能上被触发过**。安全阀不被演练过，就等于没有。

本脚本在**独立演练库**里造一个已晋升技能，注入线上退化，触发
monitor_and_rollback，并校验三件事：
  1) 判定为 degraded 且给出可核对的理由；
  2) 技能真的被禁用（不是只记日志）；
  3) 回滚审计有记录、后续注入立即停止。

生产库只做 dry_run 巡检（不改任何状态），因此可以安全地反复运行。

用法：
  python scripts/drill_skill_rollback.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import skill_synthesis as SS                               # noqa: E402
import skills as SK                                        # noqa: E402


def _trace(stderr, seq=1):
    return {"seq": seq, "logical": seq, "name": "code.run_in_sandbox",
            "status": "error", "error_type": "AssertionError",
            "output": {"stderr": stderr}}


def _cases(prefix, n=5):
    return [{"case_id": "%s%d" % (prefix, i), "agent_type": "order",
             "task_id": "%s%d" % (prefix, i), "fault_type": "missing_field",
             "seed": 7} for i in range(n)]


def run_drill():
    tmp = tempfile.mkdtemp(prefix="drill-")
    import core
    store = core.Store(os.path.join(tmp, "drill.db"))
    steps = []

    # 1) 学出一个候选并真的晋升（走完整 audit -> promote）
    out = SS.synthesize(store, [_trace("page index must start at 1, got 0")],
                        task_type="order", require_info_gain=True)
    sk = out["skill"]
    SK.save(store, sk)
    steps.append({"step": "synthesize", "skill_id": sk["id"],
                  "invariants": len(sk.get("env_invariants") or [])})

    def paired(case, active_skill):
        """target：不带技能失败、带技能成功（增益 100pp）。
        guard：两期都必须成功（技能不得破坏原有能力）。"""
        if str(case.get("case_id", "")).startswith("g"):
            return True
        return active_skill is not None

    # 必须传训练用例：training_overlap_checked 为 False 时晋升门禁拒绝
    # （这正是设计意图——没说清训练/验证边界的评估不能用）。
    ev = SK.evaluate(store, sk, _cases("t"), _cases("g"), paired,
                     training_cases=_cases("z", 1))
    steps.append({"step": "audit", "verdict": ev["verdict"],
                  "gain_pp": ev["gain_pp"], "chance_p": ev.get("chance_p_value"),
                  "target_n": ev["target_n"]})
    if ev["verdict"] != "PROMOTE":
        return {"ok": False, "stage": "audit", "steps": steps,
                "reason": "演练前提不成立：未晋升，无法验证回滚"}
    SK.promote(store, sk["id"], evaluation_id=ev["id"])
    steps.append({"step": "promote", "status": "active"})

    # 2) 注入线上退化（成功率从 ~1.0 掉到 0.1）
    degraded_stats = {"n": 40, "success_rate": 0.10, "unauthorized": 0,
                      "warnings": 38, "runs": 40}
    res = SS.monitor_and_rollback(store, sk["id"], online_runs=degraded_stats)
    steps.append({"step": "monitor", "degraded": res["degraded"],
                  "reasons": res["reasons"][:3],
                  "rolled_back": res["rolled_back"],
                  "baseline": res["baseline"]})

    # 3) 校验安全阀真的关上了
    with store.conn() as c:
        row = c.execute("SELECT status FROM skills WHERE id=?",
                        (sk["id"],)).fetchone()
        audits = c.execute("SELECT COUNT(*) n FROM skill_rollback_audit").fetchone()["n"]
    status_after = row["status"] if row else None
    steps.append({"step": "verify", "status_after": status_after,
                  "rollback_audit_rows": audits})

    # 4) 回滚后不能再被注入
    sk_after = [r for r in SK.list_skills(store) if r["id"] == sk["id"]][0]
    active_inv = SS.active_invariants(sk_after)
    injectable = [r for r in SK.list_skills(store, status=SK.CANDIDATE)
                  if r["id"] == sk["id"]]
    steps.append({"step": "inject_blocked",
                  "still_candidate": bool(injectable),
                  "note": "disabled 技能不参与注入检索"})

    # 回滚有两种正当结局：换回旧正文（rolled_back）或内容相同直接停用
    # （disabled）。两者都必须让技能脱离 active。
    ok = (res["degraded"] and (res["rolled_back"] or res.get("disabled"))
          and status_after != SK.ACTIVE and audits >= 1)
    store.close()
    # 不写绝对路径：产物要能被任何机器复现，路径属于开发机细节
    return {"ok": ok, "steps": steps}


def audit_production():
    """生产库只读巡检：对每个 active 技能算一次退化判定（dry_run）。"""
    db = os.path.join(ROOT, "data", "evoguard.db")
    if not os.path.isfile(db):
        return {"skipped": "no production db"}
    import core
    store = core.Store(db)
    rows = SK.list_skills(store, status=SK.ACTIVE) or []
    report = []
    for r in rows:
        try:
            res = SS.monitor_and_rollback(store, r["id"], dry_run=True)
            report.append({"skill_id": r["id"], "task_type": r.get("task_type"),
                           "degraded": res.get("degraded"),
                           "baseline": res.get("baseline"),
                           "online_n": (res.get("online") or {}).get("n")})
        except Exception as exc:                           # noqa: BLE001
            report.append({"skill_id": r["id"], "error":
                           "%s: %s" % (type(exc).__name__, str(exc)[:80])})
    store.close()
    return {"active_skills": len(rows), "checked": report}


def main():
    drill = run_drill()
    prod = audit_production()
    rep = {"drill": drill, "production_audit": prod}
    out = os.path.join(ROOT, "experiments", "rollback_drill.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(json.dumps({"drill_ok": drill["ok"],
                      "audit": {k: v for k, v in prod.items() if k != "checked"},
                      "degraded_skills": [c["skill_id"] for c in
                                          prod.get("checked", [])
                                          if c.get("degraded")]},
                     ensure_ascii=False))
    print("WROTE", os.path.relpath(out, ROOT))
    return 0 if drill["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
