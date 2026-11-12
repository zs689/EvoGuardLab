# -*- coding: utf-8 -*-
"""EvoGuard REST API（标准库 http.server，零依赖）。

启动（在 EvoGuardLab 目录下）：
    python ai-service/api/app.py            # http://127.0.0.1:8770
    python ai-service/api/app.py 9000

设计约束：
  1. 只读真实数据（metrics.json / evoguard.db），不返回任何静态假数字
  2. 前端静态文件从 frontend/ 目录提供，前后端同源，不需要 CORS 配置
  3. 所有接口只读；跑实验走 POST /api/run，明确由用户点击触发
"""
import json
import os
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core       # noqa: E402
import sim        # noqa: E402
import runtime    # noqa: E402
import skills     # noqa: E402
import experience # noqa: E402
import service_controls  # noqa: E402  —— 服务层工程基线（纯标准库）

try:                                    # 企业形态模块：缺失也不能拖垮服务
    from enterprise import audit as ent_audit          # noqa: E402
    from enterprise import observability as ent_obs    # noqa: E402
    from enterprise import tenancy as ent_tenancy      # noqa: E402
    _ENT_OK = True
except Exception:                       # noqa: BLE001
    ent_audit = ent_obs = ent_tenancy = None
    _ENT_OK = False


def _read_version():
    """从 pyproject.toml 单一来源读取版本，避免与打包元数据漂移。

    优先用 3.11+ 的 tomllib；3.10 没有 tomllib 时退回解析 ``version = "..."``
    行；两者都失败才兜底到与 pyproject 对齐的常量。要升版本请改 pyproject，
    不要改这里。
    """
    pyproject = os.path.join(ROOT, "pyproject.toml")
    try:
        import tomllib  # Python 3.11+
        with open(pyproject, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        pass
    try:
        with open(pyproject, encoding="utf-8") as f:
            m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', f.read())
            if m:
                return m.group(1)
    except Exception:
        pass
    return "0.6.0"  # 与 pyproject.toml 对齐的兜底


VERSION = _read_version()
# 单一全局实例：限流窗口需要在多线程间共享（check_rate 内用 Lock 保护）。
CONTROLS = service_controls.ServiceControls.from_env()

# 企业形态：租户、配额、指标、追踪、审计。默认全部「可用但不落盘」。
TENANT_RESOLVER = ent_tenancy.HeaderResolver() if _ENT_OK else None
TENANT_REGISTRY = ent_tenancy.TenantRegistry() if _ENT_OK else None
TENANT_QUOTA = (ent_tenancy.TenantQuota(TENANT_REGISTRY)
                if _ENT_OK else None)
# 注意：不要叫 METRICS —— 下面已有同名常量指向 experiments/metrics.json。
METRICS_REG = ent_obs.Metrics() if _ENT_OK else None
TRACER = ent_obs.Tracer() if _ENT_OK else None
# 审计默认关闭：只有显式设置路径才落库，避免测试与只读看板产生副作用。
AUDIT = None
AUDIT_ERROR = ""
if _ENT_OK and os.environ.get("AGENTLAB_AUDIT_DB"):
    try:
        AUDIT = ent_audit.AuditStore(os.environ["AGENTLAB_AUDIT_DB"])
    except Exception as exc:            # noqa: BLE001
        # 不能静默降级：审计掉了还照样「看起来正常」，等于把最该知道的
        # 事故藏起来。这里留话给 stderr 与 /api/health，让监控能告警。
        AUDIT = None
        AUDIT_ERROR = "%s: %s" % (type(exc).__name__, exc)
        print("[warn] 审计库不可用，已降级为不落库：%s" % AUDIT_ERROR,
              file=sys.stderr, flush=True)


def _route_label(path):
    """把路径归一成路由标签，避免指标基数被 id 撑爆。"""
    for _m, pat, fn in ROUTES:
        if re.match(pat, path):
            return "/api/" + fn
    if path.startswith("/static/") or path in ("/", "/index.html"):
        return "/static"
    return "/other"


def _audit(rid, method, path, status, actor, decision):
    """成功/失败请求都记一条结构化日志（token / 请求正文由 log 白名单挡掉）。"""
    CONTROLS.log({"request_id": rid, "method": method, "path": path,
                  "status": status, "actor": actor, "decision": decision})


METRICS = os.path.join(ROOT, "experiments", "metrics.json")
REAL_METRICS = os.path.join(ROOT, "experiments", "real_model", "metrics.json")
REAL_REPORT = os.path.join(ROOT, "experiments", "real_model", "report.md")
REAL_ANALYSIS = os.path.join(ROOT, "experiments", "real_model", "analysis.md")
REAL_ATTRIBUTION = os.path.join(ROOT, "experiments", "real_model", "attribution.md")
REAL_CHECKPOINT = os.path.join(ROOT, "experiments", "real_model", "runs.jsonl")
REPORT = os.path.join(ROOT, "experiments", "evolution_report.md")
TAXONOMY = os.path.join(ROOT, "docs", "taxonomy.md")
FRONTEND = os.path.join(ROOT, "frontend")
DB = os.path.join(ROOT, "data", "evoguard.db")

ROUTES = [
    ("GET", r"^/api/health$", "h_health"),
    ("GET", r"^/api/metrics$", "h_metrics"),
    ("GET", r"^/api/metrics/prometheus$", "h_metrics_prometheus"),
    ("GET", r"^/metrics$", "h_metrics_prometheus"),
    ("GET", r"^/api/meta$", "h_meta"),
    ("GET", r"^/api/real-model$", "h_real_model"),
    ("GET", r"^/api/taxonomy$", "h_taxonomy"),
    ("GET", r"^/api/report$", "h_report"),
    ("GET", r"^/api/skills$", "h_skills"),
    ("GET", r"^/api/experiences$", "h_experiences"),
    ("GET", r"^/api/runs$", "h_runs"),
    ("GET", r"^/api/runs/([\w\-]+)$", "h_run"),
    ("POST", r"^/api/run$", "h_run_once"),
]


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "EvoGuard/" + VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("[api] " + (fmt % args) + "\n")

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        self._response_status = code  # 供 _guarded 记结构化日志
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 传统 IT 安全的基本输出侧约束：不让浏览器「猜」内容类型、
        # 不允许被内嵌、不允许把本域当脚本源执行。
        # 这些不是锦上添花 —— 少了 nosniff，一个 JSON 接口里的
        # 用户可控字符串就可能被浏览器当 HTML 解析。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        rid = getattr(self, "_rid", "")
        if rid:
            self.send_header("X-Request-Id", rid)
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, msg, http=400):
        self._send(http, {"error": {"code": code, "message": msg}})

    def _static(self, path):
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        full = os.path.normpath(os.path.join(FRONTEND, rel))
        if not full.startswith(FRONTEND) or not os.path.isfile(full):
            return self._err("E_NOT_FOUND", "静态文件不存在", 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".json": "application/json; charset=utf-8"}.get(
                     ext, "application/octet-stream")
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)

    def _dispatch(self, method):
        path = self.path.split("?")[0]
        if method == "GET" and (path == "/" or path.startswith("/static/")
                                or path == "/index.html" or path.endswith((".css", ".js"))):
            return self._static(path)
        for m, pat, fn in ROUTES:
            if m != method:
                continue
            hit = re.match(pat, path)
            if hit:
                try:
                    return getattr(self, fn)(*hit.groups())
                except core.ApiError as e:
                    return self._err(e.code, e.message, e.http)
                except Exception:
                    traceback.print_exc()
                    return self._err("E_INTERNAL", traceback.format_exc()[-500:], 500)
        return self._err("E_NOT_FOUND", "接口不存在：%s" % path, 404)

    def _reject(self, status, rid, method, path, actor, decision, retry_after=None):
        """守卫失败：返回带 request_id 的 JSON 体，并记日志 / 指标 / 审计。"""
        payload = {"error": {"code": decision, "message": decision},
                   "request_id": rid}
        if retry_after is not None:
            payload["retry_after"] = round(retry_after, 3)
        self._send(status, payload)
        _audit(rid, method, path, status, actor, decision)
        if METRICS_REG is not None:
            METRICS_REG.inc("evoguard_rejected_total", {"decision": decision})
            METRICS_REG.inc("evoguard_requests_total",
                            {"route": _route_label(path), "status": str(status)})
        if AUDIT is not None:
            try:
                AUDIT.emit({"tenant_id": getattr(self, "_tenant_id", "default"),
                            "request_id": rid, "actor": actor,
                            "method": method, "path": path, "status": status,
                            "decision": decision})
            except Exception:           # noqa: BLE001 - 审计失败不改响应
                pass

    def _guarded(self, method):
        """统一入口：request-id、鉴权、限流、stop switch、租户、配额、审计、指标。

        默认（无 token、无租户头、未触发限流、kill switch 关闭、未设审计库）时
        全部放行，响应体与改造前一致 —— 保持 test_api_smoke 向后兼容。
        """
        rid = CONTROLS.new_request_id()
        path = self.path.split("?")[0]
        self._response_status = 0
        self._tenant_id = "default"
        # 回显到响应头：客户端报「某次请求出错」时能直接对上服务端日志与审计行。
        self._rid = rid
        t0 = time.perf_counter()

        ok, reason, actor = CONTROLS.authorize(self.headers)
        if not ok:
            return self._reject(401, rid, method, path, actor, reason)
        allowed, retry_after = CONTROLS.check_rate(actor)
        if not allowed:
            return self._reject(429, rid, method, path, actor,
                                "rate_limited", retry_after)
        if CONTROLS.kill_switch(method):
            return self._reject(503, rid, method, path, actor, "kill_switch")

        if TENANT_RESOLVER is not None:
            tenant = TENANT_RESOLVER.resolve(self.headers)
            if tenant is None:
                return self._reject(400, rid, method, path, actor,
                                    "invalid_tenant")
            self._tenant_id = tenant.tenant_id
            self._actor_scopes = tenant.scopes
            q_ok, q_retry = TENANT_QUOTA.check(tenant.tenant_id)
            if not q_ok:
                return self._reject(429, rid, method, path, actor,
                                    "tenant_quota", q_retry)

        try:
            self._dispatch(method)
        finally:
            status = self._response_status or 500
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            if METRICS_REG is not None:
                label = _route_label(path)
                METRICS_REG.inc("evoguard_requests_total",
                                {"route": label, "status": str(status)})
                METRICS_REG.observe("evoguard_request_duration_seconds",
                                    latency_ms / 1000.0, {"route": label})
            _audit(rid, method, path, status, actor, "allowed")
            if AUDIT is not None:
                try:
                    AUDIT.emit({"tenant_id": self._tenant_id,
                                "request_id": rid, "actor": actor,
                                "method": method, "path": path,
                                "status": status, "decision": "allowed",
                                "latency_ms": latency_ms})
                except Exception:       # noqa: BLE001
                    pass
            if TRACER is not None:
                TRACER.record("http.request", latency_ms,
                              route=_route_label(path), status=status)

    def do_GET(self):
        self._guarded("GET")

    def do_POST(self):
        self._guarded("POST")

    # ---------------- 接口 ----------------
    def h_health(self):
        store = core.Store(DB)
        d = CONTROLS.describe()
        self._send(200, {"ok": True, "version": VERSION,
                         "auth_enabled": d["auth_enabled"],
                         "rate_limit": d["rate_limit"],
                         "tenant": getattr(self, "_tenant_id", "default"),
                         "audit_enabled": AUDIT is not None,
                         "audit_error": AUDIT_ERROR,
                         "db": store.health(),
                         "metrics_exists": os.path.exists(METRICS)})

    def h_metrics_prometheus(self):
        """Prometheus 文本格式；企业接 scrape 即可。

        注意：`/api/metrics` 仍然是 JSON —— 前端与冒烟测试都以它为
        「唯一数字来源」。Prometheus 单独走 `/api/metrics/prometheus`
        与根路径 `/metrics`，不动既有契约。
        """
        if METRICS_REG is None:
            return self._err("E_UNAVAILABLE", "metrics 未启用", 503)
        self._send(200, METRICS_REG.render_prometheus(),
                   "text/plain; version=0.0.4; charset=utf-8")

    def h_meta(self):
        self._send(200, {
            "version": VERSION,
            "agent_types": sim.AGENT_TYPES,
            "fault_types": sim.FAULT_TYPES,
            "fault_desc": sim.FAULT_DESC,
            "modes": {"baseline": "无故障", "A": "有故障、不恢复",
                      "B": "固定规则恢复（不诊断）",
                      "C": "用经验但不严格验证",
                      "D": "完整闭环（诊断+验证+晋升）"},
            "strategies": sorted(runtime.STRATEGY_BY_CAUSE.values()),
            "repair_coverage": {k: sorted(v) for k, v in runtime.REPAIR_COVERAGE.items()},
            "dataset": sim.dataset_stats(),
            "tasks": {at: [t["task_id"] for t in sim.TASKS[at]]
                      for at in sim.AGENT_TYPES},
            "case_faults": sim.CASE_FAULTS,
            "composites": [
                {"agent": a, "a": fa, "b": fb, "desc": d}
                for a, fa, fb, d in sim.COMPOSITES
            ],
        })

    def h_metrics(self):
        m = _read_json(METRICS, None)
        if m is None:
            return self._err("E_NOT_FOUND", "尚未生成指标，请先跑 ai-service/evolution_bench.py", 404)
        self._send(200, m)

    def h_real_model(self):
        """真实模型评测进度/结果。评测跑一半时也能看，不返回假数字。

        关键：上一轮跑完会留下旧的 metrics.json。新一轮 --fresh 重跑时，
        检查点会持续增长而 metrics 还是旧的 —— 只看「metrics 存在」会
        把「正在跑」误报成「已完成」，前端就会显示过期数字。
        """
        n_done = 0
        ck_mtime = 0
        if os.path.exists(REAL_CHECKPOINT):
            ck_mtime = os.path.getmtime(REAL_CHECKPOINT)
            with open(REAL_CHECKPOINT, encoding="utf-8") as f:
                n_done = sum(1 for line in f if line.strip())
        m = _read_json(REAL_METRICS, None)
        mt_mtime = os.path.getmtime(REAL_METRICS) if os.path.exists(REAL_METRICS) else 0
        running = n_done > 0 and (
            m is None
            or n_done > int(m.get("runs_completed") or 0)
            or ck_mtime > mt_mtime)
        ready = m is not None and not running
        return self._send(200, {
            "ready": ready,
            "running": running,
            "checkpoint_runs": n_done,
            "metrics": m if ready else None,
            "stale_metrics_runs": (m or {}).get("runs_completed"),
            "report": _read_text(REAL_REPORT, "") if ready else "",
            "analysis": _read_text(REAL_ANALYSIS, "") if ready else "",
            "attribution": _read_text(REAL_ATTRIBUTION, "") if ready else "",
        })

    def h_taxonomy(self):
        self._send(200, {"markdown": _read_text(TAXONOMY, "（未找到 docs/taxonomy.md）")})

    def h_report(self):
        self._send(200, {"markdown": _read_text(REPORT, "（未生成报告）")})

    def h_skills(self):
        store = core.Store(DB)
        skills.ensure_schema(store)
        rows = skills.list_skills(store)
        for r in rows:
            r["chain"] = core.as_chain(r["strategy"])
        self._send(200, {"n": len(rows), "items": rows})

    def h_experiences(self):
        store = core.Store(DB)
        experience.ensure_schema(store)
        with store.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id,task_type,failure_type,recovery_action,recovery_result,"
                "confidence,validation_status,created_at FROM experiences "
                "ORDER BY created_at DESC LIMIT 200").fetchall()]
        for r in rows:
            r["chain"] = core.as_chain(r["recovery_action"])
        self._send(200, {"stats": experience.stats(store), "n": len(rows),
                         "items": rows})

    def h_runs(self):
        store = core.Store(DB)
        m = re.search(r"[?&]limit=(\d+)", self.path)
        limit = int(m.group(1)) if m else 60
        with store.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT r.id,r.mode,r.status,r.success,r.tokens,r.latency_ms,"
                "e.agent_type,e.task_id,e.fault_type "
                "FROM runs r LEFT JOIN experiments e ON e.id=r.experiment_id "
                "ORDER BY r.started_at DESC LIMIT ?", (limit,)).fetchall()]
        self._send(200, {"n": len(rows), "items": rows})

    def h_run(self, rid):
        store = core.Store(DB)
        run = store.get_run(rid)
        out = {"run": run,
               "steps": store.list_steps(rid),
               "diagnoses": store.list_diagnoses(rid),
               "recoveries": store.list_recoveries(rid),
               "faults": store.get_faults(rid)}
        self._send(200, out)

    def h_run_once(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
        at = body.get("agent_type", "order")
        tid = body.get("task_id", "A1001")
        ft = body.get("fault_type", "silent_corruption")
        mode = body.get("mode", "D")
        if at not in sim.AGENT_TYPES or mode not in ("baseline", "A", "B", "C", "D", "R"):
            return self._err("E_BAD_REQUEST", "agent_type 或 mode 不合法")
        has_chain = bool(body.get("fault_chain"))
        if ft not in sim.FAULT_TYPES and ft != "none" and not has_chain:
            return self._err("E_BAD_REQUEST", "未知故障类型：%s" % ft)
        if has_chain:
            for f in body["fault_chain"]:
                if not isinstance(f, dict) or f.get("type") not in sim.FAULT_TYPES:
                    return self._err("E_BAD_REQUEST", "fault_chain 里有未知故障类型")
        store = core.Store(DB)
        experience.ensure_schema(store)
        skills.ensure_schema(store)
        exp = store.create_experiment("web/%s/%s/%s/%s" % (at, tid, ft, mode),
                                      at, tid, ft, 0, 7, mode)
        if body.get("fault_chain"):
            exp["fault_chain"] = body["fault_chain"]
        runtime.FORCED_SKILL = None
        run = runtime.run_once(store, exp, mode)
        return self._send(200, {
            "run": run,
            "steps": store.list_steps(run["id"]),
            "diagnoses": store.list_diagnoses(run["id"]),
            "recoveries": store.list_recoveries(run["id"]),
        })


def _parse_port(argv):
    """接受 `--port 8770` 与裸 `8770` 两种写法。

    以前只认裸位置参数，而 `evoguard serve` 传的是 `--port` ——
    `int("--port")` 直接抛 ValueError，CLI 一用就崩。
    """
    for i, a in enumerate(argv):
        if a == "--port":
            if i + 1 >= len(argv):
                raise SystemExit("--port 后面要跟端口号")
            return int(argv[i + 1])
        if a.startswith("--port="):
            return int(a.split("=", 1)[1])
    for a in argv:
        if not a.startswith("-"):
            return int(a)
    return 8770


def main():
    # 启动期 fail-closed：配置不合法（坏数值 / 阈值文件缺失或损坏）就不起来，
    # 避免带着半吊子控制对外提供服务。冒烟测试直接构造 Server，不走这里。
    errors = CONTROLS.validate_config()
    if errors:
        for e in errors:
            print("配置错误：" + e, file=sys.stderr, flush=True)
        raise SystemExit("启动中止（fail-closed）：服务控制配置不合法")
    warning = CONTROLS.describe().get("warning")
    if warning:
        print("[warn] " + warning, file=sys.stderr, flush=True)
    port = _parse_port(sys.argv[1:])
    if not os.path.isdir(FRONTEND):
        raise SystemExit("找不到前端目录：%s" % FRONTEND)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("EvoGuard  ->  http://127.0.0.1:%d" % port, flush=True)
    print("数据：%s" % METRICS, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
