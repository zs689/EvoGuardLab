# -*- coding: utf-8 -*-
"""Agent / Proxy / Sandbox / Plan / Skill 路由

由 scripts/_split_app2.py 从 app.py 机械拆分；接口零变化。
修改业务逻辑请改对应文件后重跑拆分脚本。
"""
import app_core as _app_core
globals().update({k: v for k, v in vars(_app_core).items()
                  if not k.startswith('__')})

def _flag(name, default=False):
    """运行时读开关：app_core 的模块级快照在 apply_entry_defaults 之前就冻结了。"""
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "on")


def _light_guard_on():
    return _flag("EVOGUARD_LIGHT_GUARD")


def _harmful_judge_on():
    return _flag("EVOGUARD_HARMFUL_JUDGE")


def h_openai_chat(self):
    """PROD-02-T1：OpenAI-compatible 透明安全代理。

    base_url 指向 /v1 即可；工具调用过 preflight，绝不代第三方执行。
    """
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    try:
        import openai_proxy as _op
        if body.get("stream") is True:
            return self._send_stream(_op, body)
        code, resp, audit = _op.proxy(
            body, use_semantic=_harmful_judge_on(),
            agent_type=str(body.get("evoguard_agent") or "react"))
    except ValueError as exc:
        return self._err("E_NOT_READY", str(exc), 503)
    if audit.get("decision") == "deny":
        self._audit(self._rid, "POST", "/v1/chat/completions", code,
                    getattr(self, "_actor", "anonymous"), "denied")
    self._send(code, resp)

def h_react_run(self):
    """INT-01-T2：驱动真实多轮 ReAct Agent（工具走 tool_registry + preflight）。

    body: {"goal": str, "agent_type": str, "max_steps": int,
           "tools": [{"name":..., "schema":{...}}]?, "executor":"none|stub","use_memory":"bool?",
         "memory_task_type":"string?"}
    默认不执行任何真实副作用：executor=none 时工具调用一律 executed=False，
    只有显式 "stub" 才用只读桩执行器（用于演示/集成测试）。
    """
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return self._err("E_BAD_REQUEST", "payload 必须是对象")
    goal = str(body.get("goal") or "").strip()
    if not goal:
        return self._reject(400, self._rid, "POST", "/api/react/run",
                            "anonymous", "goal_required")
    # INT-04 后续：直接越狱/有害请求语义层（**默认关**，显式开启才用）。
    # 判官不可用 -> 503 转人工，绝不静默放行；判有害 -> 400 拒绝。
    if _light_guard_on():
        try:
            import light_classifier as _LC
            lg = _LC.guard_input_light(goal,
                                       judge=None if _harmful_judge_on()
                                       else (lambda t: {"harmful": False,
                                                        "persona_jailbreak": False}))
            if lg["decision"] == "block":
                return self._reject(400, self._rid, "POST", "/api/react/run",
                                    "anonymous", "light_guard_blocked")
            if lg["decision"] == "review":
                return self._reject(503, self._rid, "POST", "/api/react/run",
                                    "anonymous", "light_guard_review")
        except Exception as exc:                    # noqa: BLE001
            return self._reject(503, self._rid, "POST", "/api/react/run",
                                "anonymous",
                                "light_guard_error:" + type(exc).__name__)
    if _harmful_judge_on():
        try:
            import harmful_request_judge as _HRJ
            sem = _HRJ.guard_input_semantic(goal)
        except Exception as exc:                    # noqa: BLE001
            sem = {"decision": "report", "reason":
                   "semantic_error:" + type(exc).__name__}
        if sem.get("decision") == "deny":
            return self._reject(400, self._rid, "POST", "/api/react/run",
                                "anonymous", "harmful_request_denied")
        if sem.get("decision") == "report":
            return self._reject(503, self._rid, "POST", "/api/react/run",
                                "anonymous", "semantic_judge_unavailable")
    try:
        import autonomous_agent as AA
        import tool_registry as TR
        reg = TR.ToolRegistry()
        for t in (body.get("tools") or []):
            if isinstance(t, dict) and t.get("name"):
                reg.from_json_schema(str(t["name"]), t.get("schema") or {},
                                     source="api")
        if not (body.get("tools") or []):
            # 未提供工具时用内置注册表快照（只读模拟工具）
            reg = TR.REGISTRY
        agent_type = str(body.get("agent_type") or "react")
        mode = str(body.get("executor") or "none").lower()
        executor = None
        if mode == "stub":
            calls = []

            def executor(tool, args):
                calls.append({"tool": tool, "args": args})
                return {"ok": True, "stub": True,
                        "docs": ["stub-doc"]}
        memory_store = None
        if body.get("use_memory") is not False:
            memory_store = get_store(self._tenant_id)
        out = AA.ReActAgent(
            agent_type=agent_type, goal=goal, registry=reg,
            executor=executor, memory_store=memory_store,
            memory_task_type=body.get("memory_task_type"),
            session_id=str(body.get("session_id") or "").strip() or None,
            store=get_store(self._tenant_id),
            tenant_id=self._tenant_id,
            max_steps=int(body.get("max_steps") or 4)).run()
        self._send(200, {
            "answer": out["answer"], "stopped_by": out["stopped_by"],
            "contract": out["contract"], "memory": out.get("memory"),
            "session_id": out.get("session_id"),
            "session_saved": out.get("session_saved"),
            "steps": [{"seq": s.get("seq"), "kind": s.get("kind"),
                       "tool": s.get("tool"),
                       "executed": bool(s.get("executed")),
                       "result_codes": s.get("result_codes")}
                      for s in out["steps"]]})
    except Exception as exc:                        # noqa: BLE001
        self._send(500, {"error": "react_failed",
                         "detail": type(exc).__name__})

def h_sandbox_run(self):
    """AGENT-PRO-01-T2：容器代码沙箱执行端点（需 EVOGUARD_SANDBOX_HTTP=1）。

    body: {"code": str, "timeout_s": number?}
    在无网/只读根/限内存 CPU 的临时容器里跑 Python；无 Docker 明确 503，
    绝不降级到宿主机执行。请求体大小、鉴权、限流由 _guarded 统一处理。
    """
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    code = str((body or {}).get("code") or "")
    if not code.strip():
        return self._err("E_BAD_REQUEST", "code 不能为空")
    # SBX-SEC-01-T2：timeout 硬上限，越界直接 400（不接受 86400）。
    # 必须在构造沙箱**之前**校验：否则 timeout=86400 且 Docker 不可用时
    # 会先吃 503，掩盖真正的参数错误（测试曾因此偶发失败）。
    raw_timeout = (body or {}).get("timeout_s")
    timeout = None
    if raw_timeout is not None:
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return self._err("E_BAD_REQUEST", "timeout_s 必须是数字")
        if not (MIN_SANDBOX_TIMEOUT <= timeout <= MAX_SANDBOX_TIMEOUT):
            return self._err(
                "E_BAD_REQUEST",
                "timeout_s 必须在 %.0f..%.0f 秒之间"
                % (MIN_SANDBOX_TIMEOUT, MAX_SANDBOX_TIMEOUT))
    # SBX-SEC-01-T2：入容器前静态扫描高危模式，记审计（不静默）
    try:
        import code_guard as _cg
        findings = _cg.scan_code(code)
        sev = _cg.worst_severity(findings)
        if sev == "error":
            _audit(self._rid, "POST", "/api/sandbox/run", 400,
                   "anonymous", "sandbox_code_guard_blocked")
            return self._err("E_BAD_REQUEST",
                             "代码静态扫描命中高危模式，拒绝执行")
    except ImportError:
        pass                                        # 可选模块，缺失不阻断
    # 参数全部校验通过后才构造沙箱（无 Docker 明确 503，不降级宿主机）
    try:
        import sandbox_executor as _sbx
        box = _sbx.DockerCodeSandbox()
    except Exception as exc:                        # noqa: BLE001
        return self._send(503, {"error": "sandbox_unavailable",
                                "detail": str(exc)[:200]})
    try:
        out = box.run(code, timeout_s=timeout)
        self._send(200, out)
    except _sbx.SandboxOverloaded as exc:
        # SBX-SEC-01-T1：并发超限快速拒绝，429 + retry_after
        self._reject(429, self._rid, "POST", "/api/sandbox/run",
                     "anonymous", "sandbox_overloaded", retry_after=1.0)
    except _sbx.DockerSandboxError as exc:
        self._send(400, {"error": "sandbox_error", "detail": str(exc)[:200]})
    except Exception as exc:                        # noqa: BLE001
        self._send(500, {"error": "sandbox_failed",
                         "detail": type(exc).__name__})

def _plan_runner(self, session_id):
    """取/建 PlanRunner（按 tenant+session 隔离，企业模式要 executor 角色）。"""
    import autonomous_agent as _aa
    key = (self._tenant_id, session_id)
    with _PLAN_RUNNERS_LOCK:
        runner = _PLAN_RUNNERS.get(key)
        if runner is not None:
            return runner
        agent = _aa.ReActAgent(goal="plan", store=get_store(self._tenant_id),
                               tenant_id=self._tenant_id,
                               session_id=session_id,
                               executor=None)   # 默认无执行器 -> 不产生副作用
        runner = _aa.PlanRunner(agent, get_store(self._tenant_id),
                                session_id, tenant_id=self._tenant_id)
        _PLAN_RUNNERS[key] = runner
        return runner

def _plan_role_check(self):
    if MODE == "enterprise" and "executor" not in self._roles:
        self._err("E_FORBIDDEN", "该身份没有执行角色", 403)
        return False
    return True

def h_plan_start(self):
    if not self._plan_role_check():
        return
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    sid = str((body or {}).get("session_id") or "").strip()
    plan = (body or {}).get("plan")
    if not sid or not isinstance(plan, list) or not plan:
        return self._err("E_BAD_REQUEST", "session_id 与非空 plan 必填")
    try:
        out = self._plan_runner(sid).start(plan)
        self._send(200, dict(out, session_id=sid))
    except Exception as exc:                        # noqa: BLE001
        self._send(400, {"error": "plan_start_failed",
                         "detail": str(exc)[:200]})

def h_plan_approve(self, sid):
    if not self._plan_role_check():
        return
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    approver = str((body or {}).get("approver") or "human")
    try:
        out = self._plan_runner(sid).approve(
            approver=approver, tenant_id=self._tenant_id)
        self._send(200, dict(out, session_id=sid))
    except Exception as exc:                        # noqa: BLE001
        # 租户不匹配/无审批单都明确拒绝，不静默
        self._send(403, {"error": "plan_approve_denied",
                         "detail": str(exc)[:200]})

def h_plan_resume(self, sid):
    if not self._plan_role_check():
        return
    try:
        out = self._plan_runner(sid).resume()
        self._send(200, dict(out, session_id=sid))
    except Exception as exc:                        # noqa: BLE001
        self._send(400, {"error": "plan_resume_failed",
                         "detail": str(exc)[:200]})

def h_skill_monitor(self):
    """FIX-C1：线上退化监控。用**上线后真实运行指标**判定是否回滚。

    与 /api/skills/synthesize 的分工：那条是「离线验证后晋升」，
    这条是「上线后变差了要退回去」。两者判据不同，不能互相顶替。
    """
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return self._err("E_BAD_REQUEST", "payload 必须是对象")
    skill_id = str(body.get("skill_id") or "").strip()
    if not skill_id:
        return self._reject(400, self._rid, "POST",
                            "/api/skills/monitor", "anonymous",
                            "skill_id_required")
    try:
        import skill_synthesis as SS
        out = SS.monitor_and_rollback(
            get_store(self._tenant_id), skill_id,
            online_runs=body.get("online_runs"),
            thresholds=body.get("thresholds"),
            since=body.get("since"),
            dry_run=bool(body.get("dry_run")))
        code = 200 if not out.get("error") else 409
        self._send(code, out)
    except Exception as exc:                        # noqa: BLE001
        self._err("E_INTERNAL", type(exc).__name__, 500)

def h_skill_synthesize(self):
    """INT-01-T2：从失败轨迹合成候选 Skill 并跑沙箱门禁（不自动晋升）。"""
    n = int(self.headers.get("Content-Length") or 0)
    try:
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    except Exception:
        return self._err("E_BAD_REQUEST", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return self._err("E_BAD_REQUEST", "payload 必须是对象")
    steps = body.get("steps") or []
    if not isinstance(steps, list):
        return self._reject(400, self._rid, "POST",
                            "/api/skills/synthesize", "anonymous",
                            "steps_must_be_list")
    try:
        import skill_synthesis as SS
        out = SS.synthesize(
            get_store(self._tenant_id), steps,
            task_type=str(body.get("task_type") or "newdomain"),
            expected_total=body.get("expected_total"),
            policy=body.get("policy"))
        diag = out["diagnosis"]
        self._send(200, {
            "diagnosis": {"root_cause": diag.get("root_cause"),
                          "first_bad_step": diag.get("first_bad_step"),
                          "confidence": diag.get("confidence")},
            "accepted": out["accepted"],
            "skill": ({"id": out["skill"]["id"],
                       "name": out["skill"]["name"],
                       "status": out["skill"]["status"]}
                      if out["skill"] else None)})
    except Exception as exc:                        # noqa: BLE001
        self._send(500, {"error": "synthesize_failed",
                         "detail": type(exc).__name__})

def h_skills(self):
    # EG-012：技能列表有界分页 + created_at 游标。
    store = get_store(self._tenant_id)
    qs = self.path.split("?", 1)[1] if "?" in self.path else ""
    m = re.search(r"(?:^|&)limit=(\d+)", qs)
    limit = min(MAX_PAGE_SIZE, max(1, int(m.group(1)) if m else 60))
    cm = re.search(r"(?:^|&)cursor=([0-9]+(?:\.[0-9]+)?)", qs)
    cursor = float(cm.group(1)) if cm else None
    rows = skills.list_skills(store, limit=limit, cursor=cursor)
    for r in rows:
        r["chain"] = core.as_chain(r["strategy"])
    next_cursor = rows[-1]["created_at"] if len(rows) == limit else None
    self._send(200, {"n": len(rows), "items": rows,
                     "limit": limit, "next_cursor": next_cursor})

def _send_stream(self, _op, body):
    """FIX-PRX-T2：SSE 流式响应。

    tool_calls 增量在代理侧被缓冲，判定通过才补发；被拦则下发标准错误帧。
    因此客户端不会收到「半截参数」去执行。
    """
    try:
        code, lines, audit = _op.proxy_stream(
            body, use_semantic=_harmful_judge_on(),
            agent_type=str(body.get("evoguard_agent") or "react"))
    except ValueError as exc:
        return self._err("E_NOT_READY", str(exc), 503)
    if code != 200:
        return self._send(code, lines)
    self._response_status = 200
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("X-Accel-Buffering", "no")
    # 不设 Content-Length：SSE 是流，长度事先不可知；
    # 关连接让客户端知道结束（标准库 http.server 不支持 chunked 编码）。
    self.send_header("Connection", "close")
    self.end_headers()
    try:
        for chunk in lines:
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass                        # 客户端提前断开，不算服务端错误
    self.close_connection = True

