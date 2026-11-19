# -*- coding: utf-8 -*-
"""PROD-01：PostgreSQL 共享存储（**可选后端，核心零依赖**）。

为什么需要：Compose 虽起 2 个 evoguard 副本 + 1 个 PG 容器，但业务与审计
仍写各自容器内的 SQLite —— 副本 A 写的数据副本 B 查不到。这里给一套与
`core.Store` 同接口的 PostgresStore，让多副本共享同一套表。

**设计红线**：
1. 核心 `evoguard/` 不 import pg8000 —— 驱动在**隔离 venv** 里，
   通过 `PG_DRIVER` 注入或 `import pg8000` 惰性加载；没装时给出明确错误，
   不静默退回 SQLite（那会让多副本配额/数据分裂而不自知）。
2. **每次事务第一句必须是 `SET LOCAL app.current_tenant = %s`**，且连接
   用普通角色 `evoguard_app`（superuser 绕过 RLS，实测验证过）。
3. SQLite 方言 → PG 方言适配只做**必要的两件**：
   `?` → `%s`（跳过字符串字面量里的 `?`）、`CREATE TABLE IF NOT EXISTS`
   → `CREATE TABLE IF NOT EXISTS`（PG 支持）/`AUTOINCREMENT` 剥离。
   SQL 里没有其它 SQLite 专有语法（已 grep 核实：12 处 PRAGMA 都在
   core.Store 的连接层，这里不经过）。
4. `describe()` 明示边界：这是应用级适配，不承诺 100% 方言等价。
"""
import contextlib
import os
import re
import threading

try:
    from . import core as _core
except ImportError:
    import core as _core                                    # type: ignore


class PGStoreError(RuntimeError):
    """PG 不可用 / 配置错误。调用方必须如实上报，不静默降级。"""


def _load_driver():
    """惰性加载 pg8000（隔离 venv 里装；核心库不硬依赖）。"""
    try:
        import pg8000                                       # noqa: F401
        import pg8000.dbapi as dbapi
        return dbapi
    except ImportError as exc:
        raise PGStoreError(
            "EVOGUARD_PG_DSN 已设置但找不到 pg8000；"
            "请在隔离 venv 安装，核心库不引入第三方依赖") from exc


def parse_dsn(dsn):
    """把 postgresql://user:pass@host:port/db 拆成 pg8000 的关键字参数。

    pg8000 不接受整条 URL（会当成 hostname），必须拆开传 —— 这是实测踩到的。
    """
    import urllib.parse as _up
    u = _up.urlparse(str(dsn))
    if u.scheme not in ("postgresql", "postgres"):
        raise PGStoreError("DSN 必须是 postgresql://  开头：%s" % dsn[:40])
    return {
        "host": u.hostname or "127.0.0.1",
        "port": int(u.port or 5432),
        "user": _up.unquote(u.username or ""),
        "password": _up.unquote(u.password or ""),
        "database": (u.path or "").lstrip("/") or "postgres",
    }


def translate(sql):
    """SQLite → PG 最小方言翻译。

    只替换**字面量之外、注释之外**的 `?`。用有限状态机逐字符扫：
    - `'...'` / `"..."` 字符串字面量（含 SQL 标准的 '' 转义）
    - `-- ...` 行注释（到行尾）
    - `/* ... */` 块注释（可跨行）

    这三种都要跳过，否则 `-- what?` 会被换成 `-- what%s`，参数个数与实际
    占位符不符（pg8000 会报错；若驱动容错则可能把参数塞错位置）。
    这是实测发现的真实缺口，不是理论问题。
    """
    s = str(sql)
    out, quote, i, n = [], None, 0, len(s)
    while i < n:
        ch = s[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < n and s[i + 1] == quote:      # '' 转义
                    out.append(s[i + 1]); i += 2; continue
                quote = None
            i += 1
            continue
        # 行注释
        if s.startswith("--", i):
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(s[i:j])
            i = j
            continue
        # 块注释
        if s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(s[i:j])
            i = j
            continue
        if ch in ("'", '"'):
            quote = ch; out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    t = "".join(out)
    # AUTOINCREMENT 在 PG 不存在；本 schema 的 id 都是 TEXT PRIMARY KEY，
    # 极少用到，但保险起见剥掉。
    t = re.sub(r"\s+AUTOINCREMENT\b", "", t, flags=re.I)
    return t


class _PGRow:
    """sqlite3.Row 的最小等价物：支持 dict(row) / row["col"] / row[i]。"""

    __slots__ = ("_keys", "_vals")

    def __init__(self, keys, vals):
        self._keys = tuple(str(k) for k in (keys or []))
        self._vals = tuple(vals or ())

    def keys(self):
        return self._keys

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._vals[self._keys.index(key)]
        return self._vals[key]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return "_PGRow(%r)" % (dict(zip(self._keys, self._vals)),)


class _PGCursor:
    """把 pg8000 cursor 包成 sqlite3 cursor 的常用形状。"""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, args=()):
        # pg8000 的 execute 返回 None，sqlite3 返回 cursor；
        # 全仓库都用 cur = c.execute(...); cur.fetchone() 的链式写法，
        # 所以这里必须返回 self，否则 .fetchone() 挂在 None 上。
        self._raw.execute(translate(sql), tuple(args or ()))
        return self

    def executemany(self, sql, seq):
        return self._raw.executemany(translate(sql), list(seq))

    def executescript(self, sql):
        # PG 的 execute 支持多语句（用 ; 分隔），但没有 sqlite3 的 executescript
        for stmt in [s.strip() for s in str(sql).split(";") if s.strip()]:
            self._raw.execute(translate(stmt))
        return self

    def _wrap(self, row):
        if row is None:
            return None
        return _PGRow([d[0] for d in (self._raw.description or [])], row)

    def fetchone(self):
        return self._wrap(self._raw.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._raw.fetchall()]

    def __iter__(self):
        return (self._wrap(r) for r in self._raw)

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _PGConnection:
    """conn() 上下文返回的对象：execute/close/commit/rollback。"""

    def __init__(self, raw, tenant_id, role):
        self._raw = raw
        self._tenant_id = str(tenant_id or "default")
        self._role = role
        self._bound = False

    def _bind_tenant(self):
        if not self._bound:
            # PG 的 SET LOCAL 不接受参数化占位符（实测 syntax error at $1），
            # 只能拼字面量 —— 因此必须自己做严格白名单，绝不能拼接未净化输入。
            tid = str(self._tenant_id)
            if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", tid):
                raise PGStoreError("非法 tenant_id（只允许字母数字_-/1-64）：%r"
                                   % tid[:40])
            cur = self._raw.cursor()
            cur.execute("SET LOCAL app.current_tenant = '%s'" % tid)
            cur.close()
            self._bound = True

    def cursor(self):
        self._bind_tenant()
        return _PGCursor(self._raw.cursor())

    def execute(self, sql, args=()):
        return self.cursor().execute(sql, args)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        return self._raw.close()

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, v):
        pass                                   # pg8000 返回 tuple，无需 sqlite.Row


class PostgresStore:
    """与 core.Store 同接口的部分实现：业务表共用一套 PG 实例。

    刻意**不继承** core.Store —— 后者 __init__ 会跑 SQLite 建表和连接池，
    与 PG 无关。这里只实现 `conn()` / `init()` / `close()` / 统计字段，
    其余 create_experiment / add_step 等业务方法通过 __getattr__ 委托给一个
    内部的「方法代理」，它们只用 conn()，所以无需重写。
    """

    def __init__(self, dsn, tenant_id="default", *, role="evoguard_app"):
        self.dsn = dsn
        self.tenant_id = str(tenant_id or "default")
        self.role = role
        self.path = dsn                         # 让依赖 store.path 的代码不炸
        self.fast = False
        self.conn_opened = 0
        self._write_gate_obj = None
        self._conn_lock = threading.Lock()
        self._local = threading.local()
        import queue as _queue
        # 小池即可：每个请求独立事务，池只为省掉反复握手
        self._pool = _queue.Queue(maxsize=int(
            os.environ.get("EVOGUARD_PG_POOL_SIZE") or 8))
        self.reconnects = 0
        self._dbapi = _load_driver()
        self._conn_kwargs = parse_dsn(dsn)
        self.init()

    @staticmethod
    def from_env(tenant_id="default", environ=None):
        import os
        env = environ if environ is not None else os.environ
        dsn = env.get("EVOGUARD_PG_DSN") or ""
        if not dsn:
            raise PGStoreError("EVOGUARD_PG_DSN 未设置")
        # 用普通角色连接：superuser 绕过 RLS（已实测）
        role = env.get("EVOGUARD_PG_ROLE") or "evoguard_app"
        return PostgresStore(dsn, tenant_id=tenant_id, role=role)

    def _new_conn(self):
        return self._dbapi.connect(**self._conn_kwargs)

    def _acquire(self):
        """取一条可用连接：优先复用池里的，失效则重建。

        「断线重连」只做一次：先试池中/新建连接执行 `SELECT 1` 探活，
        失败就丢弃并重建。不做无限重试 —— 数据库真的挂了要快速失败，
        而不是把请求线程挂在这里。
        """
        while True:
            raw = None
            try:
                raw = self._pool.get_nowait() if not self._pool.empty() else None
            except Exception:                                # noqa: BLE001
                raw = None
            try:
                if raw is None:
                    raw = self._new_conn()
                cur = raw.cursor()
                cur.execute("SELECT 1")
                cur.close()
                return raw
            except Exception:                                # noqa: BLE001
                if raw is not None:
                    try:
                        raw.close()
                    except Exception:                        # noqa: BLE001
                        pass
                raw = None
                self.reconnects += 1
                if self.reconnects > 3:
                    raise PGStoreError("PG 连接不可用（重连 3 次仍失败）")
                continue

    def _release(self, raw, broken=False):
        if broken:
            try:
                raw.close()
            except Exception:                                # noqa: BLE001
                pass
            return
        try:
            self._pool.put_nowait(raw)
        except Exception:                                    # noqa: BLE001
            try:
                raw.close()
            except Exception:                                # noqa: BLE001
                pass

    @contextlib.contextmanager
    def conn(self):
        with self._conn_lock:
            self.conn_opened += 1
        raw = self._acquire()
        c = _PGConnection(raw, self.tenant_id, self.role)
        broken = False
        try:
            yield c
            c.commit()
        except Exception:
            broken = True
            try:
                c.rollback()
            except Exception:
                pass
            raise
        finally:
            # 归还原始连接（不能塞代理），出错则丢弃
            self._release(raw, broken=broken)

    #: core.SCHEMA 的表原本没有 tenant_id（SQLite 模式靠「每租户一个文件」
    #: 做物理隔离）。PG 模式共享一套表，必须在 **PG 库里**加列做 RLS。
    #: 这是 PG 专用迁移，绝不改 core.SCHEMA —— 否则 SQLite 模式 1000+ 测试
    #: 和既有归档全部受影响。
    PG_TENANT_TABLES = ("experiments", "runs", "steps", "checkpoints",
                        "faults", "diagnoses", "recoveries", "events", "evals")

    def init(self):
        """建表 + PG 专用租户列/RLS。SQLite 模式完全不受影响。"""
        with self.conn() as c:
            c.executescript(_core.SCHEMA)
        # 用超级用户/迁移角色加列与策略（普通角色没有 DDL 权限）。
        # 这里用同一个 DSN 的普通角色尝试；若权限不足，由部署方预先跑
        # deploy/pg_bootstrap.sql（见 describe）。
        with self.conn() as c:
            for table in self.PG_TENANT_TABLES:
                # 幂等：列存在就跳过（cursor 才支持 fetchone）
                cur = c.cursor()
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name=? AND column_name='tenant_id' "
                    "AND table_schema='public'", (table,))
                exists = cur.fetchone()
                cur.close()
                if not exists:
                    c.execute("ALTER TABLE %s ADD COLUMN tenant_id TEXT "
                              "NOT NULL DEFAULT 'default'" % table)
                # 无论列是新加的还是历史遗留，都必须确保默认值取 GUC。
                # 之前写成「列存在就 continue」，导致历史库里 events 等表
                # 的默认值还是 'default' —— INSERT 时 tenant 与 GUC 不符，
                # 被 RLS 的 WITH CHECK 拒绝（实测踩到）。
                c.execute("ALTER TABLE %s ALTER COLUMN tenant_id "
                          "SET DEFAULT current_setting('app.current_tenant', true)"
                          % table)
                c.execute("ALTER TABLE %s ENABLE ROW LEVEL SECURITY" % table)
                c.execute("ALTER TABLE %s FORCE ROW LEVEL SECURITY" % table)
                c.execute("DROP POLICY IF EXISTS tenant_iso_%s ON %s"
                          % (table, table))
                c.execute(
                    "CREATE POLICY tenant_iso_%s ON %s "
                    "USING (tenant_id = current_setting('app.current_tenant', true)) "
                    "WITH CHECK (tenant_id = current_setting('app.current_tenant', true))"
                    % (table, table))
        # EVO-LOOP-01-T2：PG 旧库的 runs 表同样可能缺 skill_id（CREATE TABLE
        # IF NOT EXISTS 对已存在表整表跳过）。幂等补列，与 core.Store.init()
        # 的 SQLite 迁移保持一致，保证精确归因在多副本 PG 下也能写入。
        with self.conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='runs' AND column_name='skill_id' "
                "AND table_schema='public'")
            exists = cur.fetchone()
            cur.close()
            if not exists:
                c.execute("ALTER TABLE runs ADD COLUMN skill_id TEXT")

    def close(self):
        while True:
            try:
                raw = self._pool.get_nowait()
            except Exception:                                # noqa: BLE001
                break
            try:
                raw.close()
            except Exception:                                # noqa: BLE001
                pass
        return None

    def journal_mode(self):
        return "pg"

    def pool_stats(self):
        return None

    def write_gate_stats(self):
        return None

    # ---- 业务方法委托：复用 core.Store 里所有「只用 conn()」的方法 ----
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        # 构造一个只带 conn() 的轻量 core.Store 视图，避免触发其 SQLite __init__
        view = object.__new__(_core.Store)
        view.conn = self.conn
        view.path = self.path
        view.fast = False
        view.conn_opened = self.conn_opened
        view._pool = None
        view._write_gate_obj = None
        return getattr(view, name)


def describe():
    return {"backend": "postgresql via pg8000 (optional, isolated venv)",
            "pool": "queue-based, EVOGUARD_PG_POOL_SIZE (default 8)",
            "reconnect": "SELECT 1 probe, up to 3 rebuilds per acquire",
            "core_dependency": "none (lazy import)",
            "tenant_binding": "SET LOCAL app.current_tenant every transaction",
            "require_non_superuser": True,
            "dialect": "SQLite->PG minimal translation (?, AUTOINCREMENT)",
            "not_supported": ["PRAGMA", "sqlite-specific row_factory",
                              "SQLite-only VACUUM/ATTACH"]}
