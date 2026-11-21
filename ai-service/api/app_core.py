# -*- coding: utf-8 -*-
"""app.py 的模块级内容（imports / 常量 / 辅助函数）

由 scripts/_split_app2.py 从 app.py 机械拆分；接口零变化。
修改业务逻辑请改对应文件后重跑拆分脚本。
"""
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


def refresh_security_mode():
    """ZERO-01：服务 main() 应用入口默认后重算缓存的安全模式。"""
    global _SEC_MODE, _OUTPUT_GATE_ENFORCES
    _SEC_MODE = security.mode()
    _OUTPUT_GATE_ENFORCES = (_SEC_MODE == security.MODE_ENFORCE)
    return _SEC_MODE


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


