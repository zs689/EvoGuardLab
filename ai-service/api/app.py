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
    ("GET", r"^/api/meta$", "h_meta"),
    ("GET", r"^/api/metrics$", "h_metrics"),
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
        """守卫失败：返回带 request_id 的 JSON 体，并记一条结构化日志。"""
        payload = {"error": {"code": decision, "message": decision},
                   "request_id": rid}
        if retry_after is not None:
            payload["retry_after"] = round(retry_after, 3)
        self._send(status, payload)
        _audit(rid, method, path, status, actor, decision)

    def _guarded(self, method):
        """每个请求的统一入口：request-id、鉴权、限流、stop switch、审计日志。

        默认（未设 AGENTLAB_API_TOKEN、未触发限流、kill switch 关闭）时全部放行，
        响应体结构与改造前完全一致 —— 保持 test_api_smoke 向后兼容。
        """
        rid = CONTROLS.new_request_id()
        path = self.path.split("?")[0]
        self._response_status = 0
        ok, reason, actor = CONTROLS.authorize(self.headers)
        if not ok:
            return self._reject(401, rid, method, path, actor, reason)
        allowed, retry_after = CONTROLS.check_rate(actor)
        if not allowed:
            return self._reject(429, rid, method, path, actor,
                                "rate_limited", retry_after)
        if CONTROLS.kill_switch(method):
            return self._reject(503, rid, method, path, actor, "kill_switch")
        try:
            self._dispatch(method)
        finally:
            _audit(rid, method, path, self._response_status or 500,
                   actor, "allowed")

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
                         "db": store.health(),
                         "metrics_exists": os.path.exists(METRICS)})

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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
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
