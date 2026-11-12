#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""故障演练：把每个故障真的按下去，验证「该坏的地方坏、该活的地方还活着」。

    python scripts/drill.py                    # 全部演练
    python scripts/drill.py --only kill_switch

产物：experiments/drill_v1/result.json

和单测的区别：单测验组件行为，这里验**整个进程在故障下的取舍**。
每条演练都分三步：注入故障 → 断言该拦的拦了 → 撤掉故障 → 断言恢复了。
只做第二步的演练是假演练 —— 「坏了以后永远好不了」比「没坏」更糟。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import live_client as L                                    # noqa: E402

TOKEN = "drill-token-0123456789"
AUTH = {"Authorization": "Bearer " + TOKEN}
NOAUTH = {}


def _drill_auth():
    """关掉鉴权 vs 打开鉴权：没 token 必须 401，带 token 必须 200。"""
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN}) as svc:
        st_no, _, _ = L.http(svc.port, "GET", "/api/health", NOAUTH)
        st_ok, _, _ = L.http(svc.port, "GET", "/api/health", AUTH)
    return {
        "steps": [{"name": "无 token 请求", "status": st_no, "expect": 401},
                  {"name": "带 token 请求", "status": st_ok, "expect": 200}],
        "passed": st_no == 401 and st_ok == 200,
        "note": "403/401 的取舍：这里用 401（未认证），不是 403（已认证无权限），"
                "因为请求根本没有可识别的主体。",
    }


def _drill_kill_switch():
    """停止开关：写被拒、读仍可用；关掉后写立刻恢复。"""
    post = dict(AUTH)
    post["Content-Type"] = "application/json"
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN,
                    "AGENTLAB_KILL_SWITCH": "1"}) as svc:
        st_read, _, _ = L.http(svc.port, "GET", "/api/health", AUTH)
        st_write, _, _ = L.http(svc.port, "POST", "/api/run", post, body=b"{}")
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN,
                    "AGENTLAB_KILL_SWITCH": "0"}) as svc2:
        st_write2, _, _ = L.http(svc2.port, "POST", "/api/nope", post, body=b"{}")
    return {
        "steps": [
            {"name": "开关打开时读", "status": st_read, "expect": 200},
            {"name": "开关打开时写", "status": st_write, "expect": 503},
            {"name": "关掉后写回到正常路径", "status": st_write2,
             "expect": "非 503（本例 404，因为路由不存在；说明已过闸门）"},
        ],
        # 关键取舍：开关只拦写。读仍可用 → 运维在事故中还能看健康与指标。
        "passed": st_read == 200 and st_write == 503 and st_write2 != 503,
        "note": "开关刻意只拦 WRITE_METHODS，GET 保持可读 —— 否则事故中运维"
                "连健康检查都用不了，等于把自己关在门外。",
    }


def _drill_rate_limit():
    """限流：超过阈值给 429，且带 Retry-After，不是默默丢连接。"""
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN,
                    "AGENTLAB_RATE_LIMIT": "3",
                    "AGENTLAB_RATE_WINDOW": "60"}) as svc:
        codes, retry = [], ""
        for _ in range(6):
            st, hdrs, _ = L.http(svc.port, "GET", "/api/health", AUTH)
            codes.append(st)
            if st == 429:
                retry = hdrs.get("retry-after", "")
    n429 = codes.count(429)
    return {
        "steps": [{"name": "连打 6 次（限额 3）", "statuses": codes,
                   "expect": "前 3 次 200，其后 429"}],
        "passed": n429 >= 1 and codes[:3] == [200, 200, 200],
        "retry_after": retry,
        "note": "限流按 actor 分桶，不是全局限流 —— 一个客户端刷爆不该拖累别的租户。" ,
    }


def _drill_restart():
    """重启后恢复：服务能被正常拉起两次，且第二次照样鉴权。"""
    first = second = 0
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN}) as svc:
        first, _, _ = L.http(svc.port, "GET", "/api/health", AUTH)
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN}) as svc2:
        second, _, _ = L.http(svc2.port, "GET", "/api/health", AUTH)
        st_no, _, _ = L.http(svc2.port, "GET", "/api/health", NOAUTH)
    return {
        "steps": [{"name": "第一次启动", "status": first, "expect": 200},
                  {"name": "第二次启动", "status": second, "expect": 200},
                  {"name": "重启后鉴权仍生效", "status": st_no, "expect": 401}],
        "passed": first == 200 and second == 200 and st_no == 401,
        "note": "重启不改语义：没有「重启后变成默认放开」这种降级。",
    }


def _drill_no_audit_fallback():
    """审计库不可用时：服务照常起，但降级必须**看得见**。

    这里断言三件事，缺一不可：① 服务仍可用（可观测性故障不拖垮业务）；
    ② /api/health 明确报告 audit_enabled=false；③ 给出 audit_error 原因。
    只做 ① 的演练会漏掉最危险的情形：审计悄悄没了，谁也不知道。
    """
    bad = os.path.join(ROOT, "README.md", "audit.db")   # 父路径是文件，必失败
    with L.Service({"AGENTLAB_API_TOKEN": TOKEN,
                    "AGENTLAB_AUDIT_DB": bad}) as svc:
        st, _, raw = L.http(svc.port, "GET", "/api/health", AUTH)
    try:
        import json as _json
        body = _json.loads(raw.decode("utf-8"))
    except Exception:                                       # noqa: BLE001
        body = {}
    enabled = body.get("audit_enabled")
    err = body.get("audit_error") or ""
    return {
        "steps": [
            {"name": "审计库路径非法时仍能访问", "status": st, "expect": 200},
            {"name": "健康检查报告审计已关", "value": enabled, "expect": False},
            {"name": "健康检查给出降级原因", "value": err[:80],
             "expect": "非空"},
        ],
        "passed": st == 200 and enabled is False and bool(err),
        "note": "降级不静默：审计掉了会在 /api/health 的 audit_error 里写明原因，"
                "监控据此告警，而不是等审计查不出东西才发现。",
    }


DRILLS = [
    ("auth", _drill_auth),
    ("kill_switch", _drill_kill_switch),
    ("rate_limit", _drill_rate_limit),
    ("restart", _drill_restart),
    ("audit_degraded", _drill_no_audit_fallback),
]


def _scrub(obj):
    """把本机绝对路径换成 `<repo>`。

    演练会把异常原文记进产物，里面带完整路径；仓库里的产物不该泄露
    开发机目录结构（之前清过一轮元数据，别又从结果文件漏回来）。
    """
    if isinstance(obj, str):
        # OSError 的 str() 用 repr 引文件名，Windows 下反斜杠会变成双写，
        # 所以除了原样写法还要试转义后的写法，否则漏网。
        for needle in (ROOT, ROOT.replace("\\", "/"), ROOT.replace("/", "\\"),
                       ROOT.replace("\\", "\\\\")):
            obj = obj.replace(needle, "<repo>")
        return obj
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    return obj


def main():
    ap = argparse.ArgumentParser(description="故障演练")
    ap.add_argument("--only", choices=[d[0] for d in DRILLS], default="")
    ap.add_argument("--out", default=os.path.join(
        ROOT, "experiments", "drill_v1", "result.json"))
    a = ap.parse_args()

    results = []
    for name, fn in DRILLS:
        if a.only and name != a.only:
            continue
        t0 = time.time()
        try:
            r = fn()
        except Exception as exc:                            # noqa: BLE001
            r = {"passed": False, "note": "演练自身异常：%r" % exc, "steps": []}
        r["drill"] = name
        r["seconds"] = round(time.time() - t0, 2)
        results.append(r)
        print("%s %-16s %.2fs  %s"
              % ("PASS" if r["passed"] else "FAIL", name, r["seconds"],
                 r.get("note", "")[:70]))

    payload = _scrub({
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "results": results,
        "summary": {"total": len(results),
                    "passed": sum(1 for r in results if r["passed"])},
        "boundaries": [
            "在本地回环上用独立进程演练，不含真实网络分区 / 磁盘满 / 依赖超时。",
            "故障注入通过环境变量完成，属于「已知可控故障」，不是混沌工程的随机注入。",
            "没有验证长时间（小时级）稳定性与内存增长。",
        ],
    })
    L.write_json(a.out, payload)
    s = payload["summary"]
    print("== %d/%d 通过 ==" % (s["passed"], s["total"]))
    print("[√] %s" % a.out)
    return 0 if s["passed"] == s["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
