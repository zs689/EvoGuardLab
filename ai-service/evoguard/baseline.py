# -*- coding: utf-8 -*-
"""P0 06 冻结可复现基线：manifest、稳定技能库指纹、防混跑与报告数字统一。

为什么需要独立的 manifest 模块：之前只有一份一次性生成的
baseline_manifest.json（31 个文件哈希），它回答不了三个问题：
  1. 这次跑的四组数字是哪一份 C/D 技能库、哪一份配置产生的？
  2. 把两次运行的 runs.jsonl 拼起来算成功率，有没有混进不同指纹的结果？
  3. 报告里的百分比和 metrics.json 的分母是否真的对得上？

技能库指纹刻意**不哈希 SQLite 文件字节**：同一个库被打开/关闭后页内容与
journal 状态可能变，字节哈希会飘。这里按 id 排序后哈希技能正文。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

PROTOCOL = "evoguard-baseline-v1"
MANIFEST_NAME = "manifest.json"

# 关键代码指纹：这些文件变了却沿用旧报告，就是隐式混跑。
#
# 评审 R09：原来只有 11 个文件，**防护逻辑本身一个都没在里面**——
# security.py / approval.py / tool_contract.py / semantic_guard.py /
# action_scope.py / scan_pipeline.py / code_guard.py / execution.py 全缺席，
# 于是「把检测器改松一半」这种事不会让 manifest 变化。
# 现在凡是**会改变判定结果**的模块都在清单里，缺一个就直接报错（不再静默跳过）。
CODE_FILES = (
    # 执行与状态
    "ai-service/evoguard/core.py",
    "ai-service/evoguard/skills.py",
    "ai-service/evoguard/experience.py",
    "ai-service/evoguard/runtime.py",
    "ai-service/evoguard/sim.py",
    "ai-service/evoguard/gateway.py",
    "ai-service/evoguard/evaluate.py",
    "ai-service/evoguard/baseline.py",
    # 防护（评审 R09 补全）
    "ai-service/evoguard/security.py",
    "ai-service/evoguard/execution.py",
    "ai-service/evoguard/approval.py",
    "ai-service/evoguard/tool_contract.py",
    "ai-service/evoguard/semantic_guard.py",
    "ai-service/evoguard/deep_scan.py",
    "ai-service/evoguard/action_scope.py",
    "ai-service/evoguard/tool_rails.py",
    "ai-service/evoguard/scan_pipeline.py",
    "ai-service/evoguard/code_guard.py",
    # 评测入口
    "ai-service/evolution_bench.py",
    "ai-service/real_model_train.py",
    "ai-service/real_model_bench.py",
    "ai-service/real_model_attribution.py",
)

# 会把「同一份代码跑出不同数字」的配置与数据。
CONFIG_FILES = (
    "config/thresholds_v1.json",
    "config/thresholds_v2_calibrated.json",
    "tests/fixtures/inspect_config.json",
)

# 生效开关：只记**已知**的安全开关（取值固定），不把整份环境变量塞进指纹 ——
# 那会把机器名、临时目录也算进去，指纹天天变，等于没有指纹。
SECURITY_SWITCHES = (
    "AGENTLAB_SECURITY_MODE",
    "AGENTLAB_SECURITY_INTENT",
    "AGENTLAB_SECURITY_SEMANTIC",
    "AGENTLAB_SECURITY_DEEP",
    "AGENTLAB_SECURITY_WRAP",
    "AGENTLAB_SECURITY_SCOPE",
    "AGENTLAB_EXEC_MODE",
    "AGENTLAB_AUTO_APPROVE",
    "AGENTLAB_ALLOW_AUTO_APPROVE",
    "AGENTLAB_DEEP_MODEL",
    "AGENTLAB_SEMANTIC_MODEL",
    # 审计链 HMAC：不配就只有 hash 链，**持库写权者可整链重写**（已实测：
    # tests/test_audit_chain_rewrite.py 里无 key 时 verify 仍通过）。
    # 放进这份可见配置，是为了让人跑 `evoguard config` 就能看到它没配；
    # 值本身由 _mask_value 脱敏成 <redacted>/<unset>，不会泄露密钥。
    "EVOGUARD_AUDIT_HMAC_KEY",
)

# 技能库内容指纹覆盖的字段。status/version/active_version 也算：
# 同一批技能「候选」和「已晋升」是两种不同的实验条件。
LIB_FIELDS = ("id", "name", "task_type", "fault_type", "strategy",
              "required_tools", "safety", "scope", "decision_params",
              "status", "version", "active_version")

MODES = ("A", "B", "C", "D")


class BaselineMismatch(ValueError):
    """指纹不一致，拒绝把不同基线的数据混在一起。"""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def sha256_obj(obj):
    return hashlib.sha256(core.dumps(obj).encode("utf-8")).hexdigest()


def _canon(value):
    """内存对象与落库后的 JSON 字符串归一，保证同一技能算出同一指纹。"""
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


def library_content_hash(db_path):
    """技能库内容指纹（稳定）。库不存在或没有 skills 表返回 (None, 0)。"""
    if not os.path.exists(db_path):
        return None, 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(skills)").fetchall()]
        if "id" not in cols:
            return None, 0
        fields = [f for f in LIB_FIELDS if f in cols]
        rows = conn.execute("SELECT %s FROM skills ORDER BY id"
                            % ",".join(fields)).fetchall()
        body = [{f: _canon(r[f]) for f in fields} for r in rows]
        return sha256_obj({"protocol": PROTOCOL, "fields": fields, "skills": body}), len(body)
    finally:
        conn.close()


def config_hash(config):
    return sha256_obj(_canon(config or {}))


def library_hashes(lib_paths):
    """技能库指纹表。与 freeze 内部用同一套算法，保证两处算出的值一致。"""
    out = {}
    for label, path in sorted((lib_paths or {}).items()):
        content, n = library_content_hash(path)
        out[label] = {"path": path, "content_sha256": content, "skills": n}
    return out


def hashes(config, lib_paths):
    """运行前就能算出的两个指纹 —— metrics 里要写它们，才能事后追是哪次基线。"""
    libs = library_hashes(lib_paths)
    return {"config_hash": config_hash(config),
            "library_hash": sha256_obj({k: v["content_sha256"] for k, v in libs.items()}),
            "libraries": libs}


def default_config(root):
    """从**当前代码常量**与配置读协议/门槛，不手写一份可能漂移的副本。"""
    cfg = {"protocol": PROTOCOL}
    try:
        import skills
        cfg.update({"validation_protocol": skills.VALIDATION_PROTOCOL,
                    "promotion_protocol": skills.PROMOTION_PROTOCOL,
                    "min_gain_pp": skills.MIN_GAIN_PP,
                    "max_neg_transfer": skills.MAX_NEG_TRANSFER,
                    "min_validation_tasks": skills.MIN_VALIDATION_TASKS,
                    "max_evaluation_age_s": skills.MAX_EVALUATION_AGE_S})
    except Exception:
        pass
    try:
        import experience
        cfg["env_protocol"] = experience.ENV_PROTOCOL
    except Exception:
        pass
    # 生效安全开关：脱敏后的取值进指纹。开着还是关着，是两种实验条件，
    # 不能只在报告文字里写一句「本次开启了语义判定」就算交代过。
    cfg["security_switches"] = effective_security_config()
    try:
        import security
        cfg["security_defaults"] = {
            "mode": security.DEFAULT_MODE,
            "detectors": sorted(d["code"] for d in security.DETECTORS),
        }
    except Exception:
        pass
    try:
        import semantic_guard
        cfg["prompt_fingerprints"] = (semantic_guard.prompt_fingerprints()
                                      if hasattr(semantic_guard, "prompt_fingerprints")
                                      else None)
    except Exception:
        pass
    path = os.path.join(root, "tests", "fixtures", "inspect_config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg["inspect_config"] = json.load(f)
    return cfg


def _git_head(root):
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                      stderr=subprocess.DEVNULL)
        return out.decode("utf-8").strip()
    except Exception:
        return None


def _rel(root, path):
    return os.path.relpath(os.path.abspath(path), os.path.abspath(root)).replace(os.sep, "/")


def _find_libraries(root, run_dir):
    out = {}
    for label, name in (("C", "library_c.db"), ("D", "library_d.db")):
        for base in (run_dir, os.path.join(root, "data")):
            p = os.path.join(base, name)
            if os.path.exists(p):
                out[label] = p
                break
    return out


def _discover(run_dir, root):
    """收集本次运行的产物，manifest 自身不参与（否则自指循环）。"""
    files = []
    for name in sorted(os.listdir(run_dir)):
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path) or name == MANIFEST_NAME:
            continue
        if name == "metrics.json":
            kind = "metrics"
        elif name == "runs.jsonl":
            kind = "runs"
        elif name.endswith(".md"):
            kind = "report"
        elif name.endswith(".db"):
            kind = "database"
        elif name.endswith(".json"):
            kind = "evidence"
        else:
            continue
        files.append({"path": _rel(root, path), "kind": kind,
                      "bytes": os.path.getsize(path), "sha256": sha256_file(path)})
    missing = []
    for rel in CODE_FILES:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            missing.append(rel)
            continue
        files.append({"path": rel, "kind": "code",
                      "bytes": os.path.getsize(path), "sha256": sha256_file(path)})
    for rel in CONFIG_FILES:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue                      # 配置可选：没有就不算进指纹
        files.append({"path": rel, "kind": "config",
                      "bytes": os.path.getsize(path), "sha256": sha256_file(path)})
    # 缺文件不能静默省略（评审 R09）：少一个文件，指纹就少一块证据，
    # 而报告仍然写着「已冻结」—— 那是把「没查」包装成「查过了」。
    # 但合成根目录（例如测试里临时建的 root）本来就没有源码，不能因此报错；
    # 判据用「这个根目录里到底有没有项目代码」：一个 evoguard 模块都没有 = 不是项目根。
    project_like = os.path.isdir(os.path.join(root, "ai-service", "evoguard"))
    if missing and project_like:
        raise BaselineMismatch(
            "基线指纹缺少必需文件，拒绝冻结：%s\n"
            "（缺文件时生成的 manifest 会漏掉安全模块，等于给了假的「已冻结」）"
            % "、".join(missing))
    _discover.code_coverage = {
        "project_root": project_like,
        "listed": len(CODE_FILES),
        "hashed": len(CODE_FILES) - len(missing),
        "missing": missing,
    }
    return sorted(files, key=lambda x: x["path"])


#: 名字像凭据的键一律脱敏。用户把 `evoguard config` 输出贴到 issue 里
#: 是极常见的操作——当前 SECURITY_SWITCHES 里只有 0/1 开关和模型名，
#: 但一旦有人加了 *_API_KEY，原样输出就会把密钥打出去。
_SENSITIVE_NAME = re.compile(r"(?i)(?:key|token|secret|password|passwd|credential)")


def _mask_value(key, value):
    if value is None:
        return "<unset>"
    if _SENSITIVE_NAME.search(str(key)):
        # 连长度都不给：长度本身也是信息（可配合字典攻击缩小范围）
        return "<redacted>"
    return value


def effective_security_config(env=None, extra=()):
    """本次运行的**生效**安全配置。启动时打印/导出，不靠人猜环境变量。

    凭据类键（名字含 key/token/secret/password/credential）输出 <redacted>，
    绝不打印真实值。
    """
    env = os.environ if env is None else env
    keys = tuple(SECURITY_SWITCHES) + tuple(extra)
    return {k: _mask_value(k, env.get(k)) for k in keys}


def _fingerprint(files, libraries, cfg_hash):
    body = {"files": [[f["path"], f["sha256"]] for f in files],
            "libraries": {k: v["content_sha256"] for k, v in sorted(libraries.items())},
            "config_hash": cfg_hash}
    return sha256_obj(body)


def freeze(root, run_dir, *, config=None, libraries=None, note="",
           manifest_path=None, allow_overwrite=False):
    """冻结一次运行的全部证据。默认写 run_dir/manifest.json。

    冻结**历史基线**时应该传 manifest_path 指向新的隔离目录：manifest 不落在
    被冻结的目录里，既不改动历史产物，也避免 manifest 自己登记自己。
    已存在的 manifest 默认拒绝覆盖 —— 重跑必须用新的输出目录。
    """
    root = os.path.abspath(root)
    run_dir = os.path.abspath(run_dir)
    if not os.path.isdir(run_dir):
        raise FileNotFoundError("运行目录不存在：%s" % run_dir)
    out = (os.path.abspath(manifest_path) if manifest_path
           else os.path.join(run_dir, MANIFEST_NAME))
    if os.path.exists(out) and not allow_overwrite:
        raise FileExistsError("manifest 已存在，拒绝覆盖历史证据：%s" % out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    cfg = config if config is not None else default_config(root)
    cfg_hash = config_hash(cfg)
    lib_paths = libraries if libraries is not None else _find_libraries(root, run_dir)
    if libraries is not None and not libraries:
        # 显式传了空集合，几乎一定是路径算错（例如拿 basename 去 exists）。
        # 静默写一个空库集合，指纹就退化成 sha256("{}")，等于没做指纹。
        raise ValueError("技能库集合为空：检查传入的库路径是否存在")
    libs = {}
    for label, path in sorted(lib_paths.items()):
        if isinstance(path, dict) and "content_sha256" in path:
            # 允许传 hashes() 的预计算结果，但要重算一遍对账：
            # 运行期间库内容变了（例如中途晋升了技能），metrics 记的指纹就作废。
            p = path.get("path")
            content, n = library_content_hash(p) if p else (path["content_sha256"],
                                                           path.get("skills"))
            if p and content != path["content_sha256"]:
                raise BaselineMismatch(
                    "技能库 %s 在运行期间内容已变化：运行前 %s，现在 %s"
                    % (label, str(path["content_sha256"])[:16], str(content)[:16]))
            libs[label] = {"path": _rel(root, p) if p else None,
                           "content_sha256": content, "skills": n,
                           "file_sha256": sha256_file(p) if p and os.path.exists(p) else None}
            continue
        content, n = library_content_hash(path)
        libs[label] = {"path": _rel(root, path), "content_sha256": content,
                       "skills": n,
                       "file_sha256": sha256_file(path) if os.path.exists(path) else None}
    files = _discover(run_dir, root)
    library_hash = sha256_obj({k: v["content_sha256"] for k, v in sorted(libs.items())})
    manifest = {
        "protocol": PROTOCOL,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "run_dir": _rel(root, run_dir),
        "baseline_commit": _git_head(root),
        "config": cfg, "config_hash": cfg_hash,
        "libraries": libs, "library_hash": library_hash,
        "files": files,
        # 明白写下「清单里列了几个、真的哈希了几个、缺了哪些」——
        # 只写一句「已冻结」而不说覆盖面，就是最容易被误读的那种证据。
        "code_coverage": getattr(_discover, "code_coverage", None),
        "note": note,
    }
    manifest["fingerprint"] = _fingerprint(files, libs, cfg_hash)
    manifest["manifest_path"] = _rel(root, out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    return manifest


def verify(root, manifest_path):
    """重算并逐项比对。不抛异常，便于报告与 CLI 使用。

    产物变化和代码变化要分开：产物是「证据被改过」，代码是「产出它的代码
    已经不是这一份」。前者让基线作废，后者只是说这份报告属于历史版本 ——
    混在一起报，用户分不清是数据坏了还是只是代码往前走了一步。
    """
    root = os.path.abspath(root)
    manifest_path = os.path.abspath(manifest_path)
    with open(manifest_path, encoding="utf-8") as f:
        m = json.load(f)
    diffs, code_drift = [], []
    if m.get("protocol") != PROTOCOL:
        diffs.append("协议不匹配：%s != %s" % (m.get("protocol"), PROTOCOL))
    run_dir = os.path.join(root, m["run_dir"])
    actual = {f["path"]: f for f in _discover(run_dir, root)}
    expected = {f["path"]: f for f in m.get("files", [])}
    for path in sorted(set(expected) | set(actual)):
        is_code = (expected.get(path) or actual.get(path) or {}).get("kind") == "code"
        target = code_drift if is_code else diffs
        if path not in actual:
            target.append("文件缺失：%s" % path)
        elif path not in expected:
            target.append("新增未登记文件：%s" % path)
        elif expected[path]["sha256"] != actual[path]["sha256"]:
            target.append("内容变化：%s" % path)
    for label, info in sorted(m.get("libraries", {}).items()):
        p = os.path.join(root, info["path"])
        content, n = library_content_hash(p)
        if content != info.get("content_sha256") or n != info.get("skills"):
            diffs.append("技能库 %s 内容或条数变化：%s" % (label, info["path"]))
    if config_hash(m.get("config")) != m.get("config_hash"):
        diffs.append("配置哈希与 config 内容不符")
    fp = _fingerprint(m.get("files", []), m.get("libraries", {}), m.get("config_hash", ""))
    if fp != m.get("fingerprint"):
        diffs.append("manifest 总指纹与登记项不符")
    return {"ok": not diffs, "diffs": diffs, "code_drift": code_drift,
            "protocol": m.get("protocol"), "run_dir": m.get("run_dir"),
            "baseline_commit": m.get("baseline_commit"),
            "fingerprint": m.get("fingerprint")}


def run_key(case, *, cfg_hash, lib_hash, protocol=PROTOCOL):
    """按 case_id / seed / mode / 配置哈希 / 技能库哈希生成可复现恢复键。"""
    body = {
        "case_id": (case.get("case_id") or case.get("key")
                    or "%s|%s|%s|%s" % (case.get("suite") or case.get("agent_type"),
                                        case.get("task") or case.get("task_id"),
                                        case.get("fault") or case.get("fault_type"),
                                        case.get("mode"))),
        "seed": int(case.get("seed", 7)),
        "mode": case.get("mode"),
        "config_hash": cfg_hash,
        "library_hash": lib_hash,
        "protocol": protocol,
    }
    return sha256_obj(body)


def assert_no_mixing(records, *, cfg_hash, lib_hash, protocol=PROTOCOL, strict=True):
    """指纹不一致（或历史记录未标记且 strict=True）时拒绝混跑。"""
    problems = []
    for i, r in enumerate(records):
        tag = r.get("case_id") or r.get("key") or ("#%d" % i)
        for field, want in (("protocol", protocol), ("config_hash", cfg_hash),
                            ("library_hash", lib_hash)):
            got = r.get(field)
            if got is None:
                if strict:
                    problems.append("%s 缺少 %s（历史未标记数据不能与新基线混跑）"
                                    % (tag, field))
            elif got != want:
                problems.append("%s 的 %s 不一致：%s != %s" % (tag, field, got, want))
    if problems:
        raise BaselineMismatch("拒绝混跑，发现 %d 处指纹问题：\n  %s"
                               % (len(problems), "\n  ".join(problems[:20])))
    return True


def check_resume(records, *, cfg_hash, lib_hash=None, allow_legacy=False):
    """续跑前的基线核对。

    - 有指纹但不一致 → 明确拒绝（不同配置 / 不同技能库不能算同一次实验）
    - 没指纹（旧版本产物）→ 默认拒绝，允许时标 mixed，报告要写清是混合基线
    """
    foreign = [r for r in records
               if r.get("config_hash") and r["config_hash"] != cfg_hash]
    if lib_hash:
        foreign += [r for r in records
                    if r.get("library_hash") and r.get("config_hash") == cfg_hash
                    and r["library_hash"] != lib_hash]
    unmarked = [r for r in records if not r.get("config_hash")]
    if foreign:
        return {"ok": False, "reason": "foreign", "foreign": len(foreign),
                "unmarked": len(unmarked), "mixed": False}
    if unmarked and not allow_legacy:
        return {"ok": False, "reason": "unmarked", "foreign": 0,
                "unmarked": len(unmarked), "mixed": False}
    return {"ok": True, "reason": None, "foreign": 0,
            "unmarked": len(unmarked), "mixed": bool(unmarked)}


def load_runs(path, manifest, *, strict=True):
    """读 runs.jsonl 并按 manifest 指纹校验，防止把两次运行拼起来算成功率。"""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    assert_no_mixing(records, cfg_hash=manifest["config_hash"],
                     lib_hash=manifest["library_hash"],
                     protocol=manifest.get("protocol", PROTOCOL), strict=strict)
    return records


def check_denominators(metrics):
    """检查单故障 / 复合 / 难度组的分母与汇总是否自洽。

    报告应该引用这些数字，而不是另手抄一套成功率。
    """
    problems = []
    suites = [k for k in ("single", "composite", "hard") if isinstance(metrics.get(k), dict)]
    fg = metrics.get("four_groups")
    if isinstance(fg, dict) and "n" in fg:
        # mock 闭环的结构是 four_groups = {"n": N, "rates": {...}}，
        # 但也要接受每个模式自带 n/counts 的写法。
        n = fg["n"]
        for m in MODES:
            v = fg.get(m)
            if isinstance(v, dict) and v.get("n") is not None and v["n"] != n:
                problems.append("four_groups.%s 分母 %s != %s" % (m, v["n"], n))
        counts = fg.get("counts")
        if isinstance(counts, dict):
            for m in MODES:
                if counts.get(m) is not None and counts[m] > n:
                    problems.append("four_groups.%s 成功数 %s > n %s" % (m, counts[m], n))
        rates = fg.get("rates")
        if isinstance(rates, dict) and isinstance(counts, dict) and n:
            for m in MODES:
                if rates.get(m) is None or counts.get(m) is None:
                    continue
                want = round(100.0 * counts[m] / n, 1)
                if abs(float(rates[m]) - want) > 0.11:
                    problems.append("four_groups.%s rate %s 与 counts/n %s 不符"
                                    % (m, rates[m], want))
    if not suites:
        return {"ok": not problems, "problems": problems, "suites": []}
    for suite in suites:
        s = metrics[suite]
        n = s.get("cases", s.get("n"))     # mock 用 n，真实评测用 cases
        if n is None:
            problems.append("%s 缺分母（cases / n）" % suite)
            continue
        for mode, v in (s.get("modes") or {}).items():
            if not isinstance(v, dict):
                continue
            if v.get("n") != n:
                problems.append("%s/%s 分母 %s != %s" % (suite, mode, v.get("n"), n))
            parts = sum(v.get(k, 0) or 0 for k in ("ok", "failed", "unjudgeable"))
            if parts != v.get("n"):
                problems.append("%s/%s 分子之和 %s != n %s" % (suite, mode, parts, v.get("n")))
            rate = round(100.0 * (v.get("ok", 0) or 0) / n, 1) if n else None
            if rate is not None and v.get("rate") is not None and abs(rate - v["rate"]) > 0.11:
                problems.append("%s/%s 报告 rate %s 与 ok/n %s 不符"
                                % (suite, mode, v["rate"], rate))
        # mock 的简化结构：counts 是成功数、rates 是百分比，没有 modes 层。
        counts, rates = s.get("counts") or {}, s.get("rates") or {}
        if isinstance(counts, dict) and isinstance(rates, dict):
            for mode, ok in counts.items():
                if n and isinstance(ok, (int, float)) and ok > n:
                    problems.append("%s/%s 成功数 %s > n %s" % (suite, mode, ok, n))
                if n and isinstance(ok, (int, float)) and rates.get(mode) is not None:
                    want = round(100.0 * ok / n, 1)
                    if abs(float(rates[mode]) - want) > 0.11:
                        problems.append("%s/%s rate %s 与 counts/n %s 不符"
                                        % (suite, mode, rates[mode], want))
    return {"ok": not problems, "problems": problems, "suites": suites}


def summarize(metrics):
    """从同一份 metrics 生成报告该用的那组数字，附分母自洽检查。"""
    out = {"denominators": check_denominators(metrics), "suites": {}}
    for suite in ("single", "composite", "hard"):
        s = metrics.get(suite)
        if isinstance(s, dict):
            out["suites"][suite] = {
                "cases": s.get("cases", s.get("n")),
                "rates": {m: (v.get("rate") if isinstance(v, dict) else v)
                          for m, v in (s.get("modes") or s.get("rates") or {}).items()}}
    if isinstance(metrics.get("four_groups"), dict):
        out["suites"]["four_groups"] = {"rates": metrics["four_groups"]}
    if isinstance(metrics.get("composite"), dict) and "rates" not in str(metrics["composite"]):
        out["suites"]["composite_simple"] = metrics["composite"]
    return out


def render_mode_section(metrics, summary):
    """报告里「每个模式真正用了什么策略和技能」一节，数字与说明都来自 metrics。

    两种 metrics 结构都要能出这段话：真实评测带 meta.skillset（双库），
    mock 闭环只有 libraries / skills（C/D 共用同一个实验库）。
    """
    meta = metrics.get("meta") or {}
    skillset = meta.get("skillset") or {}
    libraries = metrics.get("libraries") or {}
    if skillset.get("kind") == "two_libs" or skillset.get("d_path") or skillset.get("c_path"):
        c_desc = "%s（%s 条）" % (skillset.get("c_path", "library_c.db"), skillset.get("c"))
        d_desc = "%s（%s 条）" % (skillset.get("d_path", "library_d.db"), skillset.get("d"))
    elif libraries:
        total = sum(int(v.get("skills") or 0) for v in libraries.values()
                    if isinstance(v, dict))
        c_desc = "与 D 共用同一实验库（%d 条）" % total
        d_desc = "与 C 共用同一实验库（%d 条）" % total
    else:
        c_desc = d_desc = "未登记技能库（历史产物，见 manifest）"
    lines = [
        "## 模式实际使用的策略与技能",
        "",
        "> 本节由 manifest 模块从本次 metrics 生成，不手工维护另一套数字。",
        "",
        "| 模式 | 恢复策略来源 | 技能库 |",
        "|---|---|---|",
        "| A | 无恢复 | 无 |",
        "| B | 诊断 + 固定规则表（一律 retry） | 无 |",
        "| C | 候选 + 已启用技能，仅按 agent 匹配 | %s |" % c_desc,
        "| D | 已启用技能，agent + fault_type 严格匹配 | %s |" % d_desc,
        "",
        "配置：backend=%s，model=%s，协议=%s，配置指纹=%s"
        % (meta.get("backend") or ("mock" if metrics.get("config", {}).get("backend")
                                   == "mock" else "?"),
           meta.get("model") or "-",
           metrics.get("protocol_version") or meta.get("protocol", "?"),
           (metrics.get("config_hash") or "见 manifest")),
        "",
    ]
    den = summary["denominators"]
    lines.append("分母自洽检查：%s（%s）"
                 % ("通过" if den["ok"] else "未通过",
                    "、".join(den["problems"][:5]) or "无异常"))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="冻结 / 校验可复现基线")
    ap.add_argument("action", choices=("freeze", "verify", "check"))
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument("--run-dir", help="运行输出目录（freeze）")
    ap.add_argument("--manifest", help="manifest.json 路径（verify/check）")
    ap.add_argument("--out", help="manifest 输出路径（freeze，默认写进 run-dir）")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    if args.action == "freeze":
        if not args.run_dir:
            ap.error("freeze 需要 --run-dir")
        m = freeze(root, args.run_dir, note=args.note, manifest_path=args.out)
        print("[√] manifest 已写入 %s" % m["manifest_path"])
        print("    总指纹 %s ｜ 配置 %s ｜ 技能库 %s"
              % (m["fingerprint"][:16], m["config_hash"][:16], m["library_hash"][:16]))
        return 0
    path = args.manifest or os.path.join(args.run_dir or "", MANIFEST_NAME)
    res = verify(root, path)
    print("%s ｜ 产物差异 %d 处 ｜ 代码漂移 %d 个文件"
          % ("[√] 产物与基线一致" if res["ok"] else "[×] 产物与基线不一致",
             len(res["diffs"]), len(res["code_drift"])))
    for d in res["diffs"]:
        print("  -", d)
    if res["code_drift"]:
        print("  [i] 以下代码在冻结之后改过 —— 这份报告属于历史版本，"
              "不能声称由当前代码产生：")
        for d in res["code_drift"]:
            print("      -", d)
    if args.action == "check":
        mpath = os.path.join(root, json.load(open(path, encoding="utf-8"))["run_dir"],
                             "metrics.json")
        if os.path.exists(mpath):
            with open(mpath, encoding="utf-8") as f:
                den = check_denominators(json.load(f))
            print("分母自洽：%s" % ("通过" if den["ok"] else "未通过"))
            for p in den["problems"]:
                print("  -", p)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
