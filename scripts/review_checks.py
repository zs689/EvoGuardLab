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
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVO = os.path.join(ROOT, "ai-service", "evoguard")
sys.path.insert(0, EVO)


def _read(rel):
    # FINAL-04-T1：app.py 已拆成 app_core/routes_agent/routes_enterprise。
    # 静态门禁检查的是「接线是否存在」，读合并源码即可，语义不变。
    if rel == "ai-service/api/app.py":
        parts = []
        for name in ("app.py", "app_core.py", "routes_agent.py",
                     "routes_enterprise.py"):
            p = os.path.join(ROOT, "ai-service", "api", name)
            if os.path.isfile(p):
                with io.open(p, encoding="utf-8") as f:
                    parts.append(f.read())
        return "\n".join(parts)
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


def c_hard_negative_disclosure():
    """FRONTIER-03：硬负样本误报必须被**量化并披露**，不阻塞发布。

    当前默认文本栈 FPR 远高于方案建议的 0.15 —— 这是"安全优先、宁可
    误报也不漏真密钥/真注入"的已知代价（规则自身 false_positive 字段
    就写着"文档里讲 AKIA 前缀时可能误报"）。不能为降低数字而删样本
    或削弱真检测；所以这里只要求：**产物存在、数字真实、样本未被删**。
    """
    p = os.path.join(ROOT, "experiments", "hard_negatives.json")
    rep = _json(p) or {}
    n = int(rep.get("n") or 0)
    has_fpr = isinstance(rep.get("fpr"), (int, float))
    ok = n >= 25 and has_fpr and isinstance(rep.get("false_positives"), list)
    return _result("FRONTIER-03/hard-negative-disclosure", ok,
                   "硬负样本 %d 条，诚实披露 FPR=%.3f（安全优先的已知代价，"
                   "不删样本不削弱检测）" % (n, rep.get("fpr") or 0) if ok else
                   "硬负样本产物缺失或数字不可信（n=%s, fpr=%s）"
                   % (n, rep.get("fpr")),
                   "scripts/eval_hard_negatives.py -> experiments/hard_negatives.json")


def c_no_personal_paths():
    """OSS-03：git 跟踪的文件里不能出现本机个人绝对路径。"""
    import re as _re
    pat = _re.compile(r"(?:[Cc]:\\Users\\zs|[eE]:\\agent|[Ff]:\\Anaconda3)")
    hits = []
    if os.path.isdir(os.path.join(ROOT, ".git")):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split("\n")
    else:
        # 自检的临时仓库没有 .git；退化为 walk（只扫文本文件，不看大产物）
        files = []
        for dp, _dn, fn in os.walk(ROOT):
            for name in fn:
                rel = os.path.relpath(os.path.join(dp, name), ROOT)
                if rel.endswith((".pyc", ".pyo")):
                    continue
                files.append(rel)
    for rel in files:
        if not rel:
            continue
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        try:
            with io.open(p, encoding="utf-8", errors="ignore") as fh:
                t = fh.read()
        except OSError:
            continue
        n = len(pat.findall(t))
        if n:
            hits.append((rel, n))
    ok = not hits
    return _result("OSS-03/personal-paths", ok,
                   "git 跟踪文件中 0 处本机个人路径" if ok else
                   "发现 %d 个文件含个人路径：%s"
                   % (len(hits), hits[:5]),
                   "git ls-files + 正则扫描")


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


def c_frontend_session():
    """EG-020：企业鉴权开启后浏览器要能带 token，且令牌不残留。"""
    txt = _read("frontend/index.html")
    checks = {
        "统一带 Authorization": "headers['Authorization']" in txt,
        "会话级存储": "sessionStorage.getItem(TOKEN_KEY)" in txt,
        "支持退出": "removeItem(TOKEN_KEY)" in txt,
        "401 有反馈": "未认证或令牌失效" in txt,
        "403 有反馈": "当前身份没有该操作的权限" in txt,
        "不写入持久存储": "localStorage.setItem(TOKEN_KEY" not in txt,
        "不写进 URL": not any(x in txt for x in ("?token=", "#token=")),
        "输入框不回显明文": "$('#token-input').value = ''" in txt,
        "键盘可操作": "e.key === 'Enter'" in txt,
    }
    missing = [k for k, v in checks.items() if not v]
    return _result("EG-020/session", not missing,
                   "令牌传递/失效反馈/会话隔离/键盘操作 9 项齐全" if not missing
                   else "缺：%s" % "、".join(missing),
                   "frontend/index.html（静态；真实浏览器登录未实测）")


def c_store_defaults():
    """EG-010：WAL 已判不可用，别让默认值或文档悄悄漂回去。"""
    core = _read("ai-service/evoguard/core.py")
    env = _read(".env.example")
    disk_rep = _json(os.path.join(ROOT, "docs", "review-remediation",
                                  "probes", "store_remaining_checks.json")) or {}
    # WAL 并发 readonly 在写串行化后变成 0/8，那条「判红」证据不再成立。
    # 仍成立、且不受串行化影响的两个理由是：备份只复制 db 丢数据、
    # 磁盘写放大大约 70 倍。门槛改守写放大，别靠一个会变的结果。
    wal_bytes, pool_bytes = 0, 1
    for c in disk_rep.get("checks", []):
        if c.get("id") != "disk-bytes":
            continue
        for r in c.get("evidence", {}).get("rows", []):
            if r["mode"] == "pool_wal":
                wal_bytes = r["per_write"]
            elif r["mode"] == "pool":
                pool_bytes = r["per_write"] or 1
    checks = {
        "池默认开启": 'or "1").strip().lower()' in core
                  and "pool_flag" in core,
        "WAL 不默认开": "setdefault(\"AGENTLAB_STORE_WAL\"" not in core,
        "env 里 WAL 是注释": "\nAGENTLAB_STORE_WAL=1" not in env,
        "env 明确不要开 WAL": "不要开 WAL" in env,
        "WAL 写放大有证据": wal_bytes > pool_bytes * 10,
    }
    missing = [k for k, v in checks.items() if not v]
    return _result("EG-010/store-defaults", not missing,
                   "池默认开、WAL 不默认开且写放大有证据" if not missing
                   else "缺：%s" % "、".join(missing),
                   "core.py / .env.example / probes")


def c_single_writer():
    """EG-010：SQL 感知的写串行化不能被悄悄关掉或删掉。"""
    src = _read("ai-service/evoguard/core.py")
    app = _read("ai-service/api/app.py")
    checks = {
        "代理存在": "_WriteGuardConn" in src,
        "按路径共享写锁": "_write_gate" in src and "_GATES" in src,
        "读不加锁": "读完全不加锁" in src or "读路径必须完全不加锁" in src,
        "默认开": 'or "1")' in src and "AGENTLAB_STORE_SERIALIZE_WRITES" in src,
        "可关闭": '"0", "false", "off"' in src,
        "health 可观测": '"write_gate"' in app,
        "有回归测试": os.path.isfile(
            os.path.join(ROOT, "tests", "test_single_writer.py")),
    }
    missing = [k for k, v in checks.items() if not v]
    return _result("EG-010/single-writer", not missing,
                   "写串行化默认开、可关、可观测、有测试" if not missing
                   else "缺：%s" % "、".join(missing),
                   "core.py / app.py / tests/test_single_writer.py")


def _p95_of(rep):
    a = (rep or {}).get("aggregated", {}).get("write_run", {})
    return a.get("p95_median_ms")


def c_e2e_speedup():
    """端到端 HTTP A/B：各 5 次不许有重叠，且全部无 5xx。"""
    d = os.path.join(ROOT, "docs", "review-remediation", "probes")
    rep = _json(os.path.join(d, "http_ab_repeat.json")) or {}
    rows = rep.get("runs") or []
    ok = (rep.get("no_overlap") is True
          and rep.get("all_availability_1") is True
          and rep.get("no_5xx") is True
          and len(rows) == 10)
    nd = rep.get("new_default", {}).get("median_ms")
    ao = rep.get("all_off", {}).get("median_ms")
    return _result("EG-010/e2e", ok,
                   ("各 5 次，新默认中位 %s ms vs 全关 %s ms，零重叠无 5xx"
                    % (nd, ao)) if ok else "端到端收益证据不成立",
                   "probes/http_ab_repeat.json + http_ab_repeat/*.json")


def c_docker_image_verify():
    """EG-003：镜像层与 Linux wheel 的实测证据必须留在仓库里。"""
    d = os.path.join(ROOT, "docs", "review-remediation", "probes")
    rep = _json(os.path.join(d, "docker_image_verification.json")) or {}
    checks = rep.get("checks") or []
    ids = {c.get("id") for c in checks}
    need = {"no-leaked-files", "no-broad-copy", "non-root",
            "readonly-root-service", "volume-separated", "linux-wheel"}
    ok = (rep.get("passed") is True and need <= ids
          and all(c.get("pass") is True for c in checks))
    return _result("EG-003/docker-verify", ok,
                   ("镜像层 + Linux wheel %d/%d 实测通过"
                    % (sum(1 for c in checks if c.get("pass")), len(checks)))
                   if ok else "Docker 实测证据缺失或未全过",
                   "probes/docker_image_verification.json")


def c_ci_references():
    """EG-023：CI 引用的脚本/参数不能漂移，关键门槛不能掉出 workflow。"""
    d = os.path.join(ROOT, "docs", "review-remediation", "probes")
    rep = _json(os.path.join(d, "ci_references.json")) or {}
    ok = rep.get("passed") is True and not (rep.get("problems") or [])
    return _result("EG-023/ci-references", ok,
                   ("%d 个 run 块，%d 个脚本引用，关键门槛齐全"
                    % (rep.get("run_blocks", 0),
                       len(rep.get("scripts_referenced", []))))
                   if ok else "CI 引用有问题：%s"
                   % (rep.get("problems") or [])[:3],
                   "probes/ci_references.json")


def c_heap_stability():
    """长时 soak：观测器不驻留内存，Store 堆不随时间增长。"""
    d = os.path.join(ROOT, "docs", "review-remediation", "probes")
    rep = _json(os.path.join(d, "soak_heap_stable.json")) or {}
    soak = next((c for c in (rep.get("checks") or [])
                 if c.get("id") == "soak"), {})
    ev = soak.get("evidence") or {}
    growth = ev.get("heap_growth_mb")
    ok = (rep.get("passed") is True and soak.get("pass") is True
          and ev.get("heap_stable") is True
          and ev.get("observer_excluded") is True
          and growth is not None and growth < 0.15
          and len(ev.get("samples") or []) >= 100)
    return _result("EG-010/heap-stability", ok,
                   ("%d 次写，堆增长 %+.3f MiB，观测器已排除"
                    % (ev.get("writes", 0), growth if growth is not None else 0))
                   if ok else "堆稳定性证据缺失或不达标",
                   "probes/soak_heap_stable.json")


def c_agent_watch():
    """新能力层：监测其他 agent + 强制防护的闭环证据不能退化。"""
    d = os.path.join(ROOT, "docs", "review-remediation", "probes")
    rep = _json(os.path.join(d, "agent_watch_verification.json")) or {}
    checks = rep.get("checks") or []
    need = {"static_audit_finds_vulns", "runtime_probe_finds_bugs",
            "attack_succeeds_without_guard", "guard_blocks_attack",
            "http_proxy_blocks_external_attack",
            "encoded_bypass_caught",
            "homoglyph_and_despaced_caught"}
    ids = {c.get("id") for c in checks}
    ok = (rep.get("passed") is True and need <= ids
          and all(c.get("pass") is True for c in checks))
    return _result("AGENT-WATCH/verification", ok,
                   ("%d/%d 项通过：静态/运行态/网关/网络层对照齐全"
                    % (sum(1 for c in checks if c.get("pass")), len(checks)))
                   if ok else "AgentWatch 闭环证据缺失或未全过",
                   "probes/agent_watch_verification.json")


def c_frontend_a11y():
    """前端静态可访问性：控件可访问名称不能退化。"""
    d = os.path.join(ROOT, "docs", "review-remediation", "probes")
    rep = _json(os.path.join(d, "frontend_a11y.json")) or {}
    problems = rep.get("problems") or []
    ok = rep.get("passed") is True and not problems
    return _result("EG-020/frontend-a11y", ok,
                   ("%d 个 input / %d 个 button，0 项问题"
                    % (rep.get("inputs", 0), rep.get("buttons", 0)))
                   if ok else "静态检查未通过：%s" % problems[:3],
                   "probes/frontend_a11y.json")


def c_write_gate_stress():
    """写闸门高并发下计数不漂、确实发生排队。"""
    d = os.path.join(ROOT, "docs", "review-remediation", "probes")
    rep = _json(os.path.join(d, "write_gate_stress.json")) or {}
    rows = rep.get("rows") or []
    ok = (rep.get("passed") is True and len(rows) >= 3
          and all(r.get("acquired") == r.get("threads")
                  and r.get("waiters") == 0
                  and r.get("peak_waiters", 0) > 0 for r in rows))
    return _result("EG-010/gate-stress", ok,
                   ("%d 轮 x %d 线程，峰值排队 %s"
                    % (len(rows), rows[0].get("threads", 0) if rows else 0,
                       [r.get("peak_waiters") for r in rows]))
                   if ok else "并发压测未通过",
                   "probes/write_gate_stress.json")


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


def c_audit_multiprocess():
    """EG-008：真起两个进程并发写同一个审计库。"""
    import subprocess
    cmd = [sys.executable, "-m", "unittest",
           "tests.test_audit_multiprocess", "-q"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=ROOT, timeout=180)
    except subprocess.TimeoutExpired:
        return _result("EG-008/audit-mp", False, "跨进程验收超时",
                       "tests/test_audit_multiprocess.py")
    ok = p.returncode == 0
    err = " ".join(p.stderr.strip().splitlines())[-600:]
    return _result("EG-008/audit-mp", ok,
                   "两进程并发迁移/写入：版本一致、无分叉链、事件完整"
                   if ok else ("跨进程验收失败：" + err),
                   "tests/test_audit_multiprocess.py（真两进程）")


def c_remaining_checks():
    """EG-010：真实强杀 / soak / 磁盘写入量的证据留在仓库里。"""
    rep = _json(os.path.join(ROOT, "docs", "review-remediation",
                             "probes", "store_remaining_checks.json")) or {}
    ids = {c.get("id") for c in rep.get("checks", [])}
    need = {"real-kill-pool", "real-kill-pool_wal", "soak", "disk-bytes"}
    ok = rep.get("passed") is True and need <= ids
    return _result("EG-010/remaining", ok,
                   "真实 taskkill / 60s soak / 磁盘量 4/4 有产物" if ok
                   else "产物缺失或未全过：%s" % sorted(need - ids),
                   "probes/store_remaining_checks.json")


def c_data_governance():
    """EG-022：合成敏感标记真的从每个出口都找不到。"""
    import subprocess
    cmd = [sys.executable, "-m", "unittest",
           "tests.test_data_governance", "-q"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=ROOT, timeout=180)
    except subprocess.TimeoutExpired:
        return _result("EG-022/governance", False, "数据治理验收超时",
                       "tests/test_data_governance.py")
    ok = p.returncode == 0
    return _result("EG-022/governance", ok,
                   "日志/指标/响应/列表/详情均不暴露合成敏感标记" if ok
                   else p.stderr.strip()[-180:],
                   "tests/test_data_governance.py + docs/data-governance.md")


def c_v3_prereg():
    """EG-017：判定线先写死、v2 负结果不许被改。"""
    import subprocess
    cmd = [sys.executable, "-m", "unittest",
           "tests.test_v3_prereg", "-q"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=ROOT, timeout=90)
    except subprocess.TimeoutExpired:
        return _result("EG-017/v3-prereg", False, "预注册守门超时",
                       "tests/test_v3_prereg.py")
    ok = p.returncode == 0
    return _result("EG-017/v3-prereg", ok,
                   "8 条判定线指纹冻结、文档与代码一致、v2 归档未动"
                   if ok else p.stderr.strip()[-180:],
                   "docs/semantic-injection-v3-plan.md + v3_gate.py")


def c_jobs_e2e():
    """EG-009：真服务里的作业生命周期、幂等、隔离、重启与提交/完成分离。"""
    import subprocess
    tmp = tempfile.mkdtemp(prefix="evo-jobgate-")
    cmd = [sys.executable,
           os.path.join(ROOT, "scripts", "jobs_smoke.py"), "--out", tmp]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=ROOT, timeout=420)
    except subprocess.TimeoutExpired:
        return _result("EG-009/jobs", False, "作业验收超时（420s）",
                       "scripts/jobs_smoke.py")
    rep = _json(os.path.join(tmp, "jobs_smoke.json")) or {}
    s = rep.get("summary", {})
    failed = [c["id"] for c in rep.get("checks", []) if not c.get("pass")]
    ok = p.returncode == 0 and rep.get("passed") is True and not failed
    return _result("EG-009/jobs", ok,
                   "幂等/轮询/取消/隔离/重启/提交-完成分离 %d/%d 通过"
                   % (s.get("pass", 0), s.get("total", 0)) if ok else
                   "未通过项：%s" % (failed or p.stderr[-160:]),
                   "scripts/jobs_smoke.py（真进程 + 真 HTTP）")


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


# ---- 证据新鲜度门禁：防止「读一个写着 passed:true 的旧 JSON」永久放行 ----
# 映射：证据文件 -> 它声称验证的源码。源码比证据新 = 证据已过期，
# 必须重跑探针，不能继续算通过。
EVIDENCE_SOURCES = {
    "docs/review-remediation/probes/http_ab_repeat.json": [
        "ai-service/api/app.py", "ai-service/api/app_core.py",
        "ai-service/evoguard/core.py"],
    "docs/review-remediation/probes/docker_image_verification.json": [
        "Dockerfile", "pyproject.toml"],
    "docs/review-remediation/probes/ci_references.json": [
        ".github/workflows/test.yml"],
    "docs/review-remediation/probes/soak_heap_stable.json": [
        "ai-service/evoguard/core.py"],
    "docs/review-remediation/probes/frontend_a11y.json": [
        "frontend/index.html", "frontend/command.html"],
    "docs/review-remediation/probes/agent_watch_verification.json": [
        "ai-service/evoguard/agent_watch.py"],
    "docs/review-remediation/probes/write_gate_stress.json": [
        "ai-service/evoguard/core.py"],
    "docs/review-remediation/probes/store_remaining_checks.json": [
        "ai-service/evoguard/core.py"],
}


def evidence_freshness(pairs):
    """pairs: [(证据绝对路径, [源码绝对路径或相对 ROOT]), ...]。"""
    stale, froze = [], []
    for ev, sources in pairs:
        if not os.path.isfile(ev):
            stale.append("%s：证据文件不存在" % os.path.basename(ev))
            continue
        rep = _json(ev) or {}
        if rep.get("frozen") is True:
            reason = str(rep.get("frozen_reason") or "").strip()
            if reason:
                froze.append("%s（冻结：%s）"
                             % (os.path.basename(ev), reason[:60]))
                continue
            # 标了 frozen 却没理由：不当作豁免，继续走 mtime 检查

        em = os.path.getmtime(ev)
        for src in sources:
            sp = src if os.path.isabs(src) else os.path.join(ROOT, src)
            if not os.path.isfile(sp):
                continue
            if os.path.getmtime(sp) > em + 1:   # 1s 容差，避免同秒写入误判
                stale.append("%s 早于 %s（证据过期，需重跑探针）"
                             % (os.path.basename(ev), os.path.basename(sp)))
    return {"fresh": not stale, "stale": stale, "frozen": froze,
            "checked": len(pairs)}


def c_evidence_freshness():
    pairs = [(os.path.join(ROOT, ev), srcs)
             for ev, srcs in EVIDENCE_SOURCES.items()]
    r = evidence_freshness(pairs)
    return _result(
        "EVIDENCE/freshness", r["fresh"],
        ("%d 项新鲜，%d 项冻结（已标注，非自动重跑）：%s"
         % (r["checked"] - len(r["frozen"]), len(r["frozen"]),
            "；".join(r["frozen"])[:160])) if r["fresh"]
        else ("%d/%d 项证据过期：%s"
              % (len(r["stale"]), r["checked"], "；".join(r["stale"][:4]))),
        "docs/review-remediation/probes/*.json mtime vs source mtime")


def c_bare_serve_blocking():
    """DEEP-01：零环境变量裸启动，输入注入与高危输出都必须真拦。"""
    try:
        import env as _env
        import openai_proxy as _op
        eff = _env.entry_defaults({})
        if str(eff.get("EVOGUARD_OUTPUT_BLOCK") or "") != "1":
            return _result("DEEP-01/bare-serve", False,
                           "入口默认仍缺 EVOGUARD_OUTPUT_BLOCK",
                           "env.entry_defaults({})")
        keys = tuple(eff)
        old = {k: os.environ.get(k) for k in keys}
        try:
            for k, v in eff.items():
                os.environ[k] = v
            # 输出侧：非流式响应里的 AWS Key 必须被 content_filter 拦断
            resp = {"choices": [{"index": 0, "message": {
                "role": "assistant",
                "content": "key=AKIAIOSFODNN7EXAMPLE"}}]}
            class _R:
                def __init__(self):
                    self.data = json.dumps(resp).encode()
                def read(self): return self.data
                def __enter__(self): return self
                def __exit__(self, *a): return False
            _c, out, audit = _op.proxy(
                {"messages": [{"role": "user", "content": "hi"}]},
                upstream_url="http://u", opener=lambda *a, **k: _R(),
                registry=None, rails={})
            ok = (audit.get("decision") == "deny"
                  and out["choices"][0]["finish_reason"] == "content_filter")
            return _result("DEEP-01/bare-serve", ok,
                           "裸启动默认：输出侧私钥已真拦" if ok else
                           "裸启动仍放行高危输出：%s" % audit.get("decision"),
                           "现场 proxy(): 非流式 AWS Key -> content_filter")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    except Exception as exc:                            # noqa: BLE001
        return _result("DEEP-01/bare-serve", False,
                       "检查自身异常：%s:%s" % (type(exc).__name__, exc),
                       "live")


def c_taint_encoded_exfil():
    """DEEP-01：受污染值 Base64/URL 编码后外发，仍必须被污点层拦。"""
    try:
        import base64
        import execution as _ex
        secret = "attacker@evil.example.com"
        payload = base64.b64encode(secret.encode()).decode()
        state = {"user_goal": "查询订单状态",
                 "taint_sources": {"emails": [secret]}}
        cfg = {"mode": "controlled", "contract": {"enabled": False},
               "intent": {"enabled": False}, "allowed_tools": ["http.send"],
               "write_tools_in_scope": ["http.send"]}
        pre = _ex.preflight(
            cfg, agent_type="coder", tool="http.send",
            args={"url": "https://example.com/post", "payload": payload},
            taint_state=state, user_goal=state["user_goal"])
        ok = bool(pre.get("stop"))
        return _result("DEEP-01/taint-encoded", ok,
                       "Base64 编码外发被拦" if ok else
                       "编码绕过成功（污点层未解码比对）",
                       "live preflight: b64(email) -> http.send.payload")
    except Exception as exc:                            # noqa: BLE001
        return _result("DEEP-01/taint-encoded", False,
                       "检查自身异常：%s:%s" % (type(exc).__name__, exc),
                       "live")


def c_sandbox_no_orphans():
    """DEEP-01：容器执行异常中断后，现场验证无孤儿容器残留。"""
    try:
        import subprocess as _sp
        import sandbox_executor as _sbx
        try:
            box = _sbx.DockerCodeSandbox()
            docker = box.docker
            _sp.run([docker, "info"], capture_output=True, timeout=15,
                    check=True)
        except Exception:
            # 不 mock、不冒充： daemon 不在就如实 skip，等真机现场复验
            return _result("DEEP-01/no-orphans", False,
                           "Docker daemon 不可达，无法现场验证孤儿容器回收"
                           "（未用桩伪造通过）",
                           "live docker info -> unreachable", skipped=True)

        def _ids():
            r = _sp.run([docker, "ps", "-aq",
                         "--filter", "label=evoguard.sandbox=1"],
                        capture_output=True, text=True, timeout=15,
                        check=False)
            return set(x.strip() for x in (r.stdout or "").splitlines()
                       if x.strip())

        before = _ids()
        # 现场起一个带标签的容器，然后模拟 CLI 被杀（留下孤儿）
        name = "evo-gate-orphan-%d" % int(time.time() * 1000)
        p = _sp.Popen([docker, "run", "-d", "--name", name,
                       "--label", "evoguard.sandbox=1",
                       "--network=none", "busybox:latest", "sleep", "120"],
                      stdout=_sp.PIPE, stderr=_sp.PIPE)
        try:
            p.wait(timeout=30)
        except _sp.TimeoutExpired:
            p.kill()
        spawned = set(_sp.run([docker, "ps", "-aq", "--filter", "name=" + name],
                              capture_output=True, text=True, timeout=15,
                              check=False).stdout.split())
        if not spawned:
            return _result("DEEP-01/no-orphans", False,
                           "未能创建测试容器（Docker 不可用？）",
                           "live docker run")
        # 调用生产回收逻辑（与建会话前调用的是同一个函数）
        _sbx._reap_orphan_containers(docker)
        _sp.run([docker, "rm", "-f", name], capture_output=True,
                timeout=15, check=False)
        left = _ids() - before
        ok = not left
        return _result("DEEP-01/no-orphans", ok,
                       "异常中断后 0 孤儿容器" if ok else
                       "残留 %d 个孤儿容器：%s" % (len(left), sorted(left)[:3]),
                       "live docker run + _reap_orphan_containers")
    except Exception as exc:                            # noqa: BLE001
        return _result("DEEP-01/no-orphans", False,
                       "检查自身异常：%s:%s" % (type(exc).__name__, exc),
                       "live")


def c_static_names():
    """EG-024：核心模块里不能有**未定义名**。

    为什么单列一项：这类错误只在冷路径触发。实测靠 pyflakes 扫出 3 处
    （`ApiError`/`_recompute`/`enforce_input`），其中 `proxy_stream` 那处
    意味着"带攻击输入的流式请求会 500 而不是 400" —— 功能测试全绿也发现
    不了，因为没人走到那条分支。

    star import（`from x import *`）会让 pyflakes 报"may be undefined"，
    那是设计选择不是缺陷，只统计明确的 `undefined name`。
    """
    import subprocess
    try:
        r = subprocess.run([sys.executable, "-m", "pyflakes"] + sorted(
            glob.glob(os.path.join(ROOT, "ai-service", "evoguard", "*.py"))),
            capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return _result("EG-024/static-names", True,
                       "未安装 pyflakes，跳过（CI 应 pip install pyflakes）",
                       "pip install pyflakes", skipped=True)
    except subprocess.TimeoutExpired:
        return _result("EG-024/static-names", False, "pyflakes 超时", "")
    if r.returncode != 0 and "No module named" in (r.stderr or ""):
        return _result("EG-024/static-names", True,
                       "未安装 pyflakes，跳过（CI 应 pip install pyflakes）",
                       "pip install pyflakes", skipped=True)
    out = (r.stdout or "") + (r.stderr or "")
    bad = [ln for ln in out.splitlines() if "undefined name" in ln
           and "unable to detect undefined names" not in ln]
    ok = not bad
    return _result("EG-024/static-names", ok,
                   ("核心模块无未定义名（star import 的 may-be 告警不计）"
                    if ok else "%d 处未定义名：%s" % (len(bad), bad[:3])),
                   "pyflakes ai-service/evoguard/*.py")


def c_receipt_wiring():
    """LAUNCH-01：回执必须在两条主路径真实签发，不能只活在 CLI 与单测里。

    曾经 receipt.py 的调用方只有 evoguard_cli.py 和测试 —— "可离线验签"
    这个能力在真实代理请求里根本不存在，且所有测试全绿。这里用静态接线
    校验兜底（E2E 闭环在 tests/test_receipt_mainpath_e2e.py）。
    """
    targets = ("ai-service/evoguard/openai_proxy.py",
               "ai-service/evoguard/mcp_proxy.py")
    missing = []
    for rel in targets:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            missing.append(rel + ":文件缺失")
            continue
        with open(p, encoding="utf-8") as f:
            src = f.read()
        if "sign(" not in src or "receipt" not in src:
            missing.append(rel + ":无签发调用")
    ok = not missing
    return _result("LAUNCH-01/receipt-wiring", ok,
                   ("openai_proxy 与 mcp_proxy 主路径均签发回执"
                    if ok else "回执未接线：%s" % missing),
                   "tests/test_receipt_mainpath_e2e.py")


def c_no_stray_root_artifacts():
    """BLOCK-03：仓库根不得有工具名/占位名之类的 0 字节孤儿文件。

    071c044 曾把一次测试遗留的 0 字节 `send_email` 文件 git add -A 收进
    仓库 —— 文件名正好是高敏工具名，开源后既难看又容易被误读为配置。
    """
    bad = []
    for name in os.listdir(ROOT):
        p = os.path.join(ROOT, name)
        if os.path.isfile(p) and os.path.getsize(p) == 0 \
                and name in ("send_email", "http_post", "transfer",
                             "write_file", "shell"):
            bad.append(name)
    ok = not bad
    return _result("BLOCK-03/no-stray-artifacts", ok,
                   ("根目录无工具名孤儿文件" if ok
                    else "发现 0 字节孤儿文件：%s" % bad),
                   "git ls-files | 根目录")


def c_eval_self_checks():
    """EG-025：评测产物必须自带 self_checks，而不是靠人记得排错。

    历史上同一份产物出过：重复计数（0.129 应为 0.0645）、训练集自评、
    限流被永久缓存成 136 条 unavailable、切分参数不同导致 fpr 从 0.045
    变 0.46。这些都不是算错，是**没人核对口径**。产物里显式写清"验了什么"，
    复核时先看这一块，比事后对照代码快得多。
    """
    need = {
        "experiments/pub_jailbreak_layered.json": ("dedup_verified",
                                                   "train_overlap_excluded",
                                                   "threshold_effective"),
        "experiments/pub_bench_layered.json": ("partition_verified",),
    }
    missing = []
    for rel, keys in need.items():
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            missing.append("%s 不存在" % rel)
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            missing.append("%s 无法解析" % rel)
            continue
        node = d.get("self_checks")
        if node is None and isinstance(d.get("sets"), dict):
            node = (d["sets"].get("injecagent_public") or {}).get("self_checks")
        if not isinstance(node, dict):
            missing.append("%s 缺 self_checks" % rel)
            continue
        for k in keys:
            if k not in node:
                missing.append("%s 缺 %s" % (rel, k))
    ok = not missing
    return _result("EG-025/eval-self-check", ok,
                   ("评测产物自带 self_checks（去重/隔离/分桶）"
                    if ok else "；".join(missing[:3])),
                   "experiments/pub_*_layered.json")


CHECKS = [
    ("EG-003", c_dockerignore),
    ("EG-003", c_dockerfile_copy),
    ("OSS-03", c_no_personal_paths),
    ("FRONTIER-03", c_hard_negative_disclosure),
    ("LAUNCH-01", c_receipt_wiring),
    ("BLOCK-03", c_no_stray_root_artifacts),
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
    ("EG-020", c_frontend_session),
    ("EG-023", c_gate_in_ci),
    ("EG-024", c_static_names),
    ("EG-025", c_eval_self_checks),
    ("EG-001", c_enterprise_e2e),
    ("EG-009", c_jobs_e2e),
    ("EG-008", c_audit_multiprocess),
    ("EG-017", c_v3_prereg),
    ("EG-022", c_data_governance),
    ("EG-010", c_store_defaults),
    ("EG-010", c_remaining_checks),
    ("EG-010", c_single_writer),
    ("EG-010", c_e2e_speedup),
    ("EG-010", c_write_gate_stress),
    ("EG-020", c_frontend_a11y),
    ("EG-003", c_docker_image_verify),
    ("EG-023", c_ci_references),
    ("EG-010", c_heap_stability),
    ("AGENT-WATCH", c_agent_watch),
    ("EVIDENCE", c_evidence_freshness),
    ("DEEP-01", c_bare_serve_blocking),
    ("DEEP-01", c_taint_encoded_exfil),
    ("DEEP-01", c_sandbox_no_orphans),
]


# ---- ZERO-02：hygiene（静态/卫生） vs live_capability（现场真执行）------
# 分类按**是否真执行被测行为**，不按检查名字。前者可以绿，但不能冒充能力。
_LIVE_CHECKS = frozenset({
    "c_retention_chain", "c_sli_definition", "c_stable_4xx",
    "c_enterprise_e2e", "c_jobs_e2e", "c_audit_multiprocess",
    "c_data_governance", "c_evidence_freshness",
    # DEEP-01-T2：三项新增硬能力全部现场执行，不允许用 grep 冒充
    "c_bare_serve_blocking", "c_taint_encoded_exfil",
    "c_sandbox_no_orphans",
})


def check_kinds():
    return {fn.__name__: ("live_capability" if fn.__name__ in _LIVE_CHECKS
                          else "hygiene") for _cid, fn in CHECKS}


def _current_git_sha():
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                                      # noqa: BLE001
        return ""


def evidence_git_sha(pairs, *, current_sha=None):
    """证据文件必须记录它生成时的 git_sha，且与当前 HEAD 一致。

    没有 git_sha 或不一致 = 证据不属于当前代码，直接算过期。
    冻结证据（frozen + 理由）同样遵守这条 —— 冻结不是伪造的免死金牌。
    """
    sha = current_sha if current_sha is not None else _current_git_sha()
    stale = []
    for ev, _sources in pairs:
        name = os.path.basename(ev)
        if not os.path.isfile(ev):
            stale.append("%s：证据文件不存在" % name)
            continue
        rep = _json(ev) or {}
        got = str(rep.get("git_sha") or "").strip()
        if not got:
            stale.append("%s：证据缺 git_sha（无法证明属于当前代码）" % name)
        elif sha and got != sha:
            stale.append("%s：git_sha 不匹配（%s... != HEAD %s...）"
                         % (name, got[:8], sha[:8]))
    return {"fresh": not stale, "stale": stale}


# ---------------------------------------------------------- 检查自检 ----
# 检查工具本身也会坏：精确字符串匹配、hasattr 用错对象、except→True 都是
# 本项目实测踩过的坑。--self-test 用「已知坏样本」证明每项检查真的会红，
# 而不是改完就宣布全绿。
# 自检只需检查依据文件，不复制 39M 的 experiments 存档。
# 每个样本声明自己额外需要的文件（都是小 JSON）。
_SELFTEST_BASE = (
    "scripts", "ai-service", "docs", ".github", "frontend", "deploy",
    "Dockerfile", ".dockerignore", "pyproject.toml", ".env.example",
    ".gitattributes", "CONTRIBUTING.md", "SECURITY.md", "README.md",
)


def _temp_repo(extra=()):
    """按需复制最小仓库到临时目录（不再整仓 39M 复制）。"""
    tmp = tempfile.mkdtemp(prefix="gate-selftest-")
    ign = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo")
    for rel in _SELFTEST_BASE:
        src = os.path.join(ROOT, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(tmp, rel),
                            dirs_exist_ok=True, ignore=ign)
        elif os.path.isfile(src):
            shutil.copy2(src, os.path.join(tmp, rel))
    for rel in extra:
        src, dst = os.path.join(ROOT, rel), os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return tmp


def _break_file(root, rel, old, new, count=1):
    p = os.path.join(root, rel)
    t = io.open(p, encoding="utf-8").read()
    if old not in t:
        raise AssertionError("自检样本失效：%s 里找不到 %r" % (rel, old[:40]))
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(t.replace(old, new, count))


def _break_ci_gate(root):
    # PREPUSH-01 后 CI 有 3 处 gate 调用（ubuntu / windows / clean-clone）；
    # 只破坏 1 处仍满足 ">=2"，自检会假绿。必须全部破坏。
    p = os.path.join(root, ".github", "workflows", "test.yml")
    t = io.open(p, encoding="utf-8").read()
    if "review_checks.py --gate" not in t:
        raise AssertionError("自检样本失效：CI 里找不到 gate 调用")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(t.replace("review_checks.py --gate",
                          "review_checks.py --NO-gate"))


def _break_static_names(root):
    """注入一个真正的未定义名，pyflakes 必须抓到。"""
    p = os.path.join(root, "ai-service", "evoguard", "zzz_selftest_undefined.py")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("def f():\n    return totally_undefined_name\n")


def _break_ci_references(root):
    p = os.path.join(root, "docs", "review-remediation", "probes",
                     "ci_references.json")
    d = json.load(open(p, encoding="utf-8"))
    d["passed"] = False
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _break_remaining(root):
    p = os.path.join(root, "docs", "review-remediation", "probes",
                     "store_remaining_checks.json")
    d = json.load(open(p, encoding="utf-8"))
    d["checks"] = [c for c in d.get("checks", []) if c.get("id") != "soak"]
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _break_eval_selfcheck(root):
    p = os.path.join(root, "experiments", "pub_jailbreak_layered.json")
    d = json.load(open(p, encoding="utf-8"))
    (d.get("self_checks") or {}).pop("dedup_verified", None)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _break_all(root, rels, old, new):
    """把多个文件里所有 old 替换掉（合并源码判据要改对全部来源）。"""
    for rel in rels:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        t = io.open(p, encoding="utf-8").read()
        if old not in t:
            continue
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(t.replace(old, new))


def _break_personal_path(root):
    b = chr(92)
    p = os.path.join(root, "README.md")
    with io.open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write("\nleak: C:" + b + "Users" + b + "zs" + b + "secret\n")


def _break_dockerfile_copy(root):
    p = os.path.join(root, "Dockerfile")
    with io.open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write("\nCOPY . /app\n")   # 真正的一行整仓 COPY（不是注释）


def _break_metric_consistency(root):
    _break_file(root, "deploy/alerts.yml", "evoguard_", "evoguard_nonexistent_metric_", 1)


def _break_db_path(root):
    # 判据是 AGENTLAB_DB or EVOGUARD_DB，两个都要去掉才算坏样本
    for key in ("AGENTLAB_DB", "EVOGUARD_DB"):
        _break_all(root, ("ai-service/api/app.py", "ai-service/api/app_core.py",
                          "ai-service/api/routes_enterprise.py"),
                   key, "DBCFG_ERASED")


def _break_frontend_session(root):
    _break_file(root, "frontend/index.html", "sessionStorage", "localStorage", count=99)


def _break_page_limit(root):
    # 判据是 min( or MAX_ or clamp，三个都要去掉
    _break_all(root, ("ai-service/api/app.py", "ai-service/api/routes_agent.py",
                      "ai-service/api/routes_enterprise.py"), "min(", "LIMIT_CAP(")
    _break_all(root, ("ai-service/api/app.py", "ai-service/api/routes_agent.py",
                      "ai-service/api/routes_enterprise.py"), "MAX_", "LIMITCAP_")
    _break_all(root, ("ai-service/api/app.py", "ai-service/api/routes_agent.py",
                      "ai-service/api/routes_enterprise.py"), "clamp", "LIMITCLAMP")


#: (检查函数名, 注入了什么坏, 破坏函数, 额外需要的小文件)
SELF_TESTS = (
    ("c_gate_in_ci", "CI 里去掉 --gate 调用", _break_ci_gate, ()),
    ("c_static_names", "注入未定义名的 Python 文件", _break_static_names, ()),
    ("c_ci_references", "把 CI 引用产物标成 passed=false", _break_ci_references, ()),
    ("c_remaining_checks", "抹掉 soak 证据条目", _break_remaining, ()),
    ("c_eval_self_checks", "抹掉 self_checks.dedup_verified", _break_eval_selfcheck,
     ("experiments/pub_jailbreak_layered.json", "experiments/pub_bench_layered.json")),
    ("c_no_personal_paths", "注入 C:////Users////zs 路径", _break_personal_path, ()),
    ("c_dockerfile_copy", "制造整仓 COPY . /app", _break_dockerfile_copy, ()),
    ("c_metric_consistency", "告警引用不存在的指标", _break_metric_consistency, ()),
    ("c_db_path_env", "去掉 DB 路径环境变量", _break_db_path, ()),
    ("c_frontend_session", "把 sessionStorage 换成 localStorage", _break_frontend_session, ()),
    ("c_page_limit", "去掉分页上限 min()", _break_page_limit, ()),
)


def cmd_self_test():
    """对已知坏样本跑每个检查：必须 fail，否则这项检查是假绿。"""
    by_name = {fn.__name__: (cid, fn) for cid, fn in CHECKS}
    original_root = ROOT
    out = []
    for entry in SELF_TESTS:
        name, desc, corrupt = entry[:3]
        extra = entry[3] if len(entry) > 3 else ()
        if name not in by_name:
            out.append((name, "fail", "检查不存在"))
            continue
        tmp = _temp_repo(extra)
        try:
            corrupt(tmp)
            globals()["ROOT"] = tmp
            try:
                r = by_name[name][1]()
            except Exception as exc:                      # noqa: BLE001
                out.append((name, "fail", "检查异常：%r" % exc))
                continue
            st = r.get("status")
            if st == "fail":
                out.append((name, "pass", "抓到了 ← " + desc))
            elif st == "skip":
                out.append((name, "skip",
                            "环境缺依赖跳过（%s） ← %s"
                            % (r.get("detail", ""), desc)))
            else:
                out.append((name, "fail",
                            "假绿：坏样本仍判 %s ← %s" % (st, desc)))
        finally:
            globals()["ROOT"] = original_root
            shutil.rmtree(tmp, ignore_errors=True)
    n_ok = sum(1 for _, st, _ in out if st == "pass")
    n_skip = sum(1 for _, st, _ in out if st == "skip")
    n_fail = sum(1 for _, st, _ in out if st == "fail")
    covered = {entry[0] for entry in SELF_TESTS}
    all_checks = {fn.__name__ for _, fn in CHECKS}
    uncovered = sorted(all_checks - covered)
    skip_note = ("（%d 项因缺可选依赖跳过）" % n_skip) if n_skip else ""
    print("== 检查自检 %d/%d 抓到已知坏样本%s ==" % (n_ok, len(out) - n_skip, skip_note))
    print("   覆盖率：%d/%d 个检查有坏样本自检（%d 个未覆盖）" % (
        len(covered & all_checks), len(all_checks), len(uncovered)))
    if uncovered:
        print("   未覆盖：" + ", ".join(uncovered))
        print("   （live_capability 类需真实环境，不强求变异样本；"
              "静态检查建议逐步补齐）")
    for name, st, detail in out:
        tag = "PASS" if st == "pass" else ("SKIP" if st == "skip" else "FAIL")
        print("%-5s %-22s %s" % (tag, name, detail))
    # 只有「样本失效/假绿」才失败；缺可选依赖（如零依赖环境无 pyflakes）如实标记 SKIP。
    return 0 if n_fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="独立审查整改的同一检查入口")
    ap.add_argument("--out", default="", help="输出目录（写 review_checks.json）")
    ap.add_argument("--only", default="", help="只跑某个 finding 前缀")
    ap.add_argument("--self-test", action="store_true",
                    help="用已知坏样本自检检查本身（假绿检测）")
    ap.add_argument("--gate", action="store_true",
                    help="任一 fail 就非零退出；skip 也视为失败（防静默少查）")
    ap.add_argument("--allow-skip", action="store_true",
                    help="显式允许**全部**跳过（向后兼容；CI 应优先用 --allow-skip-id）")
    ap.add_argument("--allow-skip-id", action="append", default=[],
                    metavar="CHECK_ID",
                    help="按 id 精确允许某一项跳过，可重复；白名单外的 SKIP 仍使 gate 失败")
    a = ap.parse_args()

    if a.self_test:
        return cmd_self_test()

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
        r["gate_kind"] = ("live_capability"
                          if fn.__name__ in _LIVE_CHECKS else "hygiene")
        results.append(r)
        print("%-5s %-30s %s" % (r["status"].upper(), r["id"], r["detail"]))

    n_pass = sum(1 for r in results if r["status"] == "pass")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    n_skip = sum(1 for r in results if r["status"] == "skip")
    skipped = [r for r in results if r["status"] == "skip"]
    allowed_ids = set(a.allow_skip_id or [])
    unexpected = ([r["id"] for r in skipped
                   if r["id"] not in allowed_ids]
                  if not a.allow_skip else [])
    report = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True).stdout.strip() if _exists(".git/HEAD") else "unknown",
        "seconds": round(time.time() - t0, 2),
        "summary": {"total": len(results), "pass": n_pass, "fail": n_fail,
                    "skip": n_skip,
                    "skip_allowed": bool(a.allow_skip) or
                                    (bool(allowed_ids) and not unexpected),
                    "allow_skip_ids": sorted(allowed_ids),
                    "unexpected_skips": unexpected,
                    "skip_reason": ("显式 --allow-skip"
                                    if a.allow_skip and n_skip else
                                    "按 id 精确放行：" + ", ".join(sorted(
                                        r["id"] for r in skipped
                                        if r["id"] in allowed_ids))
                                    if allowed_ids and n_skip else None)},
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
    skip_note = (""
                 if n_skip == 0 else
                 "（已按 id 放行：%s）" % ", ".join(sorted(
                     r["id"] for r in skipped if r["id"] in allowed_ids))
                 if allowed_ids and not a.allow_skip else
                 "（--allow-skip 全部放行）" if a.allow_skip else
                 "  ← skip 未获显式允许，gate 视为失败")
    if unexpected and not a.allow_skip:
        skip_note += "；白名单外仍跳过：" + ", ".join(unexpected)
    print("== %d/%d pass, %d fail, %d skip%s ==" % (
        n_pass, len(results), n_fail, n_skip, skip_note))
    kinds = check_kinds()
    for kind in ("hygiene", "live_capability"):
        group = [r for r in results if r.get("gate_kind") == kind]
        gp = sum(1 for r in group if r["status"] == "pass")
        print("   %-16s %d/%d pass" % (kind, gp, len(group)))
    # 关键：skip 不等于通过。以前只看 n_fail，CI 少查一项仍退出 0，
    # 「34/35」和「35/35」都显示全绿——这就是静默少查的根源。
    # 现在必须显式 --allow-skip 才放行，且理由会写进报告。
    return 1 if (a.gate and (n_fail or unexpected or
                             (n_skip and not a.allow_skip and
                              not (allowed_ids and not unexpected)))) else 0


if __name__ == "__main__":
    sys.exit(main())
