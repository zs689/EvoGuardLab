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
import hmac
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# EG-004：checkout 布局里模块在 ROOT/ai-service/evoguard；wheel 布局里
# 它们已装到 site-packages/evoguard。两种都要能让 `import core` 成功。
_EVO = os.path.join(ROOT, "ai-service", "evoguard")
if os.path.isdir(_EVO):
    sys.path.insert(0, _EVO)
else:
    try:
        import evoguard as _pkg
        sys.path.insert(0, os.path.dirname(_pkg.__file__))
    except ImportError:
        pass

import core       # noqa: E402
import sim        # noqa: E402
import runtime    # noqa: E402
import skills     # noqa: E402
import experience # noqa: E402
import service_controls  # noqa: E402  —— 服务层工程基线（纯标准库）
import agent_watch       # noqa: E402  —— 外部 agent 监测/入口防护
import security          # noqa: E402  —— 安全模式声明（P1-2 诚实性）
import env as evo_env    # noqa: E402  —— EVOGUARD_* 前缀兼容

# EVOGUARD_* 先于 AGENTLAB_* 生效（向下兼容旧配置名）
# SCORE-05-T2：先应用安全档位（strict 一键开硬门禁），再做前缀映射。
_EV_PROFILE_CHANGED = evo_env.apply_profile()
_EV_ALIASED = evo_env.alias_env() + _EV_PROFILE_CHANGED

try:                                    # 企业形态模块：缺失也不能拖垮服务
    from enterprise import audit as ent_audit          # noqa: E402
    from enterprise import observability as ent_obs    # noqa: E402
    from enterprise import tenancy as ent_tenancy      # noqa: E402
    from enterprise import jobs as ent_jobs            # noqa: E402
    _ENT_OK = True
except Exception:                       # noqa: BLE001
    ent_audit = ent_obs = ent_tenancy = ent_jobs = None
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


# ---- INT-01-T1：可选分布式后端装配（按 env 真实切换，不悬空）-------------
# 默认全走进程内实现（零依赖）；只有显式配置才启用 Redis/JWT。
# 启动时一次性装配并把失败原因记下来，供 /api/ready 如实上报。
def _build_backends():
    """返回 {limiter, breaker, queue, auth, redis, errors}。

    **不静默降级**：配了 EVOGUARD_REDIS_URL 但连不上时，
    limiter 保持 None 并记录 error，/api/ready 会因此 503 ——
    比悄悄退回内存限流（多副本配额翻倍）安全。
    """
    import distributed as _dist
    b = {"limiter": None, "breaker": None, "queue": None,
         "auth": None, "redis_url": "", "errors": [],
         "auth_mode": (os.environ.get("EVOGUARD_AUTH_MODE") or "").strip().lower()}
    url = (os.environ.get("EVOGUARD_REDIS_URL") or "").strip()
    if url:
        b["redis_url"] = url
        try:
            import redis_backend as _rb
            if not _rb.health(url):
                b["errors"].append("redis_unhealthy")
            else:
                b["limiter"] = _rb.RedisLimiter(url=url)
                b["breaker"] = _rb.RedisCircuitBreaker(url=url)
                b["queue"] = _rb.RedisJobQueue(url=url)
        except Exception as exc:                       # noqa: BLE001
            b["errors"].append("redis_init:%s" % type(exc).__name__)
    mode = b["auth_mode"]
    if mode in ("jwt_hs256", "jwt_rs256"):
        try:
            if mode == "jwt_hs256":
                import jwt_hs256 as _jwt
                secret = os.environ.get("EVOGUARD_JWT_SECRET") or ""
                if not secret:
                    b["errors"].append("jwt_secret_missing")
                else:
                    b["auth"] = _dist.TenantResolver(
                        verifier=_jwt.tenant_verifier(
                            secret,
                            issuer=os.environ.get("EVOGUARD_JWT_ISSUER") or None,
                            audience=os.environ.get("EVOGUARD_JWT_AUDIENCE") or None))
            else:
                import jwt_rs256 as _rs
                b["auth"] = _dist.TenantResolver(
                    verifier=_rs.oidc_verifier(
                        issuer=os.environ.get("EVOGUARD_OIDC_ISSUER") or None,
                        jwks_uri=os.environ.get("EVOGUARD_JWKS_URL") or None,
                        audience=os.environ.get("EVOGUARD_JWT_AUDIENCE") or None))
        except Exception as exc:                       # noqa: BLE001
            b["errors"].append("auth_init:%s" % type(exc).__name__)
    elif mode:
        b["errors"].append("unknown_auth_mode:%s" % mode)
    return b


BACKENDS = _build_backends()
# 有害请求语义层：默认关（本地小模型 0.5-5s/条，不能拖慢普通请求）
_HARMFUL_JUDGE_ON = (os.environ.get("EVOGUARD_HARMFUL_JUDGE") or "").strip() in (
    "1", "true", "on", "yes")
# PROD-03：轻量分类器分级路由（默认关，显式开才用）
_LIGHT_GUARD_ON = (os.environ.get("EVOGUARD_LIGHT_GUARD") or "").strip() in (
    "1", "true", "on", "yes")


def _check_rate(actor):
    """限流：配了 Redis 就走跨副本共享，否则退回进程内（原有行为）。"""
    lim = BACKENDS.get("limiter")
    if lim is not None:
        return lim.check(actor, CONTROLS.config.rate_limit,
                         CONTROLS.config.rate_window)
    return CONTROLS.check_rate(actor)

# AgentWatch 入口防护。**默认 enforce** —— 与 security.DEFAULT_MODE=audit
# 相反：这里命中高危直接拒绝，不是只记账。
#   AGENTLAB_AGENT_WATCH=off    → 完全关闭
#   AGENTLAB_AGENT_WATCH=audit  → 只统计不拦（对照/观察用）
#   enforce（默认）              → 命中 error 级特征返回 403
_AW_MODE = (os.environ.get("AGENTLAB_AGENT_WATCH") or "enforce").strip().lower()
# 语义层：显式开启才探测本地模型（探测要 3 秒，不能拖慢普通启动）。
# 代价是每条 0.5-3 秒，所以默认关。
_AW_SEM = None
if (os.environ.get("AGENTLAB_AGENT_WATCH_SEMANTIC") or "").strip() \
        in ("1", "true", "on"):
    # 判官二选一：ollama（中文业务误报低）或 onnx（DeBERTa，快但业务误报高）。
    _judge = (os.environ.get("AGENTLAB_AGENT_WATCH_JUDGE")
              or "ollama").strip().lower()
    if _judge == "onnx":
        _cand = agent_watch.OnnxInjectionJudge()
    else:
        _cand = agent_watch.SemanticJudge()
    _AW_SEM = _cand if _cand.available() else None
AW_GATE = (None if _AW_MODE in ("off", "0", "false")
           else agent_watch.GuardGateway(enforce=(_AW_MODE != "audit"),
                                         semantic=_AW_SEM))
AW_BLOCKED = [0]          # list 包一层，避免 global 声明
AW_SEEN = [0]

# 红队 P1-2：security 默认 audit —— 命中只在 security_effects 记 detected，
# 工具输出正文逐字返回、调用不中断。以前看板只显示 detected 亮灯，容易误读成
# 「已拦截」。这里把真实模式与「输出门禁是否强制拦截」显式暴露，绝不静默。
_SEC_MODE = security.mode()
_OUTPUT_GATE_ENFORCES = (_SEC_MODE == security.MODE_ENFORCE)


def agent_watch_text(body):
    """把请求体文本交给入口网关。返回 None（放行）或 deny 决策。"""
    if AW_GATE is None:
        return None
    try:
        text = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(body)
    decision = AW_GATE.inspect_input(text[:MAX_BODY_BYTES])
    AW_SEEN[0] += 1
    if decision.findings:
        AW_BLOCKED[0] += 1 if not decision.allowed else 0
    if AW_GATE.enforce and not decision.allowed:
        return decision
    return None

# 企业形态：租户、配额、指标、追踪、审计。默认全部「可用但不落盘」。
# 红队 P0-2：demo 模式绝不允许匿名客户端自报任意租户读数据/建库文件。
# 默认只允许 default 租户；本地多租户演示可显式配白名单，或彻底放行旧行为。
def _demo_header_resolver():
    if (os.environ.get("EVOGUARD_DEMO_ALLOW_ANY_TENANT") or "").strip() \
            in ("1", "true", "on"):
        return ent_tenancy.HeaderResolver()          # 旧行为，显式逃生舱
    raw = (os.environ.get("EVOGUARD_DEMO_TENANT_WHITELIST") or "").strip()
    allowed = [x.strip() for x in raw.split(",") if x.strip()] or ["default"]
    # default 必须始终可用：探针/健康检查不带租户头时落到默认租户，
    # 不能因为配了其它白名单租户就把默认租户锁死在外面。
    if "default" not in allowed:
        allowed.append("default")
    return ent_tenancy.HeaderResolver(default_tenant="default",
                                      allowed_tenants=allowed)


TENANT_RESOLVER = _demo_header_resolver() if _ENT_OK else None
TENANT_REGISTRY = ent_tenancy.TenantRegistry() if _ENT_OK else None
TENANT_QUOTA = (ent_tenancy.TenantQuota(TENANT_REGISTRY)
                if _ENT_OK else None)
# 注意：不要叫 METRICS —— 下面已有同名常量指向 experiments/metrics.json。
METRICS_REG = ent_obs.Metrics() if _ENT_OK else None
TRACER = ent_obs.Tracer() if _ENT_OK else None
# 审计默认关闭：只有显式设置路径才落库，避免测试与只读看板产生副作用。
AUDIT = None
AUDIT_ERROR = ""
if _ENT_OK and (os.environ.get("AGENTLAB_AUDIT_DB")
                or os.environ.get("EVOGUARD_PG_DSN")):
    try:
        if os.environ.get("EVOGUARD_PG_DSN"):
            # INFRA-CONC-01-T2：多副本共用同一条 PG 审计链
            AUDIT = ent_audit.PostgresAuditStore(
                os.environ["EVOGUARD_PG_DSN"],
                tenant_id="default",
                hmac_key=os.environ.get("EVOGUARD_AUDIT_HMAC_KEY"))
        else:
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


# ---- 集中配置（EG-011）--------------------------------------------------
# 所有可变路径与只读资产集中在这里解析。默认仍是仓库内相对路径，
# 因此 checkout 直接跑不受影响；容器/企业部署用环境变量指到可写挂载点。
ASSET_ROOT = os.environ.get("AGENTLAB_ASSET_ROOT", ROOT)
DATA_ROOT = os.environ.get("AGENTLAB_DATA_ROOT", os.path.join(ROOT, "data"))
# 业务库：显式环境变量优先，其次落在 DATA_ROOT。以前写死在仓库里，
# 只读根文件系统 + 独立数据卷这种最基本的部署形态都起不来。
DB = os.path.abspath(os.environ.get("AGENTLAB_DB")
                     or os.path.join(DATA_ROOT, "evoguard.db"))
TENANT_DB_ROOT = os.path.join(DATA_ROOT, "tenants")

METRICS = os.path.join(ASSET_ROOT, "experiments", "metrics.json")
REAL_METRICS = os.path.join(ASSET_ROOT, "experiments", "real_model", "metrics.json")
REAL_REPORT = os.path.join(ASSET_ROOT, "experiments", "real_model", "report.md")
REAL_ANALYSIS = os.path.join(ASSET_ROOT, "experiments", "real_model", "analysis.md")
REAL_ATTRIBUTION = os.path.join(ASSET_ROOT, "experiments", "real_model", "attribution.md")
REAL_CHECKPOINT = os.path.join(ASSET_ROOT, "experiments", "real_model", "runs.jsonl")
REPORT = os.path.join(ASSET_ROOT, "experiments", "evolution_report.md")
TAXONOMY = os.path.join(ASSET_ROOT, "docs", "taxonomy.md")
FRONTEND = os.path.join(ASSET_ROOT, "frontend")

# ---- 硬上限（EG-005 / EG-012）-------------------------------------------
MAX_BODY_BYTES = int(os.environ.get("AGENTLAB_MAX_BODY_BYTES", 65536))
# EG-005：单连接的读/写 socket 超时，防慢客户端（slowloris）长期占住线程。
# 0 表示不设置（退回系统默认，供特殊压测）。默认 30s 足够正常请求。
SOCKET_TIMEOUT_S = float(os.environ.get("AGENTLAB_SOCKET_TIMEOUT", "30"))
# SBX-SEC-01-T2：沙箱执行超时的硬上限，防客户端传 timeout_s=86400 打挂服务。
MAX_SANDBOX_TIMEOUT = float(os.environ.get("EVOGUARD_SANDBOX_MAX_TIMEOUT", "30"))
MIN_SANDBOX_TIMEOUT = 1.0
MAX_PAGE_SIZE = int(os.environ.get("AGENTLAB_MAX_PAGE_SIZE", 200))
# 单次运行详情的每类轨迹上限（EG-012）：不传 limit 的内部诊断调用不受影响。
MAX_DETAIL_ITEMS = int(os.environ.get("AGENTLAB_MAX_DETAIL_ITEMS", 500))
# 探针路由：不进业务 SLI，也不该触发「无业务流量」告警（EG-013）。
PROBE_PATHS = frozenset(("/metrics", "/api/metrics/prometheus",
                         "/api/live", "/healthz", "/api/ready"))

# ---- 运行模式与身份绑定（EG-001 / EG-002）--------------------------------
# demo：默认，离线零依赖，X-Tenant-Id 仅作本地演示作用域（不可信）。
# enterprise：身份必须由 token→(tenant,actor,roles) 映射表给出，
#             忽略客户端自报的租户头；缺必要配置就在启动期拒绝。
MODE = os.environ.get("AGENTLAB_MODE", "demo").strip().lower()
if MODE not in ("demo", "enterprise"):
    MODE = "demo"


def _load_identity_map():
    raw = os.environ.get("AGENTLAB_IDENTITY_MAP", "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


IDENTITY_MAP = _load_identity_map()


def lookup_identity(presented):
    """企业模式的 token 白名单就是映射表的键；用恒定时间比对，避免时序侧信道。

    不能直接用 `IDENTITY_MAP.get(token)`：字典哈希查找的时间会随键分布
    变化。表通常很小（几个到几十个身份），线性 compare_digest 足够。
    """
    presented = str(presented or "")
    for key, ident in IDENTITY_MAP.items():
        try:
            if hmac.compare_digest(str(key).encode("utf-8"),
                                   presented.encode("utf-8")):
                return ident
        except (TypeError, ValueError):
            continue
    return None


# 企业模式下写工具需要审批后端；没配就禁用写工具，绝不「先执行再补审批」。
APPROVAL_BACKEND = os.environ.get("AGENTLAB_APPROVAL_BACKEND", "").strip()

# EG-006：运行时审计写失败必须**有信号**。以前 except: pass，
# 监控永远看不到「成功响应已发出但审计没落盘」这种最危险的状态。
AUDIT_FAILURES = 0
_AUDIT_FAIL_LOCK = threading.Lock()


def record_audit_failure():
    global AUDIT_FAILURES
    with _AUDIT_FAIL_LOCK:
        AUDIT_FAILURES += 1
    if METRICS_REG is not None:
        METRICS_REG.inc("evoguard_audit_failures_total")


def audit_state():
    """给就绪探针用：审计不可用 / 写失败都不算 ready。"""
    if AUDIT is None:
        return {"ok": False, "reason": "audit_not_configured"}
    if AUDIT_FAILURES:
        return {"ok": False, "reason": "audit_write_failed",
                "failures": AUDIT_FAILURES}
    return {"ok": True}


ROUTES = [
    ("GET", r"^/api/health$", "h_health"),
    ("GET", r"^/api/live$", "h_live"),
    ("GET", r"^/healthz$", "h_live"),
    ("GET", r"^/api/ready$", "h_ready"),
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
    # EG-009 持久作业：提交返回 job_id，轮询/取消/重试各有独立入口。
    ("POST", r"^/api/jobs$", "h_job_submit"),
    ("GET", r"^/api/jobs$", "h_job_list"),
    ("GET", r"^/api/jobs/([\w\-]+)$", "h_job_get"),
    ("POST", r"^/api/jobs/([\w\-]+)/cancel$", "h_job_cancel"),
    ("POST", r"^/api/jobs/([\w\-]+)/retry$", "h_job_retry"),
    # INT-01-T2：ReAct 与 Skill 合成走主入口
    ("POST", r"^/api/react/run$", "h_react_run"),
    ("POST", r"^/v1/chat/completions$", "h_openai_chat"),
    ("POST", r"^/api/skills/synthesize$", "h_skill_synthesize"),
    ("POST", r"^/api/skills/monitor$", "h_skill_monitor"),
]
# AGENT-PRO-01-T2：容器代码沙箱主路径。**默认关闭** —— 代码执行面必须显式
# 开启，不能因为加了端点就默认暴露。开启后仍受鉴权（_guarded 统一门禁）。
if (os.environ.get("EVOGUARD_SANDBOX_HTTP") or "").strip() in ("1", "true", "on"):
    ROUTES.append(("POST", r"^/api/sandbox/run$", "h_sandbox_run"))
# PLAN-SEC-01：显式计划的执行/审批/续跑主路径。企业模式校验 executor 角色。
ROUTES += [
    ("POST", r"^/api/plans/start$", "h_plan_start"),
    ("POST", r"^/api/plans/([A-Za-z0-9_\-]{1,64})/approve$", "h_plan_approve"),
    ("POST", r"^/api/plans/([A-Za-z0-9_\-]{1,64})/resume$", "h_plan_resume"),
]
_PLAN_RUNNERS = {}       # (tenant_id, session_id) -> PlanRunner
_PLAN_RUNNERS_LOCK = threading.Lock()


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---- 业务库工厂（EG-010 / EG-001）----------------------------------------
# 以前每个 handler 都 `core.Store(DB)`，而 Store.__init__ 会跑一遍
# CREATE TABLE IF NOT EXISTS；/api/health 每次请求还要三次 COUNT。
# 这里按路径缓存一个已初始化的实例：Store 的每个方法自己开连接，
# 实例本身无状态，多线程共享安全。
_STORES = {}
_STORES_LOCK = threading.Lock()
# 红队 P1-3：每个租户一个 sqlite 文件，_STORES 若只增不删，攻击者（或正常
# 流量）不断换租户名就能让磁盘文件数/内存句柄无界增长。加一个可配置 LRU
# 上限；0 表示不限制（保留旧行为，供压测/兼容）。
try:
    _STORE_CACHE_MAX = max(0, int(os.environ.get("EVOGUARD_STORE_CACHE_MAX", "32")))
except ValueError:
    _STORE_CACHE_MAX = 32


def tenant_db_path(tenant_id):
    """按租户分库 —— **物理隔离**，不是「拼一句 WHERE」的字符串守门。

    sqlite 没有行级安全（RLS）；在单文件数据库里声称租户隔离是骗人的。
    这里给每个租户一个独立 sqlite 文件，跨租户读不到就是读不到。
    default 沿用原库，保证既有归档与 861 项测试不变。
    """
    tid = tenant_id or "default"
    if tid == "default":
        return DB
    # 租户名会进文件路径，必须白名单，防 ../ 穿越
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
        raise ValueError("非法租户 ID：%r" % tid)
    return os.path.join(TENANT_DB_ROOT, tid, "evoguard.db")


def get_store(tenant_id="default"):
    # PROD-01-T2：设 EVOGUARD_PG_DSN 时切共享 PG 存储（多副本同一套数据，
    # 租户隔离由 PG RLS + SET LOCAL app.current_tenant 强制）；
    # **不设时下面全部逻辑不变**，默认零依赖 SQLite 行为 100% 兼容。
    pg_dsn = (os.environ.get("EVOGUARD_PG_DSN") or "").strip()
    cache_key = ("pg:" + pg_dsn + ":" + str(tenant_id)) if pg_dsn \
                else tenant_db_path(tenant_id)
    with _STORES_LOCK:
        store = _STORES.get(cache_key)
        if store is None:
            if pg_dsn:
                import pg_store
                store = pg_store.PostgresStore(pg_dsn, tenant_id=tenant_id)
            else:
                store = core.Store(cache_key)   # 唯一一次 schema DDL
            # skills / experience 的建表也移出请求路径（EG-010）
            experience.ensure_schema(store)
            skills.ensure_schema(store)
            # 列表排序命中索引，避免 SCAN + TEMP B-TREE（EG-012）
            with store.conn() as c:
                c.execute("CREATE INDEX IF NOT EXISTS idx_runs_started_at "
                          "ON runs(started_at DESC)")
            # 初始化时读一次，别让每次 /api/health 都多开一条连接
            store._eg_journal = store.journal_mode()
            store._eg_ready = True
            _STORES[cache_key] = store
        else:
            # LRU：命中就删掉再插回，把条目挪到 dict 末尾（3.7+ 保序）。
            store = _STORES.pop(cache_key)
            _STORES[cache_key] = store
        # 淘汰最久未用条目。不主动 close()：在途请求可能仍持有该 Store 的
        # 局部引用，关闭其连接池会波及它们；移除引用后交给 GC 回收即可，
        # 这已能阻止缓存无界增长与文件/句柄泄漏。default 主库永不淘汰。
        if _STORE_CACHE_MAX and len(_STORES) > _STORE_CACHE_MAX:
            for old_key in list(_STORES):
                if old_key != cache_key and old_key != tenant_db_path("default"):
                    _STORES.pop(old_key, None)
                    if len(_STORES) <= _STORE_CACHE_MAX:
                        break
    return store


def store_health(tenant_id="default"):
    try:
        store = get_store(tenant_id)
        d = store.health()
        # EG-010：连接次数与日志模式可观测，便于核对「热路径是否真的
        # 没有重复开连接」以及 WAL 是否生效。
        d["journal_mode"] = getattr(store, "_eg_journal", "unknown")
        d["conn_opened"] = store.conn_opened
        # 连接池状态（未开池时为 None），便于核对连接复用是否有界。
        d["pool_stats"] = store.pool_stats()
        # 写串行化闸门：acquired 是累计写事务数，waiters 是正在排队的线程数
        d["write_gate"] = store.write_gate_stats()
        d["serialize_writes"] = store._serialize_writes
        # 存活的写闸门数：弱引用表跟随 Store 生命周期，长时运行不该无限增长
        d["active_write_gates"] = core.active_write_gates()
        return d
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


# ---- 实验执行（同步 /api/run 与持久作业共用同一份逻辑）-------------------
def validate_run_payload(body):
    """参数校验集中一处，避免同步与异步两条路径口径漂移。"""
    at = body.get("agent_type", "order")
    ft = body.get("fault_type", "silent_corruption")
    mode = body.get("mode", "D")
    if at not in sim.AGENT_TYPES or mode not in ("baseline", "A", "B", "C", "D", "R"):
        raise ValueError("agent_type 或 mode 不合法")
    has_chain = bool(body.get("fault_chain"))
    if ft not in sim.FAULT_TYPES and ft != "none" and not has_chain:
        raise ValueError("未知故障类型：%s" % ft)
    if has_chain:
        for f in body["fault_chain"]:
            if not isinstance(f, dict) or f.get("type") not in sim.FAULT_TYPES:
                raise ValueError("fault_chain 里有未知故障类型")


def run_experiment(tenant_id, body):
    """真正跑一次实验。同步接口与 worker 都调它 —— 不复制第二份实现。"""
    validate_run_payload(body)
    at = body.get("agent_type", "order")
    tid = body.get("task_id", "A1001")
    ft = body.get("fault_type", "silent_corruption")
    mode = body.get("mode", "D")
    store = get_store(tenant_id)
    exp = store.create_experiment("web/%s/%s/%s/%s" % (at, tid, ft, mode),
                                  at, tid, ft, 0, 7, mode)
    if body.get("fault_chain"):
        exp["fault_chain"] = body["fault_chain"]
    runtime.FORCED_SKILL = None
    run = runtime.run_once(store, exp, mode)
    return {
        "run": run,
        "steps": store.list_steps(run["id"]),
        "diagnoses": store.list_diagnoses(run["id"]),
        "recoveries": store.list_recoveries(run["id"]),
    }


# ---- 持久作业（EG-009）--------------------------------------------------
_JOB_STORES = {}
_JOB_WORKERS = {}
_JOB_LOCK = threading.Lock()


def job_store(tenant_id="default"):
    """按租户分库放作业表；与业务库分开，避免抢同一把 sqlite 写锁。"""
    if ent_jobs is None:
        return None
    tid = tenant_id or "default"
    if tid == "default":
        path = os.path.join(DATA_ROOT, "jobs.db")
    else:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
            raise ValueError("非法租户 ID")
        path = os.path.join(TENANT_DB_ROOT, tid, "jobs.db")
    with _JOB_LOCK:
        s = _JOB_STORES.get(path)
        if s is None:
            s = ent_jobs.JobStore(path)
            _JOB_STORES[path] = s
        return s


def job_worker(tenant_id="default"):
    """懒启动：第一次提交任务时才起 worker，测试导入模块不会凭空起线程。"""
    store = job_store(tenant_id)
    if store is None:
        return None
    with _JOB_LOCK:
        w = _JOB_WORKERS.get(store.path)
        if w is None:
            w = ent_jobs.JobWorker(store, run_experiment).start()
            _JOB_WORKERS[store.path] = w
        return w


def real_model_state(metrics_path=None, checkpoint_path=None,
                     progress_path=None, report_paths=None):
    """真实模型评测的状态判定（EG-019）。

    以前只靠「runs.jsonl 比 metrics.json 新」推断，两个进程同时跑时
    会把进行中的评测当成已完成，前端就会显示上一轮的旧数字。
    现在优先读**显式进度文件**（原子发布）；没有才回退到 mtime 推断，
    并明确标注 `source=mtime_legacy`，不假装那是可信状态。
    """
    metrics_path = metrics_path or REAL_METRICS
    checkpoint_path = checkpoint_path or REAL_CHECKPOINT
    progress_path = progress_path or os.path.join(
        os.path.dirname(checkpoint_path), "progress.json")
    report_paths = report_paths or {
        "report": REAL_REPORT, "analysis": REAL_ANALYSIS,
        "attribution": REAL_ATTRIBUTION}

    n_done = 0
    ck_mtime = 0
    if os.path.exists(checkpoint_path):
        ck_mtime = os.path.getmtime(checkpoint_path)
        with open(checkpoint_path, encoding="utf-8") as f:
            n_done = sum(1 for line in f if line.strip())

    metrics = _read_json(metrics_path, None)
    progress = None
    if os.path.exists(progress_path):
        try:
            with open(progress_path, encoding="utf-8") as f:
                progress = json.load(f)
        except (OSError, ValueError) as exc:
            # 损坏的进度文件不能被当成「没有进度」而回退到 mtime 猜 ——
            # 那等于把不可信状态伪装成可用状态。
            progress = {"__corrupt__": repr(exc)}
    out = {
        "checkpoint_runs": n_done,
        "metrics": None,
        "report": "",
        "analysis": "",
        "attribution": "",
        "stale_metrics_runs": (metrics or {}).get("runs_completed"),
        "run_id": None,
        "progress": None,
        "source": "none",
        "error": "",
    }

    if isinstance(progress, dict) and progress.get("__corrupt__"):
        out["error"] = "progress.json 损坏：%s" % progress["__corrupt__"]
        # 只要文件存在但读不懂，就保持 not ready，不按 mtime 猜
        out.update({"source": "progress_corrupt", "ready": False,
                    "running": False, "status": "unknown"})
        return out

    if isinstance(progress, dict) and progress.get("status"):
        # 显式进度是唯一可信来源：status 不是 completed 就一律 not ready，
        # 不管 metrics.json 有多新。
        status = str(progress.get("status"))
        ready = status == "completed"
        out.update({
            "source": "progress",
            "run_id": progress.get("run_id"),
            "progress": progress,
            "ready": ready,
            "running": status in ("running", "starting", "resuming"),
            "status": status,
            "completed": progress.get("completed"),
            "total": progress.get("total"),
            "protocol": progress.get("protocol"),
            "config_hash": progress.get("config_hash"),
            "library_hash": progress.get("library_hash"),
        })
        if ready and metrics is not None:
            out["metrics"] = metrics
            for k, p in report_paths.items():
                out[k] = _read_text(p, "")
        return out

    if isinstance(progress, dict) and not progress.get("status"):
        out["error"] = "progress.json 存在但缺 status 字段"
        out.update({"source": "progress_corrupt", "ready": False,
                    "running": False, "status": "unknown"})
        return out

    # 回退：旧产物没有显式进度，只能按 mtime 推断，并如实标注来源
    if metrics is not None:
        mt = os.path.getmtime(metrics_path)
        legacy_running = n_done > int(metrics.get("runs_completed") or 0) \
            or ck_mtime > mt
    else:
        legacy_running = n_done > 0
    out.update({
        "source": "mtime_legacy",
        "ready": metrics is not None and not legacy_running,
        "running": legacy_running,
        "status": "unknown",
    })
    if out["ready"]:
        out["metrics"] = metrics
        for k, p in report_paths.items():
            out[k] = _read_text(p, "")
    return out


def _read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "EvoGuard/" + VERSION

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

    # ---------------- 接口 ----------------
    # ---------------- 运维探针（EG-013）--------------------------------
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

    def _send_stream(self, _op, body):
        """FIX-PRX-T2：SSE 流式响应。

        tool_calls 增量在代理侧被缓冲，判定通过才补发；被拦则下发标准错误帧。
        因此客户端不会收到「半截参数」去执行。
        """
        try:
            code, lines, audit = _op.proxy_stream(
                body, use_semantic=_HARMFUL_JUDGE_ON,
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
                body, use_semantic=_HARMFUL_JUDGE_ON,
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
        if _LIGHT_GUARD_ON:
            try:
                import light_classifier as _LC
                lg = _LC.guard_input_light(goal,
                                           judge=None if _HARMFUL_JUDGE_ON
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
        if _HARMFUL_JUDGE_ON:
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

    # ---------------- 持久作业接口（EG-009）------------------------------
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
