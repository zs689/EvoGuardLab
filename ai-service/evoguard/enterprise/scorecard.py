# -*- coding: utf-8 -*-
"""企业就绪度自评：按**可核对的行为证据**打分，输出缺口清单。

这不是认证，也不是评分卡营销。每条检查都真的跑一下模块或读一份产物；
拿不到证据就记 0 分并把缺口写出来。分数用于「下一轮先补哪」，不是对外宣称。

用法：
    python -m enterprise.scorecard            # 人类可读
    python -m enterprise.scorecard --json     # 机器可读
"""
import argparse
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# enterprise/ -> evoguard/ -> ai-service/ -> <repo>
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # <repo>
sys.path.insert(0, os.path.dirname(HERE))           # ai-service/evoguard

# 权重先写死；企业可改，但改权重不会产生新证据，只改排序。
# v2 起给「进程级验收」单独立一维：模块单测再绿，也证明不了服务真的这么
# 接线。把它折进其它维会掩盖「代码对、装起来错」这类问题。
DIMENSIONS = [
    ("detection", "检测能力", 0.22),
    ("integration", "进程级验收", 0.15),
    ("observability", "可观测性", 0.12),
    ("tenancy", "多租户", 0.12),
    ("audit", "审计与迁移", 0.12),
    ("resilience", "韧性", 0.09),
    ("transport", "传输安全", 0.08),
    ("reproducibility", "可复现性", 0.05),
    ("governance", "数据治理", 0.05),
]


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _p(root, *parts):
    return os.path.join(root, *parts)


def _pack(checks):
    """checks: [(ok, label, evidence)] -> (score 0-5, detail, gaps)

    参数顺序写反过一次（`(label, ok, ev)`），当时没报错，只是把 "True"
    当成检查项名字打了出来 —— 分数看着正常、内容全是噪音。所以这里显式
    拒绝反序元组：宁可让这一维报「探针异常」，也不要悄悄给一个假分。
    """
    for i, c in enumerate(checks):
        if not (isinstance(c, (tuple, list)) and len(c) == 3):
            raise TypeError("check[%d] 必须是 (ok, label, evidence)" % i)
        if isinstance(c[0], str) or not isinstance(c[1], str):
            raise TypeError("check[%d]=%r 顺序反了，应为 (ok, label, evidence)"
                            % (i, c))
    ok_n = sum(1 for ok, _, _ in checks if ok)
    detail = [{"ok": bool(ok), "check": label, "evidence": ev}
              for ok, label, ev in checks]
    gaps = [label for ok, label, _ in checks if not ok]
    score = round(5.0 * ok_n / len(checks), 2) if checks else 0.0
    return score, detail, gaps


# ------------------------------------------------- live service probe

_LIVE = {}


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(port, method, path, headers=None, timeout=5.0):
    """返回 (status, headers-dict, body-bytes)；连不上返回 (0, {}, b"")。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        return r.status, hdrs, body
    except (OSError, http.client.HTTPException):
        return 0, {}, b""
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _live_facts(root):
    """真的起一个服务进程，用 HTTP 打它。

    这一层是「代码存在」和「服务真的这么做」的分界。**没有它，前几个维度
    的分都是自说自话** —— 模块单测证明函数行为，证明不了接线、证明不了
    中间件顺序、也证明不了响应头。失败不是异常，是证据：写进 facts 里。
    """
    if _LIVE:
        return _LIVE
    facts = {"started": False, "error": "", "checks": {}, "port": 0}
    _LIVE.update(facts)
    app = _p(root, "ai-service", "api", "app.py")
    if not os.path.isfile(app):
        facts["error"] = "找不到 ai-service/api/app.py"
        return _LIVE

    import subprocess
    port = _free_port()
    token = "probe-token-0123456789"
    audit_db = os.path.join(tempfile.mkdtemp(prefix="evo-probe-"), "audit.db")
    env = dict(os.environ)
    env.update({
        "AGENTLAB_API_TOKEN": token,
        "AGENTLAB_RATE_LIMIT": "1000",     # 探针自己不打限流分支
        "AGENTLAB_KILL_SWITCH": "",
        "AGENTLAB_LOG_QUIET": "1",
        "AGENTLAB_AUDIT_DB": audit_db,
        # P0-2：demo 默认不再信任客户端自报租户。这里显式授权探针用的
        # acme 演示租户，才能验证「租户头进入上下文」这一项；生产默认
        # 不配白名单时只有 default，匿名无法自报任意租户读数据。
        "EVOGUARD_DEMO_TENANT_WHITELIST": "acme",
    })
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, app, "--port", str(port)],
            cwd=root, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            st, _, _ = _http(port, "GET", "/api/health", timeout=1.0)
            if st:
                break
            if proc.poll() is not None:
                facts["error"] = "服务进程提前退出（code=%s）" % proc.returncode
                return _LIVE
            time.sleep(0.15)
        else:
            facts["error"] = "20s 内没起来"
            return _LIVE

        auth = {"Authorization": "Bearer " + token}
        c = facts["checks"]
        c["port"] = port

        # 1) 鉴权：没 token 必须 401，不是「静默放行」
        st, _, _ = _http(port, "GET", "/api/health")
        c["auth_401"] = (st == 401)

        # 2) 带 token 必须 200，且 request-id 回显在响应头
        full = dict(auth)
        full["X-Tenant-Id"] = "acme"
        full["X-Actor"] = "alice"
        st, hdrs, body = _http(port, "GET", "/api/health", full)
        c["auth_200"] = (st == 200)
        c["rid_header"] = bool(hdrs.get("x-request-id"))
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = {}
        # 3) 租户头真的进到了请求上下文
        c["tenant_propagated"] = (payload.get("tenant") == "acme")
        # 4) 审计在进程里真的开着
        c["audit_on"] = bool(payload.get("audit_enabled"))

        # 5) Prometheus 端点：文本格式、含 counter、带 content-type
        st, hdrs, body = _http(port, "GET", "/metrics", auth)
        text = body.decode("utf-8", "replace")
        c["prometheus"] = (st == 200
                           and "text/plain" in hdrs.get("content-type", "")
                           and "evoguard_requests_total" in text)

        # 6) 既有 JSON 契约没被 Prometheus 端点顶掉
        st, _, body = _http(port, "GET", "/api/metrics", auth)
        try:
            json.loads(body.decode("utf-8"))
            c["metrics_json_contract"] = (st == 200)
        except (UnicodeDecodeError, ValueError):
            c["metrics_json_contract"] = False

        # 7) 未知路由 404 且仍带 request-id（排障时最需要的那条）
        st, hdrs, _ = _http(port, "GET", "/api/definitely-not-a-route", auth)
        c["404_has_rid"] = (st == 404 and bool(hdrs.get("x-request-id")))

        # 8) 审计落库：查一下进程写进去的行
        try:
            from enterprise import audit as A
            store = A.AuditStore(audit_db)
            c["audit_rows"] = store.count("acme")
            store.close()
        except Exception:                  # noqa: BLE001
            c["audit_rows"] = 0

        facts["started"] = True
        facts["port"] = port
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    _LIVE.clear()
    _LIVE.update(facts)
    return _LIVE


def probe_live(root):
    """进程级验收：真实起服务、真实发 HTTP。"""
    f = _live_facts(root)
    if not f["started"]:
        return _pack([(False, "服务可启动并接受 HTTP 请求",
                       f["error"] or "未启动")])
    c = f["checks"]
    checks = [
        (c.get("auth_401") is True, "无 token 时返回 401（不是静默放行）",
         "GET /api/health"),
        (c.get("auth_200") is True, "带 token 时返回 200",
         "Authorization: Bearer ***"),
        (c.get("rid_header") is True, "响应头回显 X-Request-Id",
         "X-Request-Id"),
        (c.get("tenant_propagated") is True, "租户头进入请求上下文",
         "X-Tenant-Id: acme"),
        (c.get("audit_on") is True, "审计在真实进程里开启",
         "AGENTLAB_AUDIT_DB"),
        (c.get("prometheus") is True, "Prometheus 端点返回文本指标",
         "GET /metrics"),
        (c.get("metrics_json_contract") is True,
         "既有 /api/metrics JSON 契约未被破坏", "GET /api/metrics"),
        (c.get("404_has_rid") is True, "未知路由 404 且带 request-id",
         "GET /api/definitely-not-a-route"),
        ((c.get("audit_rows") or 0) >= 1, "审计事件真的落到了库里",
         "audit rows=%s" % c.get("audit_rows")),
    ]
    return _pack(checks)


# ---------------------------------------------------------------- probes

def probe_detection(root):
    checks = []
    passed = []
    for k in ("e1", "e2", "e3", "e4"):
        r = _json(_p(root, "experiments", "semantic_injection_v1",
                     "%s_result.json" % k))
        if r is not None:
            passed.append(bool(r.get("passed")))
    checks.append((len(passed) == 4, "预注册 v1 四个实验都有结果",
                   "experiments/semantic_injection_v1/e*_result.json"))
    v2 = _json(_p(root, "experiments", "semantic_injection_v2", "result.json"))
    checks.append((v2 is not None, "预注册 v2 已执行",
                   "experiments/semantic_injection_v2/result.json"))
    checks.append((bool(passed) and any(passed), "至少一个实验达到判定线",
                   "v1 通过 %d/%d" % (sum(passed), len(passed))))
    checks.append((bool(v2 and v2.get("passed")),
                   "申报的检测器变体达到判定线",
                   "v2 passed=%s" % (v2 or {}).get("passed")))
    checks.append((bool(v2 and v2.get("per_view_checks")),
                   "逐组判定可见（未隐藏负结果）",
                   "per_view_checks"))
    score = 0.0
    if passed:
        # 按「通过的预注册判定比例」给分，v2 的总判定也算一项
        allv = passed + [bool(v2 and v2.get("passed"))] if v2 else passed
        score = round(5.0 * sum(allv) / len(allv), 2)
    detail = [{"ok": bool(ok), "check": label, "evidence": ev}
              for ok, label, ev in checks]
    gaps = [label for ok, label, _ in checks if not ok]
    if v2 is not None and not v2.get("passed"):
        gaps.append("检测器 v2 未过预注册判定线（AUC 下降）；需另立 v3 预注册")
    return score, detail, gaps


def probe_observability(root):
    checks = []
    try:
        from enterprise import observability as O
        m = O.Metrics()
        m.inc("evoguard_requests_total", {"route": "health"})
        m.observe("evoguard_latency_seconds", 0.02, {"route": "health"})
        text = m.render_prometheus()
        checks.append(("evoguard_requests_total" in text
                       and 'route="health"' in text,
                       "# TYPE 与 label 出现在 Prometheus 文本里",
                       "render_prometheus()"))
        pipe = O.LogPipeline([O.MemorySink()])
        pipe.emit({"request_id": "r1"}, tenant_id="t1")
        rec = pipe.sinks[0].records[0]
        checks.append((rec.get("tenant_id") == "t1" and "ts" in rec,
                       "日志管道注入 tenant_id 与 ts",
                       "LogPipeline.emit"))
        tr = O.Tracer()
        with tr.span("probe"):
            pass
        spans = tr.export()
        checks.append((bool(spans) and spans[0]["duration_ms"] is not None,
                       "span 记录时长", "Tracer.span"))
        sampled = O.TrafficSampler(rate=1.0).maybe_sample(
            {"path": "/x", "body": "SECRET"})
        checks.append((sampled == {"path": "/x"},
                       "流量采样只留白名单、不采正文", "TrafficSampler"))
        checks.append((hasattr(O, "MetricsExporter")
                       and hasattr(O, "TraceExporter"),
                       "可插拔 exporter 接口存在",
                       "MetricsExporter/TraceExporter"))
        # 「有指标端点」和「有人真的在 scrape 并会告警」是两件事。
        # 交付的配置文件本身是证据：缺了就是没交付（企业要拿去改的正是它）。
        checks.append((os.path.isfile(_p(root, "deploy", "prometheus.yml")),
                       "交付了 Prometheus scrape 配置", "deploy/prometheus.yml"))
        checks.append((os.path.isfile(_p(root, "deploy", "alerts.yml")),
                       "交付了告警规则", "deploy/alerts.yml"))
    except Exception as exc:               # noqa: BLE001
        checks.append((False, "可观测模块可导入并工作", repr(exc)))
    return _pack(checks)


def probe_tenancy(root):
    checks = []
    try:
        from enterprise import tenancy as T
        ctx = T.HeaderResolver().resolve({"X-Tenant-Id": "acme",
                                          "X-Actor": "alice"})
        checks.append((bool(ctx) and ctx.tenant_id == "acme",
                       "按请求头解析租户与主体", "HeaderResolver"))
        reg = T.TenantRegistry([T.TenantPolicy("acme", request_quota=2)])
        q = T.TenantQuota(reg)
        seq = [q.check("acme")[0] for _ in range(3)]
        checks.append((seq == [True, True, False],
                       "每租户配额独立生效", "TenantQuota.check"))
        # 未知租户不比默认更松：默认是不限（0），未知被收紧成有限配额。
        ghost = reg.policy("ghost")
        checks.append((0 < ghost.request_quota <= 10
                       and set(ghost.allowed_tools)
                       <= set(T.DEFAULT_POLICY.allowed_tools),
                       "未知租户落到最小权限",
                       "TenantRegistry.policy（quota=%d）"
                       % ghost.request_quota))
        try:
            T.require_tenant_filter("SELECT * FROM audit_events")
            ok = False
        except ValueError:
            ok = True
        checks.append((ok, "SQL 缺租户条件被守门拦下",
                       "require_tenant_filter"))
        app = _read(_p(root, "ai-service", "api", "app.py"))
        checks.append(("TenantResolver" in app or "tenant" in app,
                       "服务入口已接线租户上下文", "app.py"))
        # 应用层拼 WHERE 是「自觉遵守」，不是隔离。真隔离在库里。
        checks.append((os.path.isfile(_p(root, "deploy", "rls.sql")),
                       "数据库层行级隔离（非仅字符串守门）",
                       "deploy/rls.sql"))
    except Exception as exc:               # noqa: BLE001
        checks.append((False, "多租户模块可导入并工作", repr(exc)))
    return _pack(checks)


def probe_audit(root):
    checks = []
    try:
        from enterprise import audit as A
        s = A.AuditStore(":memory:")
        checks.append((s.version == A.SCHEMA_VERSION,
                       "schema 迁移到最新版本",
                       "SCHEMA_VERSION=%d" % A.SCHEMA_VERSION))
        s.emit({"tenant_id": "t1", "request_id": "r1", "path": "/x"})
        checks.append((s.count("t1") == 1 and s.query("t2") == [],
                       "审计事件可写入并按租户查询", "AuditStore"))
        try:
            s.emit({"request_id": "no-tenant"})
            ok = False
        except ValueError:
            ok = True
        checks.append((ok, "缺 tenant_id 的审计写入被拒绝", "AuditStore.emit"))
        s.emit({"tenant_id": "t2", "request_id": "r2", "path": "/y", "ts": 1.0})
        checks.append((s.purge_before(2.0) == 1,
                       "保留策略可清理过期事件", "purge_before"))
        app = _read(_p(root, "ai-service", "api", "app.py"))
        checks.append(("AGENTLAB_AUDIT_DB" in app,
                       "服务入口可开启审计落库", "app.py"))
        # 下面两条查的是「审计能不能作为证据」—— 日志表不等于审计。
        # 光有 row_hash 列不算数：要真的重算得过。
        s.emit({"tenant_id": "t3", "request_id": "chain", "ts": 3.0})
        chain_ok, chain_info = s.verify()
        checks.append((chain_ok and chain_info.get("checked", 0) >= 2,
                       "审计链重算通过（防篡改）",
                       "rows=%s/%s"
                       % (chain_info.get("checked"),
                          chain_info.get("checked", 0)
                          + chain_info.get("unhashed", 0))))
        # EG-008 的真能力是「跨进程事务边界」，不是「开了 WAL」。
        # 2026-11-13 实测多进程共享 WAL 审计库时 3/10 报
        # `attempt to write a readonly database`，按证据撤回 WAL，
        # 改由 BEGIN IMMEDIATE + busy_timeout 提供跨进程串行化（0/10 失败）。
        # 探针跟着改成验能力，别继续给一个会引发故障的配置发满分。
        src = _read(_p(root, "ai-service", "evoguard", "enterprise",
                       "audit.py"))
        checks.append(("BEGIN IMMEDIATE" in src
                       and "busy_timeout=30000" in src,
                       "审计写入有跨进程事务边界",
                       "BEGIN IMMEDIATE + busy_timeout=30s"))
        checks.append(("PRAGMA journal_mode=WAL" not in src,
                       "未启用会引发 readonly 的 WAL",
                       "按验收证据撤回，见 perf-before-after.md"))
        s.close()
    except Exception as exc:               # noqa: BLE001
        checks.append((False, "审计模块可导入并工作", repr(exc)))
    return _pack(checks)


def probe_resilience(root):
    checks = []
    try:
        from enterprise import resilience as R
        now = [1000.0]
        b = R.CircuitBreaker(failure_threshold=2, cooldown_s=10,
                             clock=lambda: now[0])
        b.record(False)
        b.record(False)
        checks.append((b.state == "open", "连续失败后熔断打开", "CircuitBreaker"))
        now[0] += 11
        checks.append((b.allow() and b.state == "half_open",
                       "冷却后放行探测", "half_open"))
        b.record(True)
        checks.append((b.state == "closed", "探测成功后闭合", "closed"))
        tb = R.TimeoutBudget(0.05)
        checks.append((tb.remaining() > 0, "超时预算可扣减", "TimeoutBudget"))
        slo = R.SLO("availability", 0.99).evaluate(1000, 995)
        checks.append((slo["met"] is True and slo["budget_remaining"] == 5.0,
                       "SLO 计算 SLI 与错误预算", "SLO.evaluate"))
        # SLO 的数学对，不等于 SLO 的数据真。没跑过压测就是没有底数。
        checks.append((os.path.isfile(_p(root, "experiments", "loadtest_v1",
                                         "result.json")),
                       "SLO 有实测流量底数（非手工数字）",
                       "experiments/loadtest_v1/result.json"))
        checks.append((os.path.isfile(_p(root, "scripts", "drill.py")),
                       "故障演练可一键重跑", "scripts/drill.py"))
    except Exception as exc:               # noqa: BLE001
        checks.append((False, "韧性模块可导入并工作", repr(exc)))
    return _pack(checks)


def probe_transport(root):
    checks = []
    try:
        from enterprise import tls_proxy as P
        checks.append((P.is_trusted("127.0.0.1")
                       and not P.is_trusted("8.8.8.8"),
                       "回环默认受信、外网默认不受信", "is_trusted"))
        checks.append((P.is_trusted("10.1.2.3", ["10.0.0.0/8"]),
                       "支持 CIDR 信任网段", "is_trusted"))
        info = P.resolve_proxy({"X-Forwarded-Proto": "https"}, "8.8.8.8")
        checks.append((info.proto == "http" and not info.trusted,
                       "不可信对端的代理头被忽略", "resolve_proxy"))
        info2 = P.resolve_proxy({"X-Forwarded-Proto": "https"}, "127.0.0.1")
        checks.append((info2.proto == "https",
                       "受信对端的 https 被识别", "resolve_proxy"))
        try:
            P.build_context(P.TLSConfig("missing.pem", "missing.key"))
            ok = False
        except FileNotFoundError:
            ok = True
        checks.append((ok, "证书缺失时明确报错，不静默降级", "build_context"))
        # 配置对象能给对参数，不等于握手能成。这里读的是**真握手成功后**
        # 写下的产物；测试文件存在不算证据（可能是 skip 掉的）。
        hs = _json(_p(root, "experiments", "tls_handshake_v1", "result.json"))
        checks.append((bool(hs and hs.get("handshake") is True),
                       "真实 TLS 握手通过（不只是配置对象）",
                       "TLS %s / %s" % ((hs or {}).get("version"),
                                        (hs or {}).get("cipher"))))
    except Exception as exc:               # noqa: BLE001
        checks.append((False, "TLS/代理模块可导入并工作", repr(exc)))
    return _pack(checks)


def probe_reproducibility(root):
    ga = _read(_p(root, ".gitattributes"))
    ci = _read(_p(root, ".github", "workflows", "test.yml"))
    report = _read(_p(root, "docs", "upgrade-report.md"))
    gi = _read(_p(root, ".gitignore"))
    checks = [
        ("eol=lf" in ga, "存在换行归一化（.gitattributes）", ".gitattributes"),
        ("pip install -e ." in ci and "unittest discover" in ci,
         "CI 安装并可跑 demo/unittest", ".github/workflows/test.yml"),
        ("clean clone" in report or "干净 clone" in report,
         "记录了干净 clone 的实测结果", "upgrade-report.md"),
        ("data/external/" in gi and "_sandbox/" in gi,
         "外部语料与沙箱被忽略", ".gitignore"),
        (os.path.isfile(_p(root, "tests", "test_reproducibility.py")),
         "有字节级可复现性守门测试", "tests/test_reproducibility.py"),
        # 手工验过一次会烂；每次推送都验才算守住。
        ("clean-clone" in ci or "git clone" in ci,
         "CI 每次推送都做干净 clone 复验", ".github/workflows/test.yml"),
    ]
    return _pack(checks)


def probe_governance(root):
    gi = _read(_p(root, ".gitignore"))
    readme = _read(_p(root, "README.md"))
    fix = _read(_p(root, "tests", "fixtures", "external", "README.md"))
    checks = [
        ("data/external/" in gi, "语料正文不入库（.gitignore）", ".gitignore"),
        (os.path.isfile(_p(root, "LICENSE")), "仓库有 LICENSE", "LICENSE"),
        ("MIT" in fix and "InjecAgent" in fix,
         "随仓库分发的夹具标注了上游许可", "fixtures/external/README.md"),
        ("Corpus files stay out of git" in readme,
         "README 声明语料不外发及例外", "README.md"),
        ("data_retention_days" in _read(_p(root, "ai-service", "evoguard",
                                           "enterprise", "tenancy.py")),
         "审计保留期可配置", "TenantPolicy.data_retention_days"),
    ]
    return _pack(checks)


PROBES = {
    "detection": probe_detection,
    "integration": probe_live,
    "observability": probe_observability,
    "tenancy": probe_tenancy,
    "audit": probe_audit,
    "resilience": probe_resilience,
    "transport": probe_transport,
    "reproducibility": probe_reproducibility,
    "governance": probe_governance,
}


def external_review_summary(root=ROOT):
    """把独立审查的台账摘要挂在自评旁边。

    独立审查明确写了「仓库自评分数不计入结论」。这个分数是模块自证，
    九个维度里有几个是「我写的探针检查我写的模块」—— 单看 84.8 很容易
    误以为没事了。所以必须同时显示审查口径：27 项里到底几项真完成。
    """
    text = _read(_p(root, "docs", "review-remediation", "README.md"))
    if not text:
        return None
    import re
    row = re.search(r"\|\s*✅\s*完成\s*\|\s*\*\*(\d+)\*\*", text)
    part = re.search(r"\|\s*🟡\s*部分\s*\|\s*\*\*(\d+)\*\*", text)
    none = re.search(r"\|\s*⬜\s*未做\s*\|\s*\*\*(\d+)\*\*", text)
    if not (row and part and none):
        return None
    return {
        "source": "docs/review-remediation/README.md",
        "total": 27,
        "done": int(row.group(1)),
        "partial": int(part.group(1)),
        "not_done": int(none.group(1)),
        "note": "独立审查口径；本 scorecard 的自评分数不计入其结论。",
    }


def evaluate(root=ROOT):
    dims = []
    weighted = 0.0
    for key, name, weight in DIMENSIONS:
        fn = PROBES.get(key)
        try:
            score, detail, gaps = fn(root) if fn else (0.0, [], ["缺少探针"])
        except Exception as exc:           # noqa: BLE001
            score, detail, gaps = 0.0, [], ["探针异常：%r" % exc]
        weighted += (score / 5.0) * weight
        dims.append({"key": key, "name": name, "weight": weight,
                     "score": score, "max": 5, "checks": detail, "gaps": gaps})
    total = round(weighted * 100, 1)
    all_gaps = []
    for d in dims:
        for g in d["gaps"]:
            all_gaps.append({"dimension": d["name"], "gap": g})
    return {
        "version": 2,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "external_review": external_review_summary(root),
        "dimensions": dims,
        "gaps": all_gaps,
        "disclaimer": ("这是按可核对证据的**自评**，不是认证、不是 SLA、"
                       "不代表可生产部署；独立审查明确不计入本分数。"),
    }


def render_markdown(report):
    L = ["# 企业就绪度自评（scorecard）", "",
         "> 总分 **%s / 100** —— %s" % (report["total"],
                                        report["disclaimer"]), ""]
    ext = report.get("external_review")
    if ext:
        L += ["> **独立审查口径**：27 项发现中 ✅ %d 完成 / 🟡 %d 部分 / "
              "⬜ %d 未做。**不要只看上面的分数** —— 审查明确「仓库自评分数"
              "不计入结论」，以 [整改台账](docs/review-remediation/README.md) 为准。"
              % (ext["done"], ext["partial"], ext["not_done"]), ""]
    L += ["| 维度 | 权重 | 得分 | 缺口数 |", "|---|---|---|---|"]
    for d in report["dimensions"]:
        L.append("| %s | %.0f%% | %.1f/5 | %d |"
                 % (d["name"], d["weight"] * 100, d["score"], len(d["gaps"])))
    L += ["", "## 逐项证据", ""]
    for d in report["dimensions"]:
        L += ["### %s（%.1f/5）" % (d["name"], d["score"]), ""]
        for c in d["checks"]:
            L.append("- %s %s —— `%s`"
                     % ("✅" if c["ok"] else "❌", c["check"], c["evidence"]))
        L.append("")
    if report["gaps"]:
        L += ["## 缺口清单（下一轮先补这些）", ""]
        for g in report["gaps"]:
            L.append("- **[%s]** %s" % (g["dimension"], g["gap"]))
        L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="企业就绪度自评")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    report = evaluate(a.root)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) \
        if a.json else render_markdown(report)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + ("\n" if not text.endswith("\n") else ""))
        print("[√] %s（总分 %s）" % (a.out, report["total"]))
    else:
        # render_markdown 自带结尾换行，print 会再多补一个空行
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
