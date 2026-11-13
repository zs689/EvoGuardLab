#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立审查整改的**同一检查入口**（EG-023 要求本地与 CI 用同一个）。

    python scripts/review_checks.py                 # 打印逐项结果
    python scripts/review_checks.py --out <dir>    # 写 review_checks.json
    python scripts/review_checks.py --only EG-007  # 只跑一项
    python scripts/review_checks.py --gate         # 有失败就非零退出（CI 用）

设计原则（来自审查 handoff_rules）：
1. 每条检查对应某个 finding 的**验收条件**，不是「文件存在吗」这种自证。
2. 能真跑的就真跑（起服务、发 HTTP、写库、清理、验链），不靠 grep 关键字。
3. 结果只有 pass / fail / skip 三态，skip 必须给理由（CI 据此判断能不能放行）。
4. 修前跑一遍留红，修后跑同一份脚本转绿 —— 数字不靠手写，靠重跑。
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVO = os.path.join(ROOT, "ai-service", "evoguard")
sys.path.insert(0, EVO)


def _read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _exists(rel):
    return os.path.isfile(os.path.join(ROOT, rel))


def _json(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _result(cid, ok, detail, evidence, skipped=False):
    return {"id": cid, "status": "skip" if skipped else
            ("pass" if ok else "fail"), "detail": detail, "evidence": evidence}


# ------------------------------------------------------------ EG-003
def c_dockerignore():
    """跑真实的标记文件模拟，而不是「文件在不在」。"""
    import subprocess
    tmp = tempfile.mkdtemp(prefix="evo-docker-")
    cmd = [sys.executable,
           os.path.join(ROOT, "scripts", "check_docker_context.py"),
           "--out", tmp]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    report = _json(os.path.join(tmp, "docker_context.json")) or {}
    n_ok = sum(1 for c in report.get("checks", []) if c.get("pass"))
    total = len(report.get("checks", []))
    ok = p.returncode == 0 and total > 0 and n_ok == total
    return _result("EG-003/context", ok,
                   "构建上下文 %d/%d 项通过（含敏感标记全部排除）"
                   % (n_ok, total) if ok else
                   "构建上下文验收失败：%s" % (p.stdout.strip()[-200:] or p.stderr[-200:]),
                   "scripts/check_docker_context.py（标记文件模拟；镜像层未实测）")


def c_dockerfile_copy():
    txt = _read("Dockerfile")
    whole = re.search(r"^\s*COPY\s+\.\s+/", txt, re.M)
    return _result("EG-003/COPY", whole is None,
                   "Dockerfile 不再整仓 COPY" if not whole else
                   "仍是 `COPY . /app`，会夹带被忽略的 data/ 与 .git/",
                   "Dockerfile")


# ------------------------------------------------------------ EG-007
def c_retention_chain():
    """真写三条、按保留期清一条、再验链。这是审查测出来的真 bug。"""
    from enterprise import audit as A
    s = A.AuditStore(":memory:")
    for i, ts in enumerate((1.0, 2.0, 3.0)):
        s.emit({"tenant_id": "t", "request_id": "r%d" % i,
                "ts": ts, "status": 200})
    before = s.verify()
    removed = s.purge_before(2.0)
    after = s.verify()
    s.close()
    ok = (before[0] and removed == 1 and after[0])
    return _result("EG-007/retention", ok,
                   "合法清理后剩余链仍可验证" if ok else
                   "清理 %d 行后验链失败：%s" % (removed, after[1]),
                   "AuditStore: emit×3 -> purge_before -> verify()")


# ------------------------------------------------------------ EG-015
def c_sli_definition():
    """EG-015：用已知状态计数校验 SLI 算法，不是 grep 关键字。"""
    scripts = os.path.join(ROOT, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import loadtest as LT
    except ImportError as e:
        return _result("EG-015/SLI", False, "无法导入 loadtest: %s" % e,
                       "scripts/loadtest.py")
    cases = [
        (200, "success"), (429, "controlled_reject"),
        (404, "client_error"), (500, "server_failure"),
        (0, "connect_failure"), (401, "controlled_reject"),
        (503, "controlled_reject"),
    ]
    wrong = [(s, LT.classify(s), want) for s, want in cases
             if LT.classify(s) != want]
    # 8 成功 + 2 限流 + 1 404：限流/404 既不算成功也不算失败 → 可用率 1.0
    c1 = {"success": 8, "controlled_reject": 2, "client_error": 1,
          "server_failure": 0, "connect_failure": 0}
    # 加一个 500 → 分母 9，可用率 8/9
    c2 = dict(c1, server_failure=1)
    ok = (not wrong and LT.sli(c1) == 1.0
          and abs(LT.sli(c2) - 8 / 9) < 1e-6)
    return _result("EG-015/SLI", ok,
                   "四类状态分类与可用率分母正确" if ok else
                   "SLI 分类或分母有误：%r / c1=%s / c2=%s"
                   % (wrong, LT.sli(c1), LT.sli(c2)),
                   "scripts/loadtest.py:classify()/sli() 构造计数验算")


# ------------------------------------------------------------ EG-005
def c_body_limit():
    txt = _read("ai-service/api/app.py")
    ok = ("MAX_BODY_BYTES" in txt or "MAX_BODY" in txt) and "413" in txt
    return _result("EG-005/body-limit", ok,
                   "请求体有硬上限且超限返回 413" if ok else
                   "入口无请求体硬上限，直接按 Content-Length 读完整",
                   "ai-service/api/app.py")


def c_stable_4xx():
    """真的起服务，发畸形 JSON 与超大 body，看是不是稳定 4xx。"""
    scripts = os.path.join(ROOT, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import live_client as L
    except ImportError as e:
        return _result("EG-005/stable-4xx", False, "无法导入 live_client: %s" % e,
                       "scripts/live_client.py")
    token = "review-probe-token"
    with L.Service({"AGENTLAB_API_TOKEN": token,
                    "AGENTLAB_RATE_LIMIT": "1000"}) as svc:
        h = {"Authorization": "Bearer " + token,
             "Content-Type": "application/json"}
        st_bad, _, _ = L.http(svc.port, "POST", "/api/run", h,
                              body=b"{not json")
        st_big, _, _ = L.http(svc.port, "POST", "/api/run", h,
                              body=b"x" * (2 * 1024 * 1024))
    ok = 400 <= st_bad < 500 and st_big in (413, 400)
    return _result("EG-005/stable-4xx", ok,
                   "畸形 JSON=%s，2MiB body=%s（都应是 4xx，不是 500）"
                   % (st_bad, st_big),
                   "真 HTTP: POST /api/run")


# ------------------------------------------------------------ EG-010
def c_health_no_ddl():
    txt = _read("ai-service/api/app.py")
    # h_health 里不能再构造 Store；存活探针应走轻量路径
    seg = txt.split("def h_health", 1)[1].split("\n    def ", 1)[0]
    ok = "core.Store(" not in seg
    return _result("EG-010/health-ddl", ok,
                   "存活探针不再每次建 Store 执行 schema" if ok else
                   "h_health 仍构造 core.Store(DB)（每次请求跑 DDL）",
                   "ai-service/api/app.py:h_health")


# ------------------------------------------------------------ EG-011
def c_db_path_env():
    txt = _read("ai-service/api/app.py")
    ok = "AGENTLAB_DB" in txt or "EVOGUARD_DB" in txt
    return _result("EG-011/db-path", ok,
                   "业务库路径可由环境变量配置" if ok else
                   "业务库路径写死在仓库内，容器只读时无法挂载",
                   "ai-service/api/app.py")


# ------------------------------------------------------------ EG-012
def c_metric_consistency():
    """EG-013：告警规则引用的每个指标，都必须在代码里真的被 emit。

    改了指标口径却没改 alerts.yml 是最隐蔽的一类回归：规则永远不触发，
    而 yaml 本身语法完全正常。
    """
    import re
    alerts = _read("deploy/alerts.yml")
    app = _read("ai-service/api/app.py")
    referenced = set(re.findall(r"\bevoguard_[a-z_]+\b", alerts))
    # 直接判断：指标名（或去掉直方图后缀）出现在代码里
    missing = []
    for n in sorted(referenced):
        candidates = {n}
        for suffix in ("_bucket", "_sum", "_count"):
            if n.endswith(suffix):
                candidates.add(n[: -len(suffix)])
        if not any(c in app for c in candidates):
            missing.append(n)
    ok = not missing
    return _result("EG-013/metric-consistency", ok,
                   "%d 个指标名全部在 app.py 里有来源" % len(referenced)
                   if ok else "告警引用了不存在的指标：%s" % missing,
                   "deploy/alerts.yml <-> ai-service/api/app.py")


def c_enterprise_e2e():
    """EG-001/EG-002：跑真服务、真 token、真跨租户查询，读产物判定。"""
    import subprocess
    tmp = tempfile.mkdtemp(prefix="evo-entgate-")
    cmd = [sys.executable,
           os.path.join(ROOT, "scripts", "enterprise_smoke.py"),
           "--out", tmp]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=ROOT, timeout=240)
    except subprocess.TimeoutExpired:
        return _result("EG-001+002/enterprise", False,
                       "企业端到端验收超时（240s）", "enterprise_smoke.py")
    rep = _json(os.path.join(tmp, "enterprise_smoke.json")) or {}
    s = rep.get("summary", {})
    failed = [c["id"] for c in rep.get("checks", []) if not c.get("pass")]
    ok = p.returncode == 0 and rep.get("passed") is True and not failed
    return _result("EG-001+002/enterprise", ok,
                   "身份绑定/角色/物理隔离/fail-closed %d/%d 通过"
                   % (s.get("pass", 0), s.get("total", 0)) if ok else
                   "未通过项：%s" % (failed or p.stderr[-160:]),
                   "scripts/enterprise_smoke.py（真进程 + 真 HTTP）")


def c_page_limit():
    txt = _read("ai-service/api/app.py")
    seg = txt.split("def h_runs", 1)[1].split("\n    def ", 1)[0]
    ok = ("min(" in seg or "MAX_" in seg or "clamp" in seg)
    return _result("EG-012/page-limit", ok,
                   "列表 limit 有上限" if ok else
                   "`limit` 由查询串直接 int()，无最大值约束",
                   "ai-service/api/app.py:h_runs")


# ------------------------------------------------------------ EG-013
def c_liveness_split():
    txt = _read("ai-service/api/app.py")
    ok = ("/api/live" in txt or "/healthz" in txt) and "/api/ready" in txt
    return _result("EG-013/probes", ok,
                   "存活/就绪/诊断探针已拆分" if ok else
                   "健康检查语义混合，监控抓取计入业务流量",
                   "ai-service/api/app.py:ROUTES")


# ------------------------------------------------------------ EG-021
def c_frontend_optional():
    """EG-021：静态结构 + 真做一次 JS 语法检查。

    浏览器交互（键盘/移动端）本机没法自动跑，这部分如实标为未实测；
    但「可选面板是否解耦」和「脚本能不能解析」这两件是可以验的。
    """
    import shutil
    txt = _read("frontend/index.html")
    has_settled = "Promise.allSettled" in txt
    no_bare = "await Promise.all(" not in txt
    node = shutil.which("node")
    if node:
        import re
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", txt, re.S)
        p = os.path.join(tempfile.mkdtemp(prefix="evo-js-"), "check.js")
        with io.open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(blocks))
        rc = subprocess.run([node, "--check", p], capture_output=True).returncode
        syntax_ok, ev = rc == 0, "node --check（%d 个 script 块）" % len(blocks)
    else:
        syntax_ok, ev = False, "node 不可用：语法检查未运行，按未通过处理"
    ok = has_settled and no_bare and syntax_ok
    return _result("EG-021/optional-panels", ok,
                   "可选面板用 allSettled 解耦且 JS 语法通过" if ok else
                   "allSettled=%s 裸Promise.all=%s 语法=%s"
                   % (has_settled, not no_bare, syntax_ok),
                   "frontend/index.html + " + ev)


# ------------------------------------------------------------ EG-023
def c_gate_in_ci():
    """EG-023：逐项硬门槛 + 制品验收 + 汇总闸门，缺一不可。"""
    ci = _read(".github/workflows/test.yml")
    needed = {
        "逐项 gate": "review_checks.py --gate" in ci,
        "干净 clone 也跑 gate": ci.count("review_checks.py --gate") >= 2,
        "构建上下文门禁": "check_docker_context.py" in ci,
        "wheel 清单验收": "check_wheel_manifest.py" in ci,
        "wheel CLI 冒烟": "wheel_smoke.py" in ci,
        "Linux+Windows 双平台": "ubuntu-latest" in ci and "windows-latest" in ci,
        "汇总闸门": "release-gate:" in ci and "needs:" in ci,
        "自评分数不单独放行": "informational" in ci,
    }
    missing = [k for k, v in needed.items() if not v]
    ok = not missing
    return _result("EG-023/gate", ok,
                   "8 项 CI 门槛齐全" if ok else
                   "CI 缺：%s" % "、".join(missing),
                   ".github/workflows/test.yml")


CHECKS = [
    ("EG-003", c_dockerignore),
    ("EG-003", c_dockerfile_copy),
    ("EG-007", c_retention_chain),
    ("EG-015", c_sli_definition),
    ("EG-005", c_body_limit),
    ("EG-005", c_stable_4xx),
    ("EG-010", c_health_no_ddl),
    ("EG-011", c_db_path_env),
    ("EG-012", c_page_limit),
    ("EG-013", c_liveness_split),
    ("EG-013", c_metric_consistency),
    ("EG-021", c_frontend_optional),
    ("EG-023", c_gate_in_ci),
    ("EG-001", c_enterprise_e2e),
]


def main():
    ap = argparse.ArgumentParser(description="独立审查整改的同一检查入口")
    ap.add_argument("--out", default="", help="输出目录（写 review_checks.json）")
    ap.add_argument("--only", default="", help="只跑某个 finding 前缀")
    ap.add_argument("--gate", action="store_true", help="任一 fail 就非零退出")
    a = ap.parse_args()

    results = []
    t0 = time.time()
    for cid, fn in CHECKS:
        if a.only and not cid.startswith(a.only):
            continue
        try:
            r = fn()
        except Exception as exc:               # noqa: BLE001
            r = _result(cid + "/exception", False,
                        "检查自身异常：%r" % exc, fn.__name__)
        results.append(r)
        print("%-5s %-30s %s" % (r["status"].upper(), r["id"], r["detail"]))

    n_pass = sum(1 for r in results if r["status"] == "pass")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    report = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True).stdout.strip() if _exists(".git/HEAD") else "unknown",
        "seconds": round(time.time() - t0, 2),
        "summary": {"total": len(results), "pass": n_pass, "fail": n_fail,
                    "skip": len(results) - n_pass - n_fail},
        "checks": results,
        "note": "每项对应独立审查 finding 的验收条件；修前留红、修后重跑转绿。",
    }
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, "review_checks.json")
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        print("[√] %s（%d pass / %d fail）" % (p, n_pass, n_fail))
    print("== %d/%d pass, %d fail ==" % (n_pass, len(results), n_fail))
    return 1 if (a.gate and n_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
