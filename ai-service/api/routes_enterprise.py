# -*- coding: utf-8 -*-
"""Jobs / Audit / Health / Metrics / Enterprise 路由

由 scripts/_split_app2.py 从 app.py 机械拆分；接口零变化。
修改业务逻辑请改对应文件后重跑拆分脚本。
"""
import app_core as _app_core
globals().update({k: v for k, v in vars(_app_core).items()
                  if not k.startswith('__')})

def h_live(self):
    """存活：只证明进程还能回应。**不碰数据库、不跑 COUNT、不进业务 SLI**。

    以前 /api/health 会建 Store 并执行三次 COUNT，Kubernetes liveness
    每分钟打一次就等于每分钟给数据库加三次读；而且探针流量混进
    evoguard_requests_total，「无业务流量」告警永远不响。
    """
    self._send(200, {"ok": True})

def h_ready(self):
    """就绪：依赖不满足就 503，让反代/编排把流量摘走。"""
    db = store_health(self._tenant_id)
    audit = audit_state()
    checks = {
        "db": bool(db.get("ok", True)),
        "audit": audit["ok"] if MODE == "enterprise"
                 else (AUDIT is None or audit["ok"]),
    }
    # INT-01-T1：配了外部后端就必须真的连得上，否则摘流量。
    backend = {"redis_url": BACKENDS.get("redis_url", ""),
               "limiter": (BACKENDS.get("limiter").kind()
                           if BACKENDS.get("limiter") else "memory"),
               "auth_mode": BACKENDS.get("auth_mode") or "off",
               "errors": list(BACKENDS.get("errors") or [])}
    if BACKENDS.get("redis_url"):
        try:
            import redis_backend as _rb
            checks["redis"] = bool(_rb.health(BACKENDS["redis_url"]))
        except Exception:                          # noqa: BLE001
            checks["redis"] = False
    checks["backends"] = not BACKENDS.get("errors")
    ready = all(checks.values())
    self._send(200 if ready else 503, {
        "ready": ready,
        "mode": MODE,
        "checks": checks,
        "audit": audit,
        "backends": backend,
        "version": VERSION,
    })

def h_health(self):
    """诊断用（保留原字段，兼容既有前端与冒烟测试）。

    用缓存的 Store 而不是每次 new —— 热路径不再重复跑 schema DDL。
    要做存活/就绪判断请用 /api/live 与 /api/ready。
    """
    d = CONTROLS.describe()
    self._send(200, {"ok": True, "version": VERSION,
                     "auth_enabled": d["auth_enabled"],
                     "rate_limit": d["rate_limit"],
                     "mode": MODE,
                     "tenant": getattr(self, "_tenant_id", "default"),
                     "identity_verified": self._identity_verified,
                     "audit_enabled": AUDIT is not None,
                     "audit_error": AUDIT_ERROR,
                     "audit_failures": AUDIT_FAILURES,
                     "db": store_health(self._tenant_id),
                     "metrics_exists": os.path.exists(METRICS),
            "security": {"mode": _SEC_MODE,
                         "output_gate_enforces": _OUTPUT_GATE_ENFORCES,
                         "note": ("命中仅记账、不拦截输出"
                                  if not _OUTPUT_GATE_ENFORCES
                                  else "命中会拦截并净化工具输出")},
            "agent_watch": {"mode": _AW_MODE,
                            "enforce": bool(AW_GATE and AW_GATE.enforce),
                            "semantic": bool(_AW_SEM),
                            "semantic_kind": (type(_AW_SEM).__name__
                                              if _AW_SEM else None),
                            "scanned": AW_SEEN[0],
                            "blocked": AW_BLOCKED[0]},
                     "db_path": tenant_db_path(self._tenant_id)})

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
        "security_profile": evo_env.profile_meta(),
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
    """真实模型评测进度/结果。

    EG-019：优先读显式进度文件（原子发布），没有才回退 mtime 推断
    并标注 `source=mtime_legacy`。进行中的评测**不再**被当成已完成。
    """
    self._send(200, real_model_state())

def h_taxonomy(self):
    self._send(200, {"markdown": _read_text(TAXONOMY, "（未找到 docs/taxonomy.md）")})

def h_report(self):
    self._send(200, {"markdown": _read_text(REPORT, "（未生成报告）")})

def h_experiences(self):
    # EG-012：经验列表也走有界分页 + created_at 游标，和 runs 一致，
    # 不再固定 LIMIT 200 且无游标（大库无法翻页/一次拉满内存）。
    store = get_store(self._tenant_id)
    qs = self.path.split("?", 1)[1] if "?" in self.path else ""
    m = re.search(r"(?:^|&)limit=(\d+)", qs)
    limit = min(MAX_PAGE_SIZE, max(1, int(m.group(1)) if m else 60))
    cm = re.search(r"(?:^|&)cursor=([0-9]+(?:\.[0-9]+)?)", qs)
    cursor = float(cm.group(1)) if cm else None
    where, params = "", []
    if cursor is not None:
        where = " WHERE created_at < ?"
        params.append(cursor)
    sql = ("SELECT id,task_type,failure_type,recovery_action,recovery_result,"
           "confidence,validation_status,created_at FROM experiences"
           + where + " ORDER BY created_at DESC, id DESC LIMIT ?")
    params.append(limit)
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    for r in rows:
        r["chain"] = core.as_chain(r["recovery_action"])
    next_cursor = rows[-1]["created_at"] if len(rows) == limit else None
    self._send(200, {"stats": experience.stats(store), "n": len(rows),
                     "items": rows, "limit": limit,
                     "next_cursor": next_cursor})

def h_runs(self):
    # EG-012：以前 `limit` 由查询串直接 int()，客户端可以要 2^31-1 行，
    # 一次把整张 runs 表拉进内存。现在设上限，并用 started_at 做稳定游标。
    store = get_store(self._tenant_id)
    qs = self.path.split("?", 1)[1] if "?" in self.path else ""
    m = re.search(r"(?:^|&)limit=(\d+)", qs)
    limit = min(MAX_PAGE_SIZE, max(1, int(m.group(1)) if m else 60))
    cm = re.search(r"(?:^|&)cursor=([0-9]+(?:\.[0-9]+)?)", qs)
    cursor = float(cm.group(1)) if cm else None
    where, params = "", []
    if cursor is not None:
        where = " WHERE r.started_at < ?"
        params.append(cursor)
    sql = ("SELECT r.id,r.mode,r.status,r.success,r.tokens,r.latency_ms,"
           "r.started_at,e.agent_type,e.task_id,e.fault_type "
           "FROM runs r LEFT JOIN experiments e ON e.id=r.experiment_id"
           + where + " ORDER BY r.started_at DESC, r.id DESC LIMIT ?")
    params.append(limit)
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    next_cursor = rows[-1]["started_at"] if len(rows) == limit else None
    self._send(200, {"n": len(rows), "items": rows,
                     "limit": limit, "next_cursor": next_cursor})

def h_run(self, rid):
    # EG-012：详情聚合读取有预算。以前四类轨迹全部 SELECT * 无上限，
    # 一条长链就能把内存和响应体撑到跟历史总量线性相关。
    store = get_store(self._tenant_id)
    cap = MAX_DETAIL_ITEMS
    run = store.get_run(rid)
    steps = store.list_steps(rid, limit=cap + 1)
    diagnoses = store.list_diagnoses(rid, limit=cap + 1)
    recoveries = store.list_recoveries(rid, limit=cap + 1)
    faults = store.get_faults(rid, limit=cap + 1)
    groups = {"steps": steps, "diagnoses": diagnoses,
              "recoveries": recoveries, "faults": faults}
    truncated = {k: len(v) > cap for k, v in groups.items()}
    out = {"run": run,
           "truncated": truncated,
           "detail_limit": cap}
    for k, v in groups.items():
        out[k] = v[:cap]
    self._send(200, out)

def h_run_once(self):
    # 二次校验：_guarded 已在入口挡过，这里保留纵深防御，
    # 避免将来有人绕过统一入口直接调本方法。
    n = int(self.headers.get("Content-Length") or 0)
    if n > MAX_BODY_BYTES:
        return self._err("E_TOO_LARGE", "请求体超过上限", 413)
    # EG-002：企业模式下写工具默认禁用，除非身份带 executor 角色
    # 且配置了审批后端。缺能力时**不调用写工具**，不是先跑再补审批。
    if MODE == "enterprise":
        if "executor" not in self._roles:
            return self._err("E_FORBIDDEN", "该身份没有执行角色", 403)
        if not APPROVAL_BACKEND:
            return self._err("E_NOT_READY",
                             "企业模式未配置审批后端，写工具已禁用", 503)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    deny = agent_watch_text(body)
    if deny is not None:
        return self._err(
            "E_INPUT_BLOCKED",
            "输入命中高危注入特征（%s），已拒绝"
            % ",".join(f["code"] for f in deny.findings)[:200], 403)
    try:
        out = run_experiment(self._tenant_id, body)
    except ValueError as exc:
        return self._err("E_BAD_REQUEST", str(exc))
    return self._send(200, out)

def h_job_submit(self):
    if ent_jobs is None:
        return self._err("E_UNAVAILABLE", "作业模块未启用", 503)
    # 企业模式同样要过执行角色与审批后端（复制 h_run_once 的门禁语义）
    if MODE == "enterprise":
        if "executor" not in self._roles:
            return self._err("E_FORBIDDEN", "该身份没有执行角色", 403)
        if not APPROVAL_BACKEND:
            return self._err("E_NOT_READY", "未配置审批后端", 503)
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    payload = body.get("payload", body)
    if not isinstance(payload, dict):
        return self._err("E_BAD_REQUEST", "payload 必须是对象")
    deny = agent_watch_text(payload)
    if deny is not None:
        return self._err(
            "E_INPUT_BLOCKED",
            "输入命中高危注入特征（%s），已拒绝"
            % ",".join(f["code"] for f in deny.findings)[:200], 403)
    try:
        validate_run_payload(payload)
    except ValueError as exc:
        return self._err("E_BAD_REQUEST", str(exc))
    key = (self.headers.get("Idempotency-Key")
           or body.get("idempotency_key"))
    store = job_store(self._tenant_id)
    try:
        job, created = store.submit(self._tenant_id, payload, key)
    except OverflowError as exc:
        return self._err("E_OVERLOADED", str(exc), 503)
    job_worker(self._tenant_id)
    # 同幂等键的重试返回原作业（200），新提交返回 202
    return self._send(202 if created else 200,
                      {"job": job, "created": created})

def h_job_list(self):
    if ent_jobs is None:
        return self._err("E_UNAVAILABLE", "作业模块未启用", 503)
    m = re.search(r"[?&]limit=(\d+)", self.path)
    limit = min(MAX_PAGE_SIZE, max(1, int(m.group(1)) if m else 50))
    items = job_store(self._tenant_id).list(self._tenant_id, limit)
    self._send(200, {"n": len(items), "items": items, "limit": limit})

def h_job_get(self, jid):
    job = job_store(self._tenant_id).get(self._tenant_id, jid)
    if job is None:
        return self._err("E_NOT_FOUND", "作业不存在", 404)
    self._send(200, {"job": job})

def h_job_cancel(self, jid):
    ok = job_store(self._tenant_id).cancel(self._tenant_id, jid)
    self._send(200 if ok else 409, {"canceled": ok, "job_id": jid})

def h_job_retry(self, jid):
    ok = job_store(self._tenant_id).retry(self._tenant_id, jid)
    if not ok:
        return self._err("E_CONFLICT", "只有 unknown/failed/canceled 可重放", 409)
    self._send(200, {"requeued": True, "job_id": jid})

