# -*- coding: utf-8 -*-
"""EvoGuard REST API 装配入口（模块与路由已拆分）

由 scripts/_split_app2.py 从 app.py 机械拆分；接口零变化。
修改业务逻辑请改对应文件后重跑拆分脚本。
"""
import app_core as _app_core
globals().update({k: v for k, v in vars(_app_core).items()
                  if not k.startswith('__')})

import routes_agent as _ra
import routes_enterprise as _re


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        # EG-005：连接级读写超时。BaseHTTPRequestHandler 的 setup 建立
        # rfile/wfile 后再设超时才生效；0 表示保持系统默认。
        super().setup()
        if SOCKET_TIMEOUT_S > 0:
            try:
                self.connection.settimeout(SOCKET_TIMEOUT_S)
            except OSError:                              # noqa: BLE001
                pass

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

    def _drain(self, n):
        """分块读完并丢弃，不把内容攒进内存。"""
        left = n
        while left > 0:
            try:
                chunk = self.rfile.read(min(8192, left))
            except OSError:
                break
            if not chunk:
                break
            left -= len(chunk)

    def _err(self, code, msg, http=400):
        # 对外只给稳定错误码 + request_id；内部细节（堆栈、路径、SQL）
        # 一律不进响应体。排障靠服务端日志里的同一个 request_id。
        payload = {"error": {"code": code, "message": msg}}
        rid = getattr(self, "_rid", "")
        if rid:
            payload["request_id"] = rid
        self._send(http, payload)

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
                    # 堆栈只写受控服务端日志，不带进响应（EG-005）。
                    rid = getattr(self, "_rid", "")
                    print("[error] request_id=%s %s" % (rid, fn),
                          file=sys.stderr, flush=True)
                    traceback.print_exc()
                    return self._err("E_INTERNAL",
                                     "内部错误，请提供 request_id 联系管理员", 500)
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
                record_audit_failure()

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
        self._roles = ()
        self._identity_verified = False
        t0 = time.perf_counter()

        # EG-005：入口就有硬上限。以前按 Content-Length 无脑 read()，
        # 一个 100MiB 的声明长度就能把进程内存吃满。超限不读正文、直接关连接。
        cl = self.headers.get("Content-Length")
        if cl is not None:
            try:
                n = int(cl)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY_BYTES:
                # 分块排掉**整个**正文再回 413：只排前 64KiB 的话，
                # 剩余数据还留在 socket 里，客户端有时拿到 413、有时
                # 直接 RST（实测同一检查两次结果不同）。排空全部能让
                # 响应稳定送达；_drain 每次只读 8KB 且丢弃，内存始终
                # 有界 —— 这里防的是「按 Content-Length 一次性分配」，
                # 不是零 I/O。
                self._drain(n)
                self.close_connection = True
                return self._reject(413, rid, method, path, "anonymous",
                                    "body_too_large")

        if MODE == "enterprise":
            # 企业模式的 token 白名单 = IDENTITY_MAP 的键。
            # 不能先过 CONTROLS.authorize：那层只认单一的
            # AGENTLAB_API_TOKEN，会让映射表里其余身份全部 401。
            presented = (self.headers.get("Authorization") or "").partition(" ")[2].strip()
            ident = lookup_identity(presented)
            if not isinstance(ident, dict) or not ident.get("tenant"):
                return self._reject(401, rid, method, path, "anonymous",
                                    "identity_not_bound")
            actor = str(ident.get("actor") or "anonymous")
        else:
            ok, reason, actor = CONTROLS.authorize(self.headers)
            if not ok:
                return self._reject(401, rid, method, path, actor, reason)
        allowed, retry_after = _check_rate(actor)
        if not allowed:
            return self._reject(429, rid, method, path, actor,
                                "rate_limited", retry_after)
        if CONTROLS.kill_switch(method):
            return self._reject(503, rid, method, path, actor, "kill_switch")

        if MODE == "enterprise":
            # EG-001：租户/主体/角色全部来自已验证的映射，客户端自报的
            # X-Tenant-Id 与 X-Actor 一律忽略。
            self._tenant_id = str(ident["tenant"])
            self._roles = tuple(ident.get("roles") or ())
            self._identity_verified = True
            if TENANT_QUOTA is not None:
                q_ok, q_retry = TENANT_QUOTA.check(self._tenant_id)
                if not q_ok:
                    return self._reject(429, rid, method, path, actor,
                                        "tenant_quota", q_retry)
        elif TENANT_RESOLVER is not None:
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
            # EG-013：探针/抓取流量单独计数，不进业务可用率与延迟 SLI。
            # 否则「只有 Prometheus 在抓、没有任何业务」也会被算成有流量。
            is_probe = path in PROBE_PATHS
            if METRICS_REG is not None:
                label = _route_label(path)
                if is_probe:
                    METRICS_REG.inc("evoguard_probe_requests_total",
                                    {"route": label, "status": str(status)})
                else:
                    METRICS_REG.inc("evoguard_requests_total",
                                    {"route": label, "status": str(status)})
                    METRICS_REG.observe("evoguard_request_duration_seconds",
                                        latency_ms / 1000.0, {"route": label})
            _audit(rid, method, path, status, actor, "allowed")
            if AUDIT is not None and not is_probe:
                try:
                    AUDIT.emit({"tenant_id": self._tenant_id,
                                "request_id": rid, "actor": actor,
                                "method": method, "path": path,
                                "status": status, "decision": "allowed",
                                "latency_ms": latency_ms})
                except Exception:       # noqa: BLE001
                    record_audit_failure()
            if TRACER is not None:
                TRACER.record("http.request", latency_ms,
                              route=_route_label(path), status=status)

    def do_GET(self):
        self._guarded("GET")

    def do_POST(self):
        self._guarded("POST")


# 路由方法从 mixin 绑定回 Handler（保持 self 语义）
for _mod in (_ra, _re):
    for _name, _fn in vars(_mod).items():
        if (_name.startswith("h_") or _name in ("_plan_runner",
                                                 "_plan_role_check",
                                                 "_send_stream")):
            if callable(_fn) and getattr(_fn, "__module__", None) == _mod.__name__:
                setattr(Handler, _name, _fn)


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


def validate_enterprise_config():
    """EG-002：企业入口的启动期 fail-closed 校验。

    目的不是「多几个开关」，是让「看起来起来了、其实缺了身份源和审计」
    这种最危险的状态根本起不来。返回人类可读的缺失项列表；空列表才放行。
    """
    errors = []
    if not CONTROLS.config.auth_enabled:
        errors.append("AGENTLAB_API_TOKEN 未设置（显式开关：企业模式不允许"
                      "无鉴权启动；实际 token 白名单是 IDENTITY_MAP 的键）")
    if not IDENTITY_MAP:
        errors.append("AGENTLAB_IDENTITY_MAP 未设置或不是合法 JSON "
                      "（token→tenant/actor/roles 绑定）")
    else:
        for tok, ident in IDENTITY_MAP.items():
            if not isinstance(ident, dict) or not ident.get("tenant"):
                errors.append("身份映射 %r 缺 tenant" % tok)
            elif not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",
                                  str(ident["tenant"])):
                errors.append("身份映射 %r 的 tenant 含非法字符" % tok)
    if not os.environ.get("AGENTLAB_AUDIT_DB"):
        errors.append("AGENTLAB_AUDIT_DB 未设置（企业模式必须持久化审计）")
    if AUDIT_ERROR:
        errors.append("审计库初始化失败：" + AUDIT_ERROR)
    if not os.environ.get("AGENTLAB_DB"):
        errors.append("AGENTLAB_DB 未设置（业务库必须指向可写持久卷）")
    if not APPROVAL_BACKEND:
        errors.append("AGENTLAB_APPROVAL_BACKEND 未设置（写工具默认禁用）")
    return errors


def main():
    # EG-014：服务进程默认给模型调用套上熔断与并发上限。
    # 离线评测脚本（real_model_bench / evoguard eval）不设这个变量 ——
    # 评测要真实失败率，不能让自己的熔断器把「模型不行」记成「被挡住」。
    os.environ.setdefault("AGENTLAB_LLM_RESILIENCE", "1")
    # 启动期 fail-closed：配置不合法（坏数值 / 阈值文件缺失或损坏）就不起来，
    # 避免带着半吊子控制对外提供服务。冒烟测试直接构造 Server，不走这里。
    errors = CONTROLS.validate_config()
    if MODE == "enterprise":
        errors = errors + validate_enterprise_config()
    if errors:
        for e in errors:
            print("配置错误：" + e, file=sys.stderr, flush=True)
        raise SystemExit("启动中止（fail-closed）：服务控制配置不合法")
    warning = CONTROLS.describe().get("warning")
    if warning:
        print("[warn] " + warning, file=sys.stderr, flush=True)
    # 红队 P1-2：默认 audit 只检测不拦截，必须在启动时明说，绝不静默。
    if not _OUTPUT_GATE_ENFORCES:
        print("[warn] 安全模式=%s：输出侧注入命中仅记账、不拦截、不净化正文。"
              "需要真实拦截请设 AGENTLAB_SECURITY_MODE=enforce。"
              % _SEC_MODE, file=sys.stderr, flush=True)
    port = _parse_port(sys.argv[1:])
    if not os.path.isdir(FRONTEND):
        raise SystemExit("找不到前端目录：%s" % FRONTEND)
    # 本地默认只绑回环（最小暴露面）；容器/内网部署显式设为 0.0.0.0，
    # 但必须置于 TLS 反代之后并开启鉴权 —— Dockerfile 里写明了这一点。
    bind = os.environ.get("AGENTLAB_BIND", "127.0.0.1")
    srv = ThreadingHTTPServer((bind, port), Handler)
    # 优雅下线：SIGTERM 默认会直接杀掉进程，正在处理的请求会被截断。
    # shutdown() 必须在另一个线程里调（同线程会死锁）。
    import signal

    def _stop(_signum, _frame):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):        # 非主线程里注册不了，忽略
        pass
    print("EvoGuard  ->  http://%s:%d (mode=%s)" % (bind, port, MODE),
          flush=True)
    print("数据：%s" % DB, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
