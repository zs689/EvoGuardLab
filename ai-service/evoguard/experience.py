# -*- coding: utf-8 -*-
"""经验层：从一次「故障→诊断→恢复」的轨迹里提炼结构化经验，并做准入校验。

这是 EvoGuard 与普通「记住一段对话」的根本区别：
  1. 经验必须带 evidence_refs（指向具体 step_id），没有证据一律拒绝
  2. 证据必须能回溯到 source_run_id 的真实步骤、诊断与成功恢复（P0 05）
  3. 经验有 validation_status，未验证的**不得**作为可信执行指导
  4. 失败的恢复只能当反面案例，不能当经验

表在本模块内按需创建（规格要求：按阶段逐步迁移，不一次建全）。
"""
import hashlib
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiences(
  id TEXT PRIMARY KEY, source_run_id TEXT, task_type TEXT, environment TEXT,
  failure_type TEXT, root_cause TEXT, recovery_action TEXT, recovery_result TEXT,
  evidence_refs TEXT, confidence REAL, validation_status TEXT,
  created_at REAL, version INTEGER,
  fault_scope TEXT, provider TEXT, model_id TEXT, model_fingerprint TEXT,
  prompt_fingerprint TEXT, dataset_version TEXT, tool_backend TEXT,
  experiment_protocol TEXT, env_meta TEXT);

CREATE TABLE IF NOT EXISTS evolution_events(
  id TEXT PRIMARY KEY, ts REAL, kind TEXT, subject_id TEXT, detail TEXT);

CREATE INDEX IF NOT EXISTS idx_exp_type ON experiences(task_type, failure_type);
CREATE INDEX IF NOT EXISTS idx_exp_status ON experiences(validation_status);
"""

VALIDATED, PENDING, REJECTED = "validated", "pending", "rejected"

ENV_PROTOCOL = "evoguard-env-v1"
DATASET_PROTOCOL = "evoguard-sim-v1"
TOOL_BACKEND_SIMULATED, TOOL_BACKEND_REAL = "simulated", "real"

# 环境元数据字段。缺数据一律留空，绝不拿默认值冒充「已核实」。
ENV_COLUMNS = ("fault_scope", "provider", "model_id", "model_fingerprint",
               "prompt_fingerprint", "dataset_version", "tool_backend",
               "experiment_protocol")


def ensure_schema(store):
    with store.conn() as c:
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(experiences)").fetchall()]
        # 历史经验的环境字段保持 NULL：来源不明就是来源不明，不能补一个
        # "mock" 让老数据看起来像新协议核实过的结果。
        for name, decl in (("fault_scope", "TEXT"), ("provider", "TEXT"),
                           ("model_id", "TEXT"), ("model_fingerprint", "TEXT"),
                           ("prompt_fingerprint", "TEXT"), ("dataset_version", "TEXT"),
                           ("tool_backend", "TEXT"), ("experiment_protocol", "TEXT"),
                           ("env_meta", "TEXT")):
            if name not in cols:
                c.execute("ALTER TABLE experiences ADD COLUMN %s %s" % (name, decl))


def event(store, kind, subject_id, detail=""):
    # 审计表在本模块里，但别的模块（skills）也会来写事件。
    # 不加这一行，先调 skills 再调 event 就会报 no such table。
    ensure_schema(store)
    with store.conn() as c:
        c.execute("INSERT INTO evolution_events(id,ts,kind,subject_id,detail) VALUES(?,?,?,?,?)",
                  ("ev_" + uuid.uuid4().hex[:10], round(time.time(), 3), kind,
                   subject_id, str(detail)[:500]))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dataset_version():
    """数据集版本指纹：从实际加载的 sim 数据算，而不是写一个假的版本号常量。"""
    sm = sys.modules.get("sim")
    if sm is None:
        return None
    try:
        types = list(getattr(sm, "AGENT_TYPES", []) or [])
        tasks = {at: [t["task_id"] for t in sm.test_tasks(at)] for at in types}
        faults = {at: list((getattr(sm, "CASE_FAULTS", {}) or {}).get(at, []))
                  for at in types}
        return _sha(core.dumps({"protocol": DATASET_PROTOCOL,
                                "agent_types": types, "tasks": tasks, "faults": faults}))
    except Exception:
        return None


def _collect_env(store, run_id, run, steps):
    """从真实轨迹复制环境元数据。缺失字段一律 None，不猜、不硬编码。

    Word 计划 3.x / P0 05：以前 environment 固定写 "mock"，真实模型跑出来的
    经验也标成 mock，导致「本地真实模型 + 模拟工具」和「真实工具链」分不开。
    """
    model_steps = [s for s in steps if s.get("kind") == "model"]
    providers, model_ids, prompts = [], [], []
    for s in model_steps:
        inp = s.get("input") if isinstance(s.get("input"), dict) else {}
        if inp.get("provider"):
            providers.append(str(inp["provider"]))
        if s.get("model_id"):
            model_ids.append(str(s["model_id"]))
        if inp.get("prompt"):
            prompts.append(str(inp["prompt"]))
    provider = providers[0] if providers else None
    model_id = model_ids[0] if model_ids else None

    # 系统 prompt 从已加载的 runtime 软引用读取，不硬 import：
    # runtime → skills → experience 是一条已存在的依赖链，反向导入会成环。
    rt = sys.modules.get("runtime")
    sys_prompt = getattr(rt, "SYSTEM_PROMPT", None) if rt else None
    prompt_parts = ([sys_prompt] if sys_prompt else []) + sorted(set(prompts))
    prompt_fingerprint = _sha("\n".join(prompt_parts)) if prompt_parts else None

    gw = sys.modules.get("gateway")
    prov_obj = None
    if gw is not None and getattr(gw, "get_provider", None):
        try:
            prov_obj = gw.get_provider()
        except Exception:
            prov_obj = None
    # 轨迹里记录的 provider 才是真相。当前进程的 gateway 默认常常是 mock，
    # 拿它覆盖轨迹会把真实模型跑出来的经验误标成 mock —— 正是 P0 05 要修的错。
    same_provider = provider is None or getattr(prov_obj, "name", None) == provider
    model_desc = {
        "provider": provider, "model_id": model_id,
        "configured_model": (getattr(prov_obj, "model", None) if same_provider else None),
        "base": (getattr(prov_obj, "base", None) if same_provider else None),
    }
    model_fingerprint = (_sha(core.dumps(model_desc))
                         if any(bool(v) for v in model_desc.values()) else None)

    backend_env = os.environ.get("AGENTLAB_TOOL_BACKEND")
    tool_backend = backend_env or TOOL_BACKEND_SIMULATED
    is_mock = (provider == "mock") or (
        provider is None and bool(getattr(prov_obj, "is_mock", False)))
    if is_mock:
        environment = "mock_simulated_tools"
    elif tool_backend == TOOL_BACKEND_REAL:
        environment = "real_toolchain"
    else:
        environment = "local_model_simulated_tools"

    sk = sys.modules.get("skills")
    protocol = core.dumps({
        "mode": run.get("mode"), "seed": run.get("seed"),
        "validation_protocol": getattr(sk, "VALIDATION_PROTOCOL", None),
        "promotion_protocol": getattr(sk, "PROMOTION_PROTOCOL", None)})

    return {"env_protocol": ENV_PROTOCOL, "environment": environment,
            "provider": provider, "model_id": model_id,
            "model_fingerprint": model_fingerprint,
            "prompt_fingerprint": prompt_fingerprint,
            "prompt_source": "system_prompt+steps" if sys_prompt else "steps_only",
            "model_fingerprint_note": "可观测标识组合，不是模型权重哈希",
            "dataset_version": _dataset_version(),
            "dataset_protocol": DATASET_PROTOCOL,
            "tool_backend": tool_backend,
            "tool_backend_source": ("AGENTLAB_TOOL_BACKEND" if backend_env
                                    else "default:simulated"),
            "experiment_protocol": protocol,
            "source_run_id": run_id,
            "model_step_count": len(model_steps), "step_count": len(steps)}


def extract(store, run_id):
    """从一次运行里提炼经验。没有诊断记录就返回 None —— 没诊断就没有可提炼的东西。"""
    run = store.get_run(run_id)
    diagnoses = store.list_diagnoses(run_id)
    if not diagnoses:
        return None
    # 索引键用**首轮**根因：运行时 plan_recovery 拿到的就是首轮诊断结果。
    # 用最后一轮根因做键，组合技能在复合故障上永远查不到 —— 这是踩过的坑。
    dia = diagnoses[0]
    recs = store.list_recoveries(run_id)
    rec = recs[-1] if recs else None
    steps = store.list_steps(run_id)

    # 恢复动作：策略链要记**整条**，只记最后一步会让「组合」在经验里丢失，
    # 进化出来的技能永远退化为单策略。
    # 判定成功与否看「最后一步」而不是任一步：链中途失败是正常排障过程。
    chain = []
    for r in recs:
        if r.get("params", {}).get("chain"):
            chain = list(r["params"]["chain"])
        elif r.get("strategy") not in (None, "", "human_takeover"):
            chain = [r["strategy"]]
    if not chain and rec and rec.get("strategy") not in (None, "", "human_takeover"):
        chain = [rec["strategy"]]
    action = core.dumps(chain) if len(chain) > 1 else (chain[0] if chain else "")

    # 证据：每一轮诊断定位到的那一步 + 恢复过程产生的 replay 步。
    # 注意：diagnosis 里存的是**绝对 seq**（报告要对得上轨迹行），
    # 这里必须按 seq 匹配，比成 logical 会一条证据都取不到 ->
    # 经验被判「没有 evidence_refs」-> 探索出来的经验全被拒。
    bad_seqs = {d["first_bad_step"] for d in diagnoses}
    refs = []
    for s in steps:
        if s.get("seq") in bad_seqs:
            refs.append(s["id"])
    for s in steps:
        if s["kind"] == "replay":
            refs.append(s["id"])

    meta = _collect_env(store, run_id, run, steps)
    fault_scope = "multi" if len(diagnoses) > 1 else "single"
    meta["fault_scope"] = fault_scope
    exp = {
        "id": "exp_" + uuid.uuid4().hex[:10],
        "source_run_id": run_id,
        "task_type": _task_type_of(store, run),
        "environment": meta["environment"],
        "failure_type": dia["root_cause"],
        "root_cause": dia["root_cause"],
        "recovery_action": action or ((rec or {}).get("strategy") or ""),
        "recovery_result": "success" if (rec or {}).get("ok") else ("fail" if rec else "none"),
        "evidence_refs": refs,
        "confidence": float(dia.get("confidence") or 0.0),
        "validation_status": PENDING,
        "created_at": round(time.time(), 3),
        "version": 1,
        # 一次运行里诊断出几个坏点 = 这条经验是「单故障」还是「多故障」学来的。
        # 技能层用它决定作用域：单故障经验不该被套到复合场景的第一步，
        # 复合学到的链也不该占住单故障的键位（实测会把单故障 6 个用例打挂）。
        "fault_scope": fault_scope,
    }
    for key in ENV_COLUMNS:
        if key not in exp:
            exp[key] = meta.get(key)
    exp["env_meta"] = core.dumps(meta)
    return exp


def _task_type_of(store, run):
    try:
        return store.get_experiment(run["experiment_id"])["agent_type"]
    except Exception:
        return "unknown"


def _verify_evidence(store, exp):
    """数据库级证据溯源。结构条件通过之后才轮到这一层。

    以前 evidence_refs 只要「非空」就算有证据，随便填几个字符串、甚至把
    别的运行的 step_id 抄过来都能通过。这里逐条核实归属与可关联性。
    """
    reasons = []
    rid = exp.get("source_run_id")
    refs = list(exp.get("evidence_refs") or [])
    with store.conn() as c:
        run = c.execute("SELECT id FROM runs WHERE id=?", (rid,)).fetchone()
        if not run:
            return ["source_run_id 不存在：%s" % rid]
        for sid in refs:
            r = c.execute("SELECT run_id FROM steps WHERE id=?", (sid,)).fetchone()
            if not r:
                reasons.append("证据步骤不存在：%s" % sid)
            elif r["run_id"] != rid:
                reasons.append("跨运行伪造引用：%s 属于 %s，不属于 %s"
                               % (sid, r["run_id"], rid))
        steps = [dict(x) for x in c.execute(
            "SELECT id,seq,kind FROM steps WHERE run_id=? ORDER BY seq",
            (rid,)).fetchall()]
        seq_to_id = {s["seq"]: s["id"] for s in steps}
        dias = [dict(x) for x in c.execute(
            "SELECT * FROM diagnoses WHERE run_id=? ORDER BY created_at,rowid",
            (rid,)).fetchall()]
        if not dias:
            reasons.append("source_run_id 下没有诊断记录")
        else:
            diag_ids = {seq_to_id.get(d["first_bad_step"]) for d in dias}
            diag_ids.discard(None)
            if not (diag_ids & set(refs)):
                reasons.append("诊断定位的坏步未被 evidence_refs 引用")
        recs = [dict(x) for x in c.execute(
            "SELECT * FROM recoveries WHERE run_id=? ORDER BY created_at,rowid",
            (rid,)).fetchall()]
        ok_recs = [r for r in recs if r["ok"]]
        if not ok_recs:
            reasons.append("source_run_id 下没有成功的恢复记录")
        else:
            chain = core.as_chain(exp.get("recovery_action"))
            linked = False
            for r in ok_recs:
                params = core.loads(r.get("params"), {}) or {}
                strategies = set(core.as_chain(params.get("chain") or []))
                strategies.add(r["strategy"])
                if not chain or (set(chain) & strategies):
                    linked = True
                    break
            if not linked:
                reasons.append("恢复动作与成功恢复记录无法关联")
        replay_ids = {s["id"] for s in steps if s["kind"] == "replay"}
        if replay_ids and not (replay_ids & set(refs)):
            reasons.append("恢复产生的 replay 步未被引用")
    return reasons


def validate(exp, store=None):
    """准入校验。返回 (status, reasons)。

    结构条件（证据非空 / 根因已知 / 恢复成功 / 置信度）照旧；
    传 store 时再做证据溯源。**不传 store 最高只能到 PENDING** ——
    没法核实来源，就不能发「可信执行指导」的资格。
    """
    reasons = []
    if not exp.get("evidence_refs"):
        reasons.append("没有 evidence_refs")
    if not exp.get("root_cause") or exp["root_cause"] == "unknown":
        reasons.append("根因未知，无法形成可复用经验")
    if exp.get("recovery_result") != "success":
        reasons.append("恢复未成功，只能作为反面案例")
    if float(exp.get("confidence") or 0) < 0.5:
        reasons.append("置信度过低")
    if reasons:
        return REJECTED, reasons
    if store is None:
        return PENDING, ["未传数据库，evidence_refs 未做来源核实"]
    reasons = _verify_evidence(store, exp)
    return (VALIDATED if not reasons else REJECTED), reasons


def save(store, exp):
    """保存经验。先做带库溯源校验，再落库 —— 不允许绕过校验直接写。

    rejected / pending 经验照样写进库（保留作分析资料），只是 retrieve
    默认与 include_pending 都不会把它们当可信执行指导返回。
    """
    ensure_schema(store)
    status, reasons = validate(exp, store)
    exp["validation_status"] = status
    env_meta = exp.get("env_meta")
    if env_meta is None:
        env_meta = core.dumps({k: exp.get(k) for k in ENV_COLUMNS})
    with store.conn() as c:
        c.execute(
            "INSERT INTO experiences(id,source_run_id,task_type,environment,failure_type,"
            "root_cause,recovery_action,recovery_result,evidence_refs,confidence,"
            "validation_status,created_at,version,fault_scope,provider,model_id,"
            "model_fingerprint,prompt_fingerprint,dataset_version,tool_backend,"
            "experiment_protocol,env_meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (exp["id"], exp["source_run_id"], exp["task_type"], exp["environment"],
             exp["failure_type"], exp["root_cause"], exp["recovery_action"],
             exp["recovery_result"], core.dumps(exp["evidence_refs"]),
             exp["confidence"], status, exp["created_at"], exp["version"],
             exp.get("fault_scope"), exp.get("provider"), exp.get("model_id"),
             exp.get("model_fingerprint"), exp.get("prompt_fingerprint"),
             exp.get("dataset_version"), exp.get("tool_backend"),
             exp.get("experiment_protocol"), env_meta))
    event(store, "experience_saved", exp["id"],
          "%s/%s/%s -> %s [%s]"
          % (exp["task_type"], exp["failure_type"], exp["recovery_action"],
             status, exp.get("environment") or "unknown"))
    return exp, reasons


def retrieve(store, task_type=None, failure_type=None, include_pending=False):
    """记忆检索（治理在这里生效）。

    - 默认只返回 validated
    - include_pending=True 时把 pending 也返回 —— 这是 C 模式（不严格验证）用的，
      目的是**故意让它吃到未验证经验**，从而测出负迁移
    - rejected 永远不返回
    """
    ensure_schema(store)
    sql = "SELECT * FROM experiences WHERE 1=1"
    args = []
    if task_type:
        sql += " AND task_type=?"
        args.append(task_type)
    if failure_type:
        sql += " AND failure_type=?"
        args.append(failure_type)
    allowed = (PENDING, VALIDATED) if include_pending else (VALIDATED,)
    sql += " AND validation_status IN (%s)" % ",".join("?" * len(allowed))
    args.extend(allowed)
    with store.conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence_refs"] = core.loads(d["evidence_refs"], [])
        d["env_meta"] = core.loads(d.get("env_meta"), None)
        out.append(d)
    return out


def stats(store):
    ensure_schema(store)
    with store.conn() as c:
        rows = c.execute("SELECT validation_status, COUNT(*) FROM experiences "
                         "GROUP BY validation_status").fetchall()
    return {r[0]: r[1] for r in rows}
