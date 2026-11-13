# -*- coding: utf-8 -*-
"""可恢复审批状态机 + 幂等结算账本（推进计划 §6 S04 / S05）。

这个模块回答一个问题：**写操作被暂停之后，怎么保证它不会重复发生、也不会被偷偷改掉。**

设计要点（对应计划「必须测试的情形」）：
  - `approval_request` 绑定 run_id / 工具 / 规范化参数摘要 / 资源范围 /
    候选版本 / 过期时间 / 批准人 / 一次性消费状态；暂停时存检查点。
  - 参数变化、权限变更、过期、重复消费、拒绝 —— 一律拒绝复用，并留原因。
  - 状态全部在 SQLite 里，进程重启后重新打开 Store 就能恢复（不依赖内存）。
  - 结算用 `idempotency_key` 主键 + INSERT OR IGNORE：「提交成功但响应丢失」
    重试同一键时仍只有一笔。
  - 首次写入与恢复期重放写入走**同一个** `ApprovalGate.require()` 门禁。

边界：本模块不引入任意代码执行、不联网（HTTP 部分见 real_tasks.py 的回环服务），
不碰真实支付；本地演示只写专用 SQLite 结算表。
"""
import decimal
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

PENDING, APPROVED, REJECTED, EXPIRED, CONSUMED = (
    "pending", "approved", "rejected", "expired", "consumed")
DEFAULT_TTL = 24 * 3600.0          # 计划 P0 03 的 24 小时有效期口径

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests(
  id TEXT PRIMARY KEY, ts REAL, run_id TEXT, tool TEXT,
  args_digest TEXT, args_canonical TEXT, resource_scope TEXT,
  candidate_version TEXT, idempotency_key TEXT, policy_digest TEXT,
  status TEXT, created_at REAL, expires_at REAL,
  approved_by TEXT, decided_at REAL, consumed_at REAL,
  checkpoint_id TEXT, reason TEXT, first_write INTEGER);
CREATE INDEX IF NOT EXISTS idx_appr_run ON approval_requests(run_id);
CREATE INDEX IF NOT EXISTS idx_appr_status ON approval_requests(status);

CREATE TABLE IF NOT EXISTS settlement_ledger(
  idempotency_key TEXT PRIMARY KEY, run_id TEXT, order_id TEXT,
  total REAL, rule_version TEXT, created_at REAL, payload TEXT);
"""


class ApprovalRequired(core.ApiError):
    """写操作被暂停，等待人工审批。带 request_id 与检查点，状态可定位。"""

    def __init__(self, request_id, checkpoint_id, tool):
        super().__init__(
            "E_APPROVAL_REQUIRED",
            "写操作 %s 需要审批（request=%s）" % (tool, request_id), 409)
        self.request_id = request_id
        self.checkpoint_id = checkpoint_id
        self.tool = tool


class ApprovalDenied(core.ApiError):
    """审批不可复用：参数/权限变了、过期、重复消费、被拒绝。"""

    def __init__(self, request_id, reason):
        super().__init__("E_APPROVAL_INVALID", reason, 409)
        self.request_id = request_id
        self.reason = reason


class LedgerConflict(core.ApiError):
    """幂等键相同、内容不同：不能当成「已结过」放过去。"""

    def __init__(self, idempotency_key, reason):
        super().__init__("E_LEDGER_CONFLICT", reason, 409)
        self.idempotency_key = idempotency_key
        self.reason = reason


def _version_mismatch(row, candidate_version, idempotency_key,
                      tool_schema_version=None, require_all=False):
    """审批里的版本字段与本次调用是否一致。返回原因字符串，一致则 None。

    刻意「严进宽出」：只要调用方**给了**期望值就必须逐项相等，
    审批里是 NULL 而调用方给了值也算不一致 —— 缺值不能当作通配符。
    """
    pairs = (("候选版本", row.get("candidate_version"), candidate_version),
             ("幂等键", row.get("idempotency_key"), idempotency_key),
             ("工具 schema 版本", row.get("tool_schema_version"),
              tool_schema_version))
    for label, stored, wanted in pairs:
        if wanted is None:
            if require_all:
                return "审批上下文缺%s，不能用于消费" % label
            continue
        if stored != wanted:
            return ("%s不匹配（审批绑定 %r，本次 %r），需重新审批"
                    % (label, stored, wanted))
    return None


CANONICAL_VERSION = "canonical-v2"

# 按**字段语义**规范化（评审 R03）。全局一刀切「所有数字转 float 再四舍五入」
# 会造成三类假等价：9007199254740992 == ...993、1.0000001 == 1.0000002、
# "file" == " file "。审批摘要一旦有假等价，改参数就能复用旧批准。
AMOUNT_FIELDS = {"amount", "total", "price", "max_amount", "fee", "cost",
                 "balance", "payable", "unit_price"}
# 金额按「分」定标：504 与 504.0 是同一条业务金额，必须同摘要。
_CENT = decimal.Decimal("0.01")


def _money(value):
    try:
        d = decimal.Decimal(str(value))
    except (decimal.InvalidOperation, ValueError):
        return "invalid:%r" % (value,)
    if not d.is_finite():
        return "nonfinite:%s" % d
    return str(d.quantize(_CENT))


def canonical(value, key=None):
    """按字段语义规范化。**无 key 时也绝不丢精度。**

    - 金额字段（amount/total/...）：定标到分，`504` 与 `504.0` 同摘要；
    - 其它整数：原样保留整数精度（9007199254740992 ≠ 9007199254740993）；
    - 其它浮点：用 repr 保持全精度（1.0000001 ≠ 1.0000002）；
    - 字符串：**不再 strip**。首尾空白是内容的一部分，抹掉它等于允许
      「参数变了一点点也能复用旧批准」—— 那是把失败方向搞反了。
    """
    if isinstance(value, bool):
        return value                     # bool 是 int 子类，不能当数字抹平
    if isinstance(value, int):
        return _money(value) if key in AMOUNT_FIELDS else value
    if isinstance(value, float):
        if key in AMOUNT_FIELDS:
            return _money(value)
        if value != value or value in (float("inf"), float("-inf")):
            return "nonfinite:%r" % value
        return repr(value)
    if isinstance(value, decimal.Decimal):
        return _money(value) if key in AMOUNT_FIELDS else str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {k: canonical(value[k], k) for k in sorted(value)}
    if isinstance(value, list):
        return [canonical(v, key) for v in value]
    return value


def canonical_json(obj):
    return json.dumps({"v": CANONICAL_VERSION, "body": canonical(obj)},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def policy_digest(allowed_tools, tool_contract=None):
    """权限指纹：白名单或工具契约变了，旧审批立即失效。"""
    return digest({"allowed_tools": sorted(allowed_tools or []),
                   "tool_contract": tool_contract or {}})


def resource_scope_of(args):
    """从参数里摘出可核对的资源范围（订单号 / 路径等），写进审批记录。"""
    if not isinstance(args, dict):
        return ""
    parts = []
    for k in sorted(args):
        if k in ("order_id", "path", "resource", "account", "db"):
            parts.append("%s=%s" % (k, canonical(args[k])))
    return "|".join(parts)


class ApprovalGate:
    """统一的写操作执行门禁。首次写入与重放写入都调 `require()`。"""

    def __init__(self, store, ttl=DEFAULT_TTL, clock=None):
        self.store = store
        self.ttl = float(ttl)
        self.clock = clock or time.time
        self.ensure_schema()

    def ensure_schema(self):
        with self.store.conn() as c:
            c.executescript(SCHEMA)
            # 旧库升级：candidate_version 当年存了但没人比对，schema 版本同理。
            # 新列用 ALTER 加，不动老数据 —— 老审批保持「缺该字段」的事实，
            # 由 _version_mismatch 判为不可复用，而不是猜一个默认值补上。
            have = {r[1] for r in c.execute(
                "PRAGMA table_info(approval_requests)")}
            # EG-026：租户绑定、外部效果回执与对账状态。老库用 ALTER 补列，
            # 老审批保持「缺该字段」的事实（tenant_id=NULL 表示未绑定租户，
            # 由 approve() 决定是否放行），不猜默认值。
            for col in ("tool_schema_version", "tenant_id", "effect_status",
                        "effect_receipt"):
                if col not in have:
                    c.execute("ALTER TABLE approval_requests ADD COLUMN %s TEXT"
                              % col)

    # ---------- 查询 ----------
    def get(self, request_id):
        with self.store.conn() as c:
            r = c.execute("SELECT * FROM approval_requests WHERE id=?",
                          (request_id,)).fetchone()
        return dict(r) if r else None

    def list(self, run_id=None, status=None, limit=200):
        sql, args = "SELECT * FROM approval_requests WHERE 1=1", []
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT %d" % int(limit)
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def expire_stale(self):
        """把过期的 pending/approved 标成 expired，任务不会自动继续。"""
        now = self.clock()
        with self.store.conn() as c:
            c.execute(
                "UPDATE approval_requests SET status=?, decided_at=?, reason=? "
                "WHERE status IN (?,?) AND expires_at < ?",
                (EXPIRED, now, "审批过期（expires_at < now），任务不自动继续",
                 PENDING, APPROVED, now))

    # ---------- 状态迁移 ----------
    def _new_request(self, run_id, tool, args, *, resource_scope=None,
                     candidate_version=None, checkpoint_id=None,
                     idempotency_key=None, policy_digest_value=None,
                     first_write=False, tool_schema_version=None,
                     tenant_id=None):
        rid = core.new_id("appr")
        body = canonical_json(args)
        ad = digest(args)
        now = self.clock()
        key = idempotency_key or digest({"run_id": run_id, "tool": tool,
                                         "args": ad})
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO approval_requests(id,ts,run_id,tool,args_digest,"
                "args_canonical,resource_scope,candidate_version,idempotency_key,"
                "policy_digest,status,created_at,expires_at,approved_by,"
                "decided_at,consumed_at,checkpoint_id,reason,first_write,"
                "tool_schema_version,tenant_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, now, run_id, tool, ad, body,
                 resource_scope or resource_scope_of(args),
                 candidate_version, key, policy_digest_value, PENDING,
                 now, now + self.ttl, None, None, None, checkpoint_id,
                 "等待人工审批", 1 if first_write else 0, tool_schema_version,
                 tenant_id))
        return rid

    def require(self, run_id, tool, args, *, allowed_tools=None,
                tool_contract=None, candidate_version=None,
                checkpoint_id=None, idempotency_key=None, first_write=False,
                tool_schema_version=None, policy_digest_value=None,
                tenant_id=None):
        """唯一的执行门禁：通过就消费一次审批，否则抛 ApprovalRequired。

        调用方在**真正执行写操作之前**调它；首次写入与恢复期重放写入走同一条路。

        版本绑定（评审 R04）：调用方给了 candidate_version / idempotency_key，
        就必须和审批里存的那一份**逐项相等**才算可复用。以前只比对
        run_id/tool/args_digest，于是「用 v1 批的票」能消费 v2 的调用 ——
        旧批准悄悄覆盖了新的执行上下文。
        """
        self.expire_stale()
        pd = policy_digest_value or policy_digest(allowed_tools, tool_contract)
        ad = digest(args)

        # 找一条可复用的已批准请求：参数、权限、工具、run、版本、幂等键全要一致。
        with self.store.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM approval_requests WHERE run_id=? AND tool=? "
                "AND args_digest=? AND status=? ORDER BY created_at DESC",
                (run_id, tool, ad, APPROVED))]
        mismatch = None
        for r in rows:
            # EG-026：审批绑定租户。跨租户复用在企业模式下等于越权。
            if tenant_id and r.get("tenant_id") and r["tenant_id"] != tenant_id:
                raise ApprovalDenied(r["id"], "租户不匹配，原审批不能复用")
            if r["policy_digest"] and r["policy_digest"] != pd:
                raise ApprovalDenied(r["id"], "权限/工具契约已变更，原审批不能复用")
            if r["expires_at"] < self.clock():
                self._set(r["id"], EXPIRED, "审批过期")
                raise ApprovalDenied(r["id"], "审批已过期")
            if r["consumed_at"]:
                raise ApprovalDenied(r["id"], "审批已被消费，不能重复使用")
            bad = _version_mismatch(r, candidate_version, idempotency_key,
                                    tool_schema_version)
            if bad:
                mismatch = (r["id"], bad)
                continue
            got = self._consume_atomic(r["id"])
            if not got:
                # 另一个消费者先到了：条件更新没命中，不能假装自己拿到的。
                raise ApprovalDenied(r["id"], "审批已被其他消费者消费（状态迁移冲突）")
            return {"status": CONSUMED, "request_id": r["id"],
                    "checkpoint_id": r["checkpoint_id"],
                    "idempotency_key": r["idempotency_key"],
                    "candidate_version": r["candidate_version"]}
        if mismatch:
            # 有已批准的票，但绑的是另一个版本 —— 必须重新审批，不能静默继承。
            raise ApprovalDenied(mismatch[0], mismatch[1])

        # 没有可复用审批：先看有没有同一操作的待批票，有就复用，别越堆越多。
        with self.store.conn() as c:
            pend = [dict(r) for r in c.execute(
                "SELECT * FROM approval_requests WHERE run_id=? AND tool=? "
                "AND args_digest=? AND status=? ORDER BY created_at DESC",
                (run_id, tool, ad, PENDING))]
        for r in pend:
            if r["policy_digest"] and r["policy_digest"] != pd:
                # 权限/契约变了，旧待批票不能再被批准，先作废再重开。
                self._set(r["id"], EXPIRED, "权限/工具契约已变更，待批票作废")
                continue
            if _version_mismatch(r, candidate_version, idempotency_key,
                                 tool_schema_version):
                self._set(r["id"], EXPIRED, "候选版本/幂等键已变更，待批票作废")
                continue
            return self._raise(r["id"], tool, r["checkpoint_id"])

        if checkpoint_id is None:
            cp = self.store.latest_checkpoint(run_id)
            checkpoint_id = cp["id"] if cp else None
        rid = self._new_request(
            run_id, tool, args, candidate_version=candidate_version,
            checkpoint_id=checkpoint_id, idempotency_key=idempotency_key,
            policy_digest_value=pd, first_write=first_write,
            tool_schema_version=tool_schema_version, tenant_id=tenant_id)
        return self._raise(rid, tool, checkpoint_id)

    def _consume_atomic(self, request_id, expected=None):
        """条件更新：只有「仍是 approved 且没被消费过」才置 consumed。

        以前是先读状态、再在另一段连接里 `UPDATE ... WHERE id=?`，
        两个线程都能读到 approved，于是都返回 consumed（评审 R05）。
        rowcount 是唯一可信的「我抢到了」证据。
        """
        now = self.clock()
        with self.store.conn() as c:
            cur = c.execute(
                "UPDATE approval_requests SET status=?, consumed_at=?, "
                "decided_at=?, reason=? "
                "WHERE id=? AND status=? AND consumed_at IS NULL",
                (CONSUMED, now, now, "已消费一次，不可重复",
                 request_id, expected or APPROVED))
            return cur.rowcount == 1

    def _raise(self, rid, tool, checkpoint_id):
        raise ApprovalRequired(rid, checkpoint_id, tool)

    def _validate_reusable(self, r, tool, args, policy_digest_value):
        """显式复用某条审批时的全部校验：参数 / 权限 / 工具 / 过期 / 状态。"""
        if r is None:
            raise ApprovalDenied("", "审批请求不存在")
        if r["tool"] != tool:
            raise ApprovalDenied(r["id"], "工具不匹配，原审批不能复用")
        if r["args_digest"] != digest(args):
            raise ApprovalDenied(r["id"], "参数摘要不匹配，原审批不能复用")
        if r["policy_digest"] and policy_digest_value \
                and r["policy_digest"] != policy_digest_value:
            raise ApprovalDenied(r["id"], "权限/工具契约已变更，原审批不能复用")
        if r["expires_at"] < self.clock():
            self._set(r["id"], EXPIRED, "审批过期")
            raise ApprovalDenied(r["id"], "审批已过期")
        if r["status"] == EXPIRED or r["status"] == REJECTED:
            raise ApprovalDenied(r["id"], "审批状态为 %s，任务不自动继续" % r["status"])
        if r["status"] != APPROVED or r["consumed_at"]:
            raise ApprovalDenied(r["id"], "审批已消费或状态不是 approved，不能重复使用")

    def consume(self, request_id, *, run_id, tool, args,
                allowed_tools=None, tool_contract=None,
                candidate_version=None, idempotency_key=None,
                tool_schema_version=None):
        """显式按 request_id 复用审批（一次性）。所有不匹配都拒绝。"""
        self.expire_stale()
        r = self.get(request_id)
        self._validate_reusable(r, tool, args,
                                policy_digest(allowed_tools, tool_contract))
        if r["run_id"] != run_id:
            raise ApprovalDenied(request_id, "run_id 不匹配，原审批不能复用")
        bad = _version_mismatch(r, candidate_version, idempotency_key,
                                tool_schema_version)
        if bad:
            raise ApprovalDenied(request_id, bad)
        if not self._consume_atomic(request_id):
            raise ApprovalDenied(request_id,
                                 "审批已被其他消费者消费（状态迁移冲突）")
        return {"status": CONSUMED, "request_id": request_id,
                "checkpoint_id": r["checkpoint_id"],
                "idempotency_key": r["idempotency_key"],
                "candidate_version": r["candidate_version"]}

    def approve(self, request_id, approved_by="human", tenant_id=None):
        self.expire_stale()
        r = self.get(request_id)
        if not r:
            raise ApprovalDenied(request_id, "审批请求不存在")
        # EG-026：审批人必须属于同一租户。企业模式下这是「租户级审批人绑定」
        # 的最小实现；真实 IdP 的角色解析仍然没有（见台账 EG-026）。
        if tenant_id and r.get("tenant_id") and r["tenant_id"] != tenant_id:
            raise ApprovalDenied(
                request_id, "审批人租户 %s 与请求租户 %s 不一致"
                % (tenant_id, r["tenant_id"]))
        with self.store.conn() as c:
            # 条件迁移：只有 pending 能变 approved。并发下第二个会 rowcount=0。
            cur = c.execute(
                "UPDATE approval_requests SET status=?, approved_by=?, "
                "decided_at=?, reason=? WHERE id=? AND status=?",
                (APPROVED, approved_by, self.clock(),
                 "批准人 %s 已批准" % approved_by, request_id, PENDING))
            n = cur.rowcount
        if n != 1:
            raise ApprovalDenied(request_id,
                                 "状态为 %s，只有 pending 可以批准" % r["status"])
        return self.get(request_id)

    def reject(self, request_id, reason="人工拒绝"):
        r = self.get(request_id)
        if not r:
            raise ApprovalDenied(request_id, "审批请求不存在")
        with self.store.conn() as c:
            cur = c.execute(
                "UPDATE approval_requests SET status=?, decided_at=?, reason=? "
                "WHERE id=? AND status IN (?,?)",
                (REJECTED, self.clock(), reason, request_id, PENDING, APPROVED))
            n = cur.rowcount
        if n != 1:
            raise ApprovalDenied(request_id,
                                 "状态为 %s，不能再拒绝" % r["status"])
        return self.get(request_id)

    def mark_effect(self, request_id, ok=True, receipt=None):
        """记录外部效果回执（EG-026）。

        本地 SQLite 只能证明「审批被消费了」，证明不了「外部系统真的执行了」。
        调用方在外部动作成功后必须回填回执；没回填的会被 `reconcile()` 列出。
        """
        with self.store.conn() as c:
            cur = c.execute(
                "UPDATE approval_requests SET effect_status=?, effect_receipt=? "
                "WHERE id=? AND status=?",
                ("succeeded" if ok else "failed", receipt, request_id, CONSUMED))
            return cur.rowcount == 1

    def reconcile(self, tenant_id=None, limit=200):
        """已消费但**没有效果回执**的审批：崩溃后可能「执行了但没记账」。

        这一层不自动重放（外部副作用可能已经发生），只负责把它们列出来
        让人对账 —— 与作业队列的 `unknown` 是同一个原则。
        """
        sql = ("SELECT * FROM approval_requests WHERE status=? "
               "AND effect_status IS NULL")
        args = [CONSUMED]
        if tenant_id:
            sql += " AND tenant_id=?"
            args.append(tenant_id)
        sql += " ORDER BY consumed_at ASC LIMIT ?"
        args.append(int(limit))
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def _set(self, request_id, status, reason):
        with self.store.conn() as c:
            c.execute("UPDATE approval_requests SET status=?, decided_at=?, "
                      "reason=? WHERE id=?",
                      (status, self.clock(), reason, request_id))

    def resume(self, run_id):
        """进程重启后从原检查点恢复；返回检查点与待处理审批，不自动执行。"""
        cp = self.store.latest_checkpoint(run_id)
        return {"checkpoint": cp,
                "pending": self.list(run_id, PENDING),
                "approved": self.list(run_id, APPROVED),
                "note": "恢复只还原检查点与审批状态，不自动继续写操作"}


class SettlementLedger:
    """专用 SQLite 结算表：幂等键唯一，重复提交只保留一笔。"""

    def __init__(self, store):
        self.store = store
        with store.conn() as c:
            c.executescript(SCHEMA)

    def settle(self, idempotency_key, *, run_id, order_id, total,
               rule_version, payload=None):
        """幂等结算：同键同内容只落一笔；同键不同内容视为冲突，不静默通过。"""
        now = core.now()
        total = float(total)
        with self.store.conn() as c:
            prev = c.execute(
                "SELECT * FROM settlement_ledger WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if prev is not None:
                same_total = abs(float(prev["total"]) - total) < 1e-6
                if prev["order_id"] != order_id or not same_total:
                    raise LedgerConflict(
                        idempotency_key,
                        "幂等键 %s 已用于另一笔结算（订单 %s/金额 %s），"
                        "本次为订单 %s/金额 %s：金额或订单不一致，拒绝静默覆盖"
                        % (idempotency_key, prev["order_id"], prev["total"],
                           order_id, total))
                return False, dict(prev)
            cur = c.execute(
                "INSERT OR IGNORE INTO settlement_ledger("
                "idempotency_key,run_id,order_id,total,rule_version,created_at,payload) "
                "VALUES(?,?,?,?,?,?,?)",
                (idempotency_key, run_id, order_id, float(total), rule_version,
                 now, canonical_json(payload or {})))
            created = cur.rowcount > 0
            r = c.execute(
                "SELECT * FROM settlement_ledger WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
        return created, dict(r) if r else None

    def count(self, order_id=None):
        sql = "SELECT COUNT(*) n FROM settlement_ledger"
        args = ()
        if order_id:
            sql += " WHERE order_id=?"
            args = (order_id,)
        with self.store.conn() as c:
            return c.execute(sql, args).fetchone()["n"]

    def rows(self):
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM settlement_ledger ORDER BY created_at")]
