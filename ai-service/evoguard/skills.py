# -*- coding: utf-8 -*-
"""技能层：候选技能 → 独立验证 → 晋升 / 拒绝 / 回滚，以及负迁移检测。

四条硬规则（规格第十一部分「严禁」逐条落实）：
  1. 候选技能**不得覆盖**旧版本，只能新增版本，晋升才生效
  2. 技能只能声明"遇到 X 用 Y 策略"，**不得**携带可执行代码
  3. 晋升必须通过独立验证集，且必须同时检查「有没有把原本能做的事弄坏」
  4. 评测标准在代码里固定，技能自己改不了

P0 03：第 3 条此前只靠「调用方自觉先 evaluate」维持，promote 自身不核对。
本模块现在把评估记录、候选正文指纹、验证集指纹和当前指针绑在同一个事务里，
没有有效评估的晋升请求直接被拒；重复请求返回已启用的版本而不继续升版本。
"""
import copy
import hashlib
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core      # noqa: E402
import experience as exp_mod  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills(
  id TEXT PRIMARY KEY, name TEXT, task_type TEXT, fault_type TEXT, strategy TEXT,
  required_tools TEXT, safety TEXT, status TEXT, version INTEGER, created_at REAL,
  scope TEXT DEFAULT 'single', active_version INTEGER,
  promoted_from_evaluation TEXT, promoted_at REAL,
  decision_params TEXT, disabled_at REAL,
  rolled_back_from_version INTEGER, rolled_back_at REAL,
  error_signature TEXT, diagnostic_steps TEXT,
  fix_pattern TEXT, verification_check TEXT);

CREATE TABLE IF NOT EXISTS skill_versions(
  id TEXT PRIMARY KEY, skill_id TEXT, version INTEGER, snapshot TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS skill_rollback_audit(
  id TEXT PRIMARY KEY, skill_id TEXT, ts REAL, reasons TEXT,
  baseline TEXT, online TEXT);

CREATE TABLE IF NOT EXISTS skill_evaluations(
  id TEXT PRIMARY KEY, skill_id TEXT, target_n INTEGER, before_rate REAL, after_rate REAL,
  gain_pp REAL, guard_n INTEGER, neg_transfer_rate REAL, verdict TEXT, created_at REAL,
  protocol_version TEXT, details TEXT, candidate_hash TEXT, validation_hash TEXT,
  consumed_by TEXT, consumed_at REAL);
"""

CANDIDATE, ACTIVE, REJECTED, NEEDS_REVIEW, DISABLED = (
    "candidate", "active", "rejected", "needs_review", "disabled")

# 技能作用域：这条经验是「一个坏点」学来的，还是「两个坏点」学来的。
# 为什么必须分开：技能只按 (agent, fault_type) 建索引，而复合故障的根因是
# 它链路里的**第一个**故障 —— 于是复合学到的两步链会被挂到单故障的键上。
# 实测后果很具体：kb/retrieval_failure 上被放进一条两步链后，
# 单故障 retrieval_failure 的 6 个用例全挂了（本该用一步 fallback_tool）。
# 这不是「负迁移难测」，是键里少了一个维度。
SINGLE, MULTI = "single", "multi"

# 晋升门槛（固定在这里，技能与模型都改不了）
MIN_GAIN_PP = 15.0
MAX_NEG_TRANSFER = 0.05
VALIDATION_PROTOCOL = "paired-v2"
MIN_VALIDATION_TASKS = 3  # 工程证据下限，不是统计安全保证

# 评估有效期：数据集、prompt、模型或工具后端换过之后，旧一次 gain/neg
# 已经不描述当前候选，不能拿它当今天的晋升证据。
MAX_EVALUATION_AGE_S = 24 * 3600
# 门禁自身的版本：评估明细里的字段布局变了，旧明细就不能再被新门禁读取。
PROMOTION_PROTOCOL = "promotion-gate-v1"

# 技能「正文」= 决定执行行为的全部字段。status / version / 时间戳刻意排除：
# 晋升本身会改它们，纳入哈希会让同一评估在幂等重放时立刻失效。
CONTENT_FIELDS = ("name", "task_type", "fault_type", "strategy",
                  "required_tools", "safety", "scope", "decision_params",
                  "error_signature", "diagnostic_steps", "fix_pattern",
                  "verification_check")

PROMOTION_ERRORS = {
    "NO_EVALUATION": "该技能没有任何评估记录，必须先跑 paired-v2 独立验证",
    "EVALUATION_NOT_FOUND": "指定的评估记录不存在",
    "EVALUATION_SKILL_MISMATCH": "评估记录属于另一条技能",
    "EVALUATION_PROTOCOL_MISMATCH": "评估协议与当前门槛不一致，历史 NULL 协议不得冒充新版",
    "EVALUATION_DETAILS_MISSING": "评估记录缺少 paired-v2 明细，无法核对配对与样本",
    "EVALUATION_VERDICT": "评估判决不是 PROMOTE",
    "EVALUATION_EXPIRED": "评估已过期，需在当前数据集 / 模型 / 工具后端下重跑",
    "EVALUATION_ALREADY_CONSUMED": "该评估已用于另一次晋升，且当前指针不在那个版本",
    "EVALUATION_PAIR_MISMATCH": "成对明细与评估记录不一致，明细可能被改动",
    "EVALUATION_HASH_MISMATCH": "验证集指纹与成对明细不一致",
    "EVALUATION_SAMPLE_INSUFFICIENT": "独立任务、baseline 成功样本或门槛不满足",
    "CANDIDATE_CONTENT_CHANGED": "评估之后候选技能正文被改动，旧评估失效",
    "SKILL_STATUS_REJECTED": "已拒绝的技能不得晋升",
    "MISSING_VERSION_SNAPSHOT": "当前版本缺不可变快照，不能追加新版本",
    "SKILL_NOT_FOUND": "技能不存在",
}


class PromotionRejected(ValueError):
    """晋升被拒。code 可定位，message 是逐项核对后的可读失败说明。"""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__("[%s] %s" % (code, message or PROMOTION_ERRORS.get(code, code)))


def ensure_schema(store):
    exp_mod.ensure_schema(store)      # evolution_events 在经验模块里，一起建
    with store.conn() as c:
        c.executescript(SCHEMA)
        # 老库没有 scope 列，补上（幂等）。已有技能默认算 single ——
        # 它们大多是单故障经验；复合链会在重新训练时带上正确的作用域。
        cols = [r[1] for r in c.execute("PRAGMA table_info(skills)").fetchall()]
        if "scope" not in cols:
            c.execute("ALTER TABLE skills ADD COLUMN scope TEXT DEFAULT 'single'")
        for name, decl in (("active_version", "INTEGER"),
                           ("promoted_from_evaluation", "TEXT"),
                           ("promoted_at", "REAL"),
                           ("decision_params", "TEXT"),
                           ("disabled_at", "REAL"),
                           ("rolled_back_from_version", "INTEGER"),
                           ("rolled_back_at", "REAL")):
            if name not in cols:
                c.execute("ALTER TABLE skills ADD COLUMN %s %s" % (name, decl))
        cols = [r[1] for r in c.execute("PRAGMA table_info(skill_evaluations)")]
        # NULL 明确表示历史协议，不能给旧结果补上新版协议标签。
        for name, decl in (("protocol_version", "TEXT"), ("details", "TEXT"),
                           ("candidate_hash", "TEXT"), ("validation_hash", "TEXT"),
                           ("consumed_by", "TEXT"), ("consumed_at", "REAL")):
            if name not in cols:
                c.execute("ALTER TABLE skill_evaluations ADD COLUMN %s %s" % (name, decl))


def propose(exp):
    """从一条已验证经验生成候选技能。声明式，不含任何可执行代码。

    strategy 允许是**策略链**（list），落库时序列化成 JSON 字符串；
    单策略仍存裸字符串，保证旧版技能与测试的行为完全一致。
    技能永远只声明「遇到 X 用哪几步策略」，不携带任何可执行代码。
    """
    chain = core.as_chain(exp["recovery_action"])
    stored = core.dumps(chain) if len(chain) > 1 else (chain[0] if chain else "")
    # 作用域取自经验本身：一次运行里诊断出几个坏点，就决定了这条技能能在
    # 「第几轮恢复」被命中。链长>1 但只有一个坏点时仍是 single。
    scope = exp.get("fault_scope") or (MULTI if len(chain) > 1 else SINGLE)
    return {
        "id": "sk_" + uuid.uuid4().hex[:10],
        "name": "遇到 %s 时按序执行 %s" % (exp["failure_type"], " -> ".join(chain) or "-"),
        "task_type": exp["task_type"],
        "fault_type": exp["failure_type"],
        "strategy": stored,
        "strategy_chain": chain,
        "scope": scope,
        "required_tools": [],
        "safety": {"max_retry": 2, "require_approval_for_write": True,
                   "max_chain": 3},
        "status": CANDIDATE,
        "version": 1,
        "created_at": round(time.time(), 3),
        "from_experience": exp["id"],
    }


def _db_value(value):
    """正文里的结构字段落库前统一成 JSON 字符串，字符串策略保持原样。"""
    if isinstance(value, (list, tuple, dict)):
        return core.dumps(value)
    return value


def _snapshot(skill, strategy=None):
    """构造不可变快照：正文 + 版本/状态元数据，字段齐全才谈得上完整回滚。

    快照一旦写入 skill_versions 就不再修改；回滚读的是它，不是当前行。
    """
    snap = dict(skill)
    if strategy is not None:
        snap["strategy"] = strategy
    snap.setdefault("scope", SINGLE)
    snap.setdefault("decision_params", None)
    snap.setdefault("active_version", None)
    return snap


def save(store, skill):
    ensure_schema(store)
    strategy = _db_value(skill["strategy"])
    with store.conn() as c:
        c.execute("INSERT INTO skills(id,name,task_type,fault_type,strategy,required_tools,"
                  "safety,status,version,created_at,scope,decision_params,"
                  "error_signature,diagnostic_steps,fix_pattern,verification_check) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (skill["id"], skill["name"], skill["task_type"], skill["fault_type"],
                   strategy, _db_value(skill["required_tools"]),
                   _db_value(skill["safety"]), skill["status"], skill["version"],
                   skill["created_at"], skill.get("scope") or SINGLE,
                   _db_value(skill.get("decision_params")),
                   skill.get("error_signature"),
                   _db_value(skill.get("diagnostic_steps")),
                   skill.get("fix_pattern"),
                   skill.get("verification_check")))
        c.execute("INSERT INTO skill_versions(id,skill_id,version,snapshot,created_at) "
                  "VALUES(?,?,?,?,?)",
                  ("skv_" + uuid.uuid4().hex[:8], skill["id"], skill["version"],
                   core.dumps(_snapshot(skill, strategy)), skill["created_at"]))
    exp_mod.event(store, "skill_proposed", skill["id"], skill["name"])
    return skill


def lookup(store, agent_type, fault_type, mode, scope=SINGLE):
    """恢复期查技能。这是 A/B/C/D 四组差异的关键开关。

    D：只认 active 技能，且必须 **agent + 故障类型都匹配**（严格）
    C：认 candidate + active，且 **只按 agent 匹配**（不严格 → 经验被过度泛化 → 负迁移）

    scope 是「现在是第几轮恢复」：
      第 1 轮（single）优先单故障技能，第 2 轮起（multi）优先多故障技能。
      只按 prefer 排序、不硬过滤 —— 多故障场景下一条单步策略仍可能是正确的第二步。
      硬过滤会让「单故障经验在复合场景下彻底失效」，那是另一个方向的错误。

    排序必须给足次序（prefer → version → created_at → rowid）：
    C 只看 agent，同一 agent 下必然有多条技能并列（技能库里的 version 通常都是 2）。
    只写 `ORDER BY version DESC` 时，SQLite 对并列行的取舍取决于扫描顺序，
    实测就是「永远取到最早插入的那条」—— 这会让「C 的成功率」变成
    技能库插入顺序的产物，而不是治理机制的效果。同一个 bug 之前在
    get_recovery 里踩过（毫秒并列取到链中间的失败步），这里用同样的兜底。
    """
    ensure_schema(store)
    ORDER = ("ORDER BY (scope=?) DESC, version DESC, created_at DESC, rowid DESC LIMIT 1")
    if mode == "D":
        sql = ("SELECT * FROM skills WHERE status=? AND task_type=? AND fault_type=? "
               + ORDER)
        args = (ACTIVE, agent_type, fault_type, scope)
    elif mode == "C":
        sql = ("SELECT * FROM skills WHERE status IN (?,?) AND task_type=? " + ORDER)
        args = (CANDIDATE, ACTIVE, agent_type, scope)
    else:
        return None
    with store.conn() as c:
        r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def _task_key(case):
    agent, task = case.get("agent_type"), case.get("task_id")
    if not agent or not task:
        raise ValueError("validation case requires agent_type and task_id")
    return (agent, task)


def _family_key(case):
    # 老数据没有 family_id 时，最多只能证明 task_id 不同。
    return ("family", case["family_id"]) if case.get("family_id") else _task_key(case)


def _case_key(case):
    return core.dumps({"task": _task_key(case), "fault_type": case.get("fault_type"),
                       "fault_chain": case.get("fault_chain") or [],
                       "fault_target": case.get("fault_target", 0),
                       "seed": case.get("seed", 7)})


def _canon(value):
    """把「内存对象」和「落库后的 JSON 字符串」归一到同一种结构。

    evaluate 收到的是 dict/list，promote 从 DB 读回的是字符串；不归一，
    同一个候选在验证时和晋升时会算出两个哈希，门禁永远误报正文改动。
    只解析看起来像 JSON 容器的字符串，避免把普通策略名 "123" 变成数字。
    """
    if isinstance(value, str):
        if value.strip()[:1] in ("[", "{", '"'):
            value = core.loads(value, value)
        else:
            return value
    if isinstance(value, (list, tuple)):
        return [_canon(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _canon(value[k]) for k in sorted(value)}
    return value


def content_hash(skill):
    """技能正文指纹。内存候选与 DB 行必须算出同一个值。"""
    body = {}
    for key in CONTENT_FIELDS:
        v = skill.get(key)
        if key == "scope":
            v = v or SINGLE
        body[key] = _canon(v)
    return hashlib.sha256(core.dumps(body).encode("utf-8")).hexdigest()


def _validation_cases(target_cases, guard_cases, training_cases):
    return {"target": [_case_key(c) for c in target_cases],
            "guard": [_case_key(c) for c in guard_cases],
            "training": sorted(_case_key(c) for c in training_cases)}


def validation_hash(validation_cases):
    return hashlib.sha256(core.dumps(validation_cases).encode("utf-8")).hexdigest()


def _outcome(result):
    """兼容旧 bool 和 (bool, run_id)，新适配器可提供 cost 字典。"""
    if isinstance(result, dict):
        success, rid, cost = result.get("success"), result.get("run_id"), result.get("cost")
    elif isinstance(result, tuple) and len(result) == 2:
        success, rid = result
        cost = None
    else:
        success, rid, cost = result, None, None
    if not isinstance(success, (bool, int)) or success not in (0, 1):
        raise ValueError("run_case must return an explicit boolean success")
    return bool(success), rid, cost


def evaluate(store, skill, target_cases, guard_cases, run_case, *, training_cases=()):
    """同 case/seed 成对验证。空分母不可估计；不足三项独立任务不得自动晋升。

    training_cases 用于排除训练任务/家族，两个正式入口必须传入。
    回调返回 bool、(bool, run_id) 或 {success, run_id, cost}。
    """
    target_cases, guard_cases = list(target_cases), list(guard_cases)
    training_cases = list(training_cases)
    seen_ids, seen_keys = set(), set()
    train_tasks = {_task_key(c) for c in training_cases}
    train_families = {_family_key(c) for c in training_cases}
    for case in target_cases + guard_cases:
        key = _case_key(case)
        cid = case.get("case_id") or "case_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if cid in seen_ids:
            raise ValueError("duplicate case_id in validation: %s" % cid)
        if key in seen_keys:
            raise ValueError("duplicate validation recipe: %s" % cid)
        if _task_key(case) in train_tasks or _family_key(case) in train_families:
            raise ValueError("validation overlaps training task/family: %s" % cid)
        seen_ids.add(cid)
        seen_keys.add(key)

    ensure_schema(store)
    pairs = []
    for group, cases in (("target", target_cases), ("guard", guard_cases)):
        for case in cases:
            key = _case_key(case)
            cid = case.get("case_id") or "case_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            # 每一侧独立拷贝，防止运行器修改 recipe 或候选，破坏配对。
            before, bid, bcost = _outcome(run_case(copy.deepcopy(case), None))
            after, aid, acost = _outcome(run_case(copy.deepcopy(case), copy.deepcopy(skill)))
            pairs.append({"group": group, "case_id": cid, "case": copy.deepcopy(case),
                          "seed": case.get("seed", 7), "fault_type": case.get("fault_type"),
                          "baseline_success": before, "candidate_success": after,
                          "baseline_run_id": bid, "candidate_run_id": aid,
                          "baseline_cost": bcost, "candidate_cost": acost})

    target = [p for p in pairs if p["group"] == "target"]
    guard = [p for p in pairs if p["group"] == "guard"]
    t_n, g_n = len(target), len(guard)
    before_rate = sum(p["baseline_success"] for p in target) / t_n if t_n else None
    after_rate = sum(p["candidate_success"] for p in target) / t_n if t_n else None
    gain_pp = (after_rate - before_rate) * 100 if t_n else None
    g_before = sum(p["baseline_success"] for p in guard)
    regressed = sum(p["baseline_success"] and not p["candidate_success"] for p in guard)
    improved = sum(not p["baseline_success"] and p["candidate_success"] for p in guard)
    retained = sum(p["baseline_success"] and p["candidate_success"] for p in guard)
    both_failed = g_n - regressed - improved - retained
    neg = regressed / g_before if g_before else None
    target_tasks = len({_family_key(c) for c in target_cases})
    guard_tasks = len({_family_key(c) for c in guard_cases})
    successful_guard_tasks = len({_family_key(p["case"]) for p in guard if p["baseline_success"]})
    reasons = []
    if target_tasks < MIN_VALIDATION_TASKS:
        reasons.append("target has fewer than 3 independent tasks/families")
    if guard_tasks < MIN_VALIDATION_TASKS:
        reasons.append("guard has fewer than 3 independent tasks/families")
    if successful_guard_tasks < MIN_VALIDATION_TASKS:
        reasons.append("guard has fewer than 3 baseline-success tasks/families")
    if neg is None:
        reasons.append("negative transfer is unestimable: no baseline guard successes")

    # 使用未舍入值判门槛；已观察到的过量退化优先拒绝。
    if neg is not None and neg > MAX_NEG_TRANSFER:
        verdict = "REJECT"
        reasons.append("paired negative transfer exceeds threshold")
    elif reasons or gain_pp is None:
        verdict = "NEEDS_REVIEW"
    elif gain_pp <= 0:
        verdict = "REJECT"
        reasons.append("no target gain")
    elif gain_pp >= MIN_GAIN_PP:
        verdict = "PROMOTE"
    else:
        verdict = "NEEDS_REVIEW"
        reasons.append("target gain below promotion threshold")

    # 评估绑定的是「这一份候选正文」和「这一组验证题」。晋升时重新指纹比对，
    # 正文被改、验证题被换或配对明细被篡改都会让旧评估失效。
    vc = _validation_cases(target_cases, guard_cases, training_cases)
    vh = validation_hash(vc)
    ch = content_hash(skill)
    ts = core.now()
    result = {"id": "ske_" + uuid.uuid4().hex[:10], "skill_id": skill["id"],
              "protocol_version": VALIDATION_PROTOCOL,
              "promotion_protocol": PROMOTION_PROTOCOL, "created_at": ts,
              "candidate_hash": ch, "validation_hash": vh, "validation_cases": vc,
              "gain_pp": gain_pp, "before_rate": before_rate, "after_rate": after_rate,
              "neg_transfer_rate": neg, "verdict": verdict, "reasons": reasons,
              "target_n": t_n, "guard_n": g_n, "target_tasks": target_tasks,
              "guard_tasks": guard_tasks, "guard_baseline_success": g_before,
              "guard_baseline_success_tasks": successful_guard_tasks,
              "guard_regressions": regressed, "guard_improvements": improved,
              "guard_retained": retained, "guard_both_failed": both_failed,
              "guard_regression_fraction": regressed / g_n if g_n else None,
              "training_overlap_checked": bool(training_cases),
              "thresholds": {"min_gain_pp": MIN_GAIN_PP,
                             "max_neg_transfer": MAX_NEG_TRANSFER,
                             "min_validation_tasks": MIN_VALIDATION_TASKS},
              "pairs": pairs}
    with store.conn() as c:
        c.execute("INSERT INTO skill_evaluations(id,skill_id,target_n,before_rate,after_rate,"
                  "gain_pp,guard_n,neg_transfer_rate,verdict,created_at,protocol_version,"
                  "details,candidate_hash,validation_hash,consumed_by,consumed_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (result["id"], skill["id"], t_n, before_rate, after_rate, gain_pp,
                   g_n, neg, verdict, ts, VALIDATION_PROTOCOL, core.dumps(result),
                   ch, vh, None, None))
    exp_mod.event(store, "skill_evaluated", skill["id"],
                  "%s gain=%spp neg=%s verdict=%s" % (VALIDATION_PROTOCOL, gain_pp, neg, verdict))
    return result


def _load_evaluation(c, skill_id, evaluation_id):
    """取本次晋升要核对的评估。不指定 id 时取该技能最近一条。"""
    if evaluation_id:
        r = c.execute("SELECT * FROM skill_evaluations WHERE id=?",
                      (evaluation_id,)).fetchone()
        if not r:
            raise PromotionRejected("EVALUATION_NOT_FOUND",
                                    "找不到评估记录 %s" % evaluation_id)
        ev = dict(r)
        if ev.get("skill_id") != skill_id:
            raise PromotionRejected("EVALUATION_SKILL_MISMATCH",
                                    "评估 %s 属于技能 %s，不是 %s"
                                    % (evaluation_id, ev.get("skill_id"), skill_id))
        return ev
    r = c.execute("SELECT * FROM skill_evaluations WHERE skill_id=? "
                  "ORDER BY created_at DESC, rowid DESC LIMIT 1", (skill_id,)).fetchone()
    if not r:
        raise PromotionRejected("NO_EVALUATION", "技能 %s 从未经过独立验证" % skill_id)
    return dict(r)


def _close(a, b):
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def _audit_promotion(row, ev, now_s, versions):
    """逐项核对晋升前提，返回可定位、可打印的结构化结果。

    不信任评估记录里的汇总数字：独立任务数、负迁移率、增益全部从 pairs
    重算再与记录比对。只把 verdict 列改成 PROMOTE 是放不了行的。
    """
    checks, failed = [], []

    def add(code, name, ok, expected, actual):
        ok = bool(ok)
        checks.append({"code": code, "name": name, "ok": ok,
                       "expected": str(expected), "actual": str(actual)})
        if not ok:
            failed.append(code)

    details = core.loads(ev.get("details"), None)
    details = details if isinstance(details, dict) else None
    d = details or {}
    pairs = d.get("pairs") or []
    vc = d.get("validation_cases") or {}
    t_pairs = [p for p in pairs if p.get("group") == "target"]
    g_pairs = [p for p in pairs if p.get("group") == "guard"]

    # 同一评估 + 同一指针 + 已启用 = 重放请求，不是新一次晋升。
    replay = (bool(ev.get("consumed_by"))
              and str(ev["consumed_by"]) == str(row["version"])
              and row["status"] == ACTIVE)

    add("EVALUATION_SKILL_MISMATCH", "skill_id 归属",
        ev.get("skill_id") == row["id"], row["id"], ev.get("skill_id"))
    add("EVALUATION_PROTOCOL_MISMATCH", "评估协议",
        ev.get("protocol_version") == VALIDATION_PROTOCOL
        and d.get("protocol_version") == VALIDATION_PROTOCOL
        and d.get("promotion_protocol") == PROMOTION_PROTOCOL,
        "%s / %s" % (VALIDATION_PROTOCOL, PROMOTION_PROTOCOL),
        "%s / %s" % (ev.get("protocol_version"), d.get("promotion_protocol")))
    add("EVALUATION_DETAILS_MISSING", "paired-v2 明细",
        bool(details) and bool(pairs), "非空 details.pairs",
        "%d 对" % len(pairs) if details else "无明细（历史记录）")
    add("EVALUATION_VERDICT", "评估判决",
        ev.get("verdict") == "PROMOTE" and d.get("verdict") == "PROMOTE",
        "PROMOTE", ev.get("verdict"))

    age = now_s - float(ev.get("created_at") or 0)
    expiry_ok = 0 <= age <= MAX_EVALUATION_AGE_S
    add("EVALUATION_EXPIRED", "评估有效期", expiry_ok or replay,
        "0 ~ %d 秒" % MAX_EVALUATION_AGE_S,
        "%.0f 秒%s" % (age, "（幂等重放）" if replay else ""))

    cur_ver = int(row["version"])
    add("SKILL_STATUS_REJECTED", "旧版本状态", row["status"] != REJECTED,
        "非 rejected", row["status"])
    add("MISSING_VERSION_SNAPSHOT", "当前版本不可变快照",
        cur_ver in versions, "v%d 快照存在" % cur_ver, sorted(versions))
    if not replay:
        add("EVALUATION_ALREADY_CONSUMED", "评估未被其他版本消费",
            not ev.get("consumed_by"), "未消费", ev.get("consumed_by") or "未消费")

    now_ch = content_hash(row)
    recorded_ch = d.get("candidate_hash")
    add("CANDIDATE_CONTENT_CHANGED", "候选正文指纹",
        recorded_ch is not None and recorded_ch == now_ch
        and ev.get("candidate_hash") == now_ch,
        now_ch, recorded_ch)

    recorded_vh = d.get("validation_hash")
    try:
        vh_ok = (isinstance(vc, dict) and bool(vc) and recorded_vh is not None
                 and validation_hash(vc) == recorded_vh
                 and ev.get("validation_hash") == recorded_vh)
    except (TypeError, ValueError):
        vh_ok = False
    add("EVALUATION_HASH_MISMATCH", "验证集指纹", vh_ok,
        recorded_vh, validation_hash(vc) if isinstance(vc, dict) else "非字典")

    measured = {}
    try:
        if not isinstance(vc, dict):
            raise TypeError("validation_cases 不是字典")
        rebuilt_t = [_case_key(p["case"]) for p in t_pairs]
        rebuilt_g = [_case_key(p["case"]) for p in g_pairs]
        pair_intact = (rebuilt_t == vc.get("target") and rebuilt_g == vc.get("guard")
                       and len(t_pairs) == d.get("target_n")
                       and len(g_pairs) == d.get("guard_n")
                       and all(isinstance(p.get("baseline_success"), bool)
                               and isinstance(p.get("candidate_success"), bool)
                               for p in pairs))
        target_tasks = len({_family_key(p["case"]) for p in t_pairs})
        guard_tasks = len({_family_key(p["case"]) for p in g_pairs})
        ok_guard_tasks = len({_family_key(p["case"]) for p in g_pairs
                              if p["baseline_success"]})
        g_before = sum(1 for p in g_pairs if p["baseline_success"])
        regressed = sum(1 for p in g_pairs
                        if p["baseline_success"] and not p["candidate_success"])
        neg = regressed / g_before if g_before else None
        t_n = len(t_pairs)
        b_rate = sum(1 for p in t_pairs if p["baseline_success"]) / t_n if t_n else None
        a_rate = sum(1 for p in t_pairs if p["candidate_success"]) / t_n if t_n else None
        gain_pp = (a_rate - b_rate) * 100 if t_n else None
        th = d.get("thresholds") or {}
        sample_ok = bool(
            pair_intact and d.get("training_overlap_checked")
            and target_tasks >= MIN_VALIDATION_TASKS
            and guard_tasks >= MIN_VALIDATION_TASKS
            and ok_guard_tasks >= MIN_VALIDATION_TASKS
            and d.get("target_tasks") == target_tasks
            and d.get("guard_tasks") == guard_tasks
            and d.get("guard_baseline_success_tasks") == ok_guard_tasks
            and _close(d.get("neg_transfer_rate"), ev.get("neg_transfer_rate"))
            and _close(d.get("gain_pp"), ev.get("gain_pp"))
            # 重算值与记录值必须一致：改了某一对的成功状态却不动汇总列，
            # 也必须被拦下，不能只靠 details 与 SQL 列互相印证。
            and _close(d.get("neg_transfer_rate"), neg)
            and _close(d.get("gain_pp"), gain_pp)
            and d.get("guard_baseline_success") == g_before
            and d.get("guard_regressions") == regressed
            and neg is not None and neg <= MAX_NEG_TRANSFER
            and gain_pp is not None and gain_pp >= MIN_GAIN_PP
            and th.get("min_gain_pp") == MIN_GAIN_PP
            and th.get("max_neg_transfer") == MAX_NEG_TRANSFER
            and th.get("min_validation_tasks") == MIN_VALIDATION_TASKS)
        measured = {"target_tasks": target_tasks, "guard_tasks": guard_tasks,
                    "guard_baseline_success_tasks": ok_guard_tasks,
                    "neg_transfer_rate": neg, "gain_pp": gain_pp,
                    "pairs_intact": bool(pair_intact)}
    except (KeyError, TypeError, ValueError, AttributeError):
        pair_intact, sample_ok = False, False

    add("EVALUATION_PAIR_MISMATCH", "成对明细自洽", pair_intact,
        "target/guard 题目、数量与两侧布尔结果和记录一致",
        "%d target + %d guard" % (len(t_pairs), len(g_pairs)))
    add("EVALUATION_SAMPLE_INSUFFICIENT", "样本充分性与门槛", sample_ok,
        "target/guard/成功 guard 各 ≥ %d 独立任务，neg ≤ %s，gain ≥ %spp，"
        "训练验证重叠已检查"
        % (MIN_VALIDATION_TASKS, MAX_NEG_TRANSFER, MIN_GAIN_PP),
        measured or "无法从成对明细重算")

    return {"ok": not failed, "code": failed[0] if failed else None,
            "failed_codes": failed,
            "idempotent": bool(replay and not failed), "version": cur_ver,
            "skill_id": row["id"], "evaluation_id": ev.get("id"),
            "candidate_hash": now_ch, "validation_hash": recorded_vh,
            "evaluation_age_s": age, "measured": measured, "checks": checks}


def promotion_failure_text(audit):
    """把逐项核对写成能直接贴进报告 / 终端的失败说明。"""
    code = audit.get("code") or "UNKNOWN"
    lines = ["晋升被拒绝：%s" % PROMOTION_ERRORS.get(code, ""),
             "诊断码 %s ｜ 技能 %s ｜ 评估 %s ｜ 当前 v%s"
             % (code, audit.get("skill_id"), audit.get("evaluation_id"),
                audit.get("version")),
             "逐项核对："]
    for c in audit.get("checks") or []:
        lines.append("  %s %-20s 期望 %s ｜ 实际 %s"
                     % ("通过" if c["ok"] else "失败", c["name"],
                        c["expected"], c["actual"]))
    if audit.get("detail"):
        lines.append("  补充：%s" % audit["detail"])
    codes = audit.get("failed_codes") or []
    if len(codes) > 1:
        lines.append("  全部失败项：%s" % "、".join(codes))
    lines.append("处理建议：确认候选正文、验证集与训练/验证划分未改动，"
                 "重新运行 paired-v2 验证后再晋升；不要手工改 verdict 放行。")
    return "\n".join(lines)


def audit_promotion(store, skill_id, evaluation_id=None, *, now_s=None):
    """只读体检：不写库，返回同一套逐项核对结果，供报告与 CLI 使用。"""
    ensure_schema(store)
    t = core.now() if now_s is None else now_s
    try:
        with store.conn() as c:
            r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
            if not r:
                return {"ok": False, "code": "SKILL_NOT_FOUND", "idempotent": False,
                        "checks": [], "skill_id": skill_id,
                        "evaluation_id": evaluation_id,
                        "detail": "技能不存在：%s" % skill_id}
            row = dict(r)
            ev = _load_evaluation(c, skill_id, evaluation_id)
            versions = [int(x[0]) for x in c.execute(
                "SELECT version FROM skill_versions WHERE skill_id=?",
                (skill_id,)).fetchall()]
            return _audit_promotion(row, ev, t, versions)
    except PromotionRejected as e:
        return {"ok": False, "code": e.code, "idempotent": False, "checks": [],
                "skill_id": skill_id, "evaluation_id": evaluation_id,
                "detail": str(e)}


def promote(store, skill_id, *, evaluation_id=None, now_s=None):
    """晋升门禁：核对评估 → 事务内追加不可变快照 → 更新当前指针。

    不传 evaluation_id 时自动绑定该技能最近一次评估（正式入口常规路径）。
    重复请求命中「同一评估 + 同一指针」时返回已启用版本，不继续升版本。
    """
    ensure_schema(store)
    t = core.now() if now_s is None else now_s
    with store.conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not r:
            raise KeyError("技能不存在：%s" % skill_id)
        row = dict(r)
        try:
            ev = _load_evaluation(c, skill_id, evaluation_id)
        except PromotionRejected as e:
            raise PromotionRejected(e.code, promotion_failure_text({
                "code": e.code, "skill_id": skill_id, "evaluation_id": evaluation_id,
                "version": int(row["version"]), "checks": [], "detail": str(e)}))
        versions = [int(x[0]) for x in c.execute(
            "SELECT version FROM skill_versions WHERE skill_id=?",
            (skill_id,)).fetchall()]
        audit = _audit_promotion(row, ev, t, versions)
        if not audit["ok"]:
            raise PromotionRejected(audit["code"], promotion_failure_text(audit))
        if audit["idempotent"]:
            return int(ev["consumed_by"])

        new_ver = int(row["version"]) + 1
        snap = dict(row)
        snap.update({"version": new_ver, "status": ACTIVE,
                     "active_version": new_ver,
                     "promoted_from_evaluation": ev["id"], "promoted_at": t})
        # 三条写在同一事务：快照、当前指针、评估消费标记。任一失败整体回滚，
        # 不会留下「已启用但没有快照」或「同一评估被用两次」的中间态。
        c.execute("INSERT INTO skill_versions(id,skill_id,version,snapshot,created_at) "
                  "VALUES(?,?,?,?,?)",
                  ("skv_" + uuid.uuid4().hex[:8], skill_id, new_ver,
                   core.dumps(snap), t))
        c.execute("UPDATE skills SET status=?, version=?, active_version=?, "
                  "promoted_from_evaluation=?, promoted_at=? WHERE id=?",
                  (ACTIVE, new_ver, new_ver, ev["id"], t, skill_id))
        c.execute("UPDATE skill_evaluations SET consumed_by=?, consumed_at=? WHERE id=?",
                  (str(new_ver), t, ev["id"]))
    exp_mod.event(store, "skill_promoted", skill_id,
                  "v%d eval=%s candidate=%s"
                  % (new_ver, ev["id"], (ev.get("candidate_hash") or "")[:12]))
    return new_ver


def revise(store, skill_id, **body):
    """改技能正文，产生一份**未启用**的新草稿；不写快照、不自动启用。

    正文一变，此前所有评估的 candidate_hash 立即失效（P0 03），必须重新
    验证才能晋升。已启用版本的不可变快照保持不动；active_version 置空表示
    「当前行是草稿，不是正在执行的版本」。version 不变，下一次 promote 会
    把这份新正文固化成新的不可变版本。
    """
    ensure_schema(store)
    unknown = set(body) - set(CONTENT_FIELDS)
    if unknown:
        raise ValueError("revise 只允许改技能正文，未知字段：%s" % sorted(unknown))
    if not body:
        raise ValueError("revise 至少要改一个正文字段")
    t = core.now()
    with store.conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not r:
            raise KeyError("技能不存在：%s" % skill_id)
        cur = dict(r)
        for k, v in body.items():
            cur[k] = _db_value(v)
        c.execute("UPDATE skills SET name=?,task_type=?,fault_type=?,strategy=?,"
                  "required_tools=?,safety=?,scope=?,decision_params=?,"
                  "status=?,active_version=NULL,promoted_from_evaluation=NULL,"
                  "promoted_at=NULL WHERE id=?",
                  (cur["name"], cur["task_type"], cur["fault_type"], cur["strategy"],
                   cur["required_tools"], cur["safety"], cur.get("scope") or SINGLE,
                   cur.get("decision_params"), CANDIDATE, skill_id))
        c.execute("INSERT INTO evolution_events(id,ts,kind,subject_id,detail) "
                  "VALUES(?,?,?,?,?)",
                  ("ev_" + uuid.uuid4().hex[:10], t, "skill_revised", skill_id,
                   "改正文 %s，已作废旧评估" % ",".join(sorted(body))))
    return _get_skill(store, skill_id)


def _get_skill(store, skill_id):
    with store.conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
    return dict(r) if r else None


def active_snapshot(store, skill_id):
    """当前 active_version 指向的不可变快照。审计用它回答「现在到底执行哪份正文」。"""
    ensure_schema(store)
    with store.conn() as c:
        r = c.execute("SELECT active_version FROM skills WHERE id=?",
                      (skill_id,)).fetchone()
        if not r or r["active_version"] is None:
            return None
        s = c.execute("SELECT * FROM skill_versions WHERE skill_id=? AND version=? "
                      "ORDER BY rowid DESC LIMIT 1",
                      (skill_id, int(r["active_version"]))).fetchone()
    return dict(s) if s else None


def disable(store, skill_id):
    """撤销启用：只改状态与指针，**不恢复任何旧版正文**。

    与 rollback 严格区分：disable 是「先别用了」，rollback 是「换回旧内容继续用」。
    """
    ensure_schema(store)
    t = core.now()
    with store.conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not r:
            raise KeyError("技能不存在：%s" % skill_id)
        prev = r["active_version"]
        c.execute("UPDATE skills SET status=?,active_version=NULL,disabled_at=? WHERE id=?",
                  (DISABLED, t, skill_id))
    exp_mod.event(store, "skill_disabled", skill_id, "撤销启用，原指针 v%s" % prev)
    return DISABLED


def rollback(store, skill_id, to_version=None):
    """恢复旧版本正文并重新启用。保留全部快照与评估，不删除历史。

    旧实现只读了快照的 version，却只 UPDATE status / version —— strategy、
    scope、适用条件、required_tools、safety 全留在「新版本」状态，于是版本号
    退回去了，实际执行的行为没变。这里把整份正文字段从不可变快照写回当前行，
    并把 active_version 指到该快照；同时追加 rollback 审计事件。
    """
    ensure_schema(store)
    t = core.now()
    with store.conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not r:
            raise KeyError("技能不存在：%s" % skill_id)
        cur = dict(r)
        pointer = int(cur.get("active_version") or cur["version"])
        rows = c.execute("SELECT * FROM skill_versions WHERE skill_id=? "
                         "ORDER BY version, rowid", (skill_id,)).fetchall()
        if not rows:
            raise KeyError("没有可回滚的版本：%s" % skill_id)
        if to_version is not None:
            target = [x for x in rows if int(x["version"]) == int(to_version)]
            if not target:
                raise KeyError("目标版本不存在：v%s" % to_version)
        else:
            target = [x for x in rows if int(x["version"]) < pointer]
            if not target:
                raise KeyError("当前指针 v%s 之前没有更旧的快照" % pointer)
        snap_row = target[-1]
        snap = core.loads(snap_row["snapshot"], {})
        if not snap:
            raise ValueError("快照为空，无法回滚：%s" % snap_row["id"])
        ver = int(snap.get("version") or snap_row["version"])
        vals = {k: _db_value(snap.get(k)) for k in CONTENT_FIELDS}
        vals["scope"] = vals.get("scope") or SINGLE
        c.execute("UPDATE skills SET name=?,task_type=?,fault_type=?,strategy=?,"
                  "required_tools=?,safety=?,scope=?,decision_params=?,"
                  "status=?,version=?,active_version=?,"
                  "rolled_back_from_version=?,rolled_back_at=?,"
                  "promoted_from_evaluation=NULL,promoted_at=NULL,"
                  "disabled_at=NULL WHERE id=?",
                  (vals["name"], vals["task_type"], vals["fault_type"], vals["strategy"],
                   vals["required_tools"], vals["safety"], vals["scope"],
                   vals["decision_params"], ACTIVE, ver, ver, pointer, t, skill_id))
        c.execute("INSERT INTO evolution_events(id,ts,kind,subject_id,detail) "
                  "VALUES(?,?,?,?,?)",
                  ("ev_" + uuid.uuid4().hex[:10], t, "skill_rolled_back", skill_id,
                   "v%s → v%s，恢复整份正文（%s），历史快照与评估保留"
                   % (pointer, ver, ",".join(CONTENT_FIELDS))))
    return ver


def list_skills(store, status=None, limit=None, cursor=None):
    """列出技能。EG-012：limit/cursor 可选，默认 None 保持原全量行为
    （内部诊断/进化基准调用不传，语义不变）；API 层传值做有界分页。"""
    ensure_schema(store)
    clauses, args = [], []
    if status:
        clauses.append("status=?")
        args.append(status)
    if cursor is not None:
        clauses.append("created_at < ?")
        args.append(cursor)
    sql = "SELECT * FROM skills"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    with store.conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
