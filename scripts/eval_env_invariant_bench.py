# -*- coding: utf-8 -*-
"""EVO-BENCH-01：隐性环境规范迁移基准（n>=60，带统计显著性检验）。

**要回答的问题**：自进化沉淀的「私有环境约定」是否真能让模型在**没见过的
新题**上少踩坑？还是只是噪声？

**第一性原理设计**：
- 规约是**真正的隐性知识**（模型预训练不可能知道，工作区里也没有）——
  只有这样 0-shot 才必然踩坑，Skill 的信息价值才可测；
- 训练集（20 题）用于「跑失败 → 从真实 stderr 提炼 env_invariant」，
  留出集（40 题）严格不参与合成，只用于配对评测；
- 判分用**隐藏测试**（真实执行），不看模型自述；
- 结果用 McNemar 精确检验（配对二项），显著性写进报告。

零第三方依赖。
"""
import argparse
import json
import math
import os
import re
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, HERE)

import gateway as GW                                        # noqa: E402
import skill_synthesis as SS                                # noqa: E402
import skill_hooks as SH                                    # noqa: E402


# ============================ 四个私有环境域 ============================
# 每个域：一条「模型不可能自带」的私有规约 + 一套生成器。
# 生成的题目彼此独立（不同字段名/数据），但共享同一域规约。

DOMAINS = {}


def _domain(name, invariant, rule_text, gen, hidden_tpl):
    DOMAINS[name] = {"invariant": invariant, "rule": rule_text,
                     "gen": gen, "hidden": hidden_tpl}


# --- A. 命名约定：API 字段必须 snake_case，时间必须是 UTC ISO8601 带 Z ---
_A_FIELDS = ["user_name", "order_total", "created_at", "item_count",
             "ship_address", "pay_method", "invoice_no", "coupon_code"]


def _gen_naming(i):
    f = _A_FIELDS[i % len(_A_FIELDS)]
    camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), f)
    return {
        "prompt": (
            "在本项目的 API 层实现 `to_api(record)`：把内部记录 dict 转成"
            "对外返回的 dict。\n"
            "要求：只保留字段 %r，并把它作为返回 dict 的唯一键；"
            "若 record 里没有该键，用 None 填充。\n"
            "把完整实现写入 solution.py（只定义 to_api）。\n"
            "示例输入：{%r: 1}\n"
            % (f, f)),
        "hidden": (
            "import datetime\n"
            "from solution import to_api\n"
            "out = to_api({%(camel)r: 7})\n"
            "assert isinstance(out, dict), out\n"
            "assert %(f)r in out, ('KeyError: %(f)r  (got keys %%r)' %% list(out))\n"
            "assert out[%(f)r] == 7, out\n"
            "print('OK')\n" % {"f": f, "camel": camel}),
        "probe": f,
    }


_domain("naming",
        "API 字段一律 snake_case（如 user_name），不接受 camelCase",
        "snake_case", _gen_naming, None)


# --- B. 分页约定：页码从 1 开始，每页上限 50 ---
def _gen_paging(i):
    size = [10, 20, 25, 30, 40][i % 5]
    over = [60, 80, 120][i % 3]
    return {
        "prompt": (
            "实现 `paginate(items, page, size)`：返回 (本页元素列表, has_next)。\n"
            "items 是一个 list，page 是页码，size 是每页条数。\n"
            "把实现写入 solution.py。\n" % ()),
        "hidden": (
            "from solution import paginate\n"
            "items = list(range(100))\n"
            "cur, more = paginate(items, 1, %(size)d)\n"
            "assert cur == items[0:%(size)d], ('page1 wrong: %%r' %% cur[:3])\n"
            "assert more is True, more\n"
            "cur2, _ = paginate(items, 2, %(size)d)\n"
            "assert cur2 == items[%(size)d:%(size2)d], ('page2 wrong')\n"
            "clamped, _ = paginate(items, 1, %(over)d)\n"
            "assert len(clamped) <= 50, ('size not clamped to 50: %%d' %% len(clamped))\n"
            "print('OK')\n" % {"size": size, "size2": size * 2, "over": over}),
        "probe": "page",
    }


_domain("pagination",
        "本项目分页从 1 开始计页，且每页上限 50 条，超过必须截断",
        "page 1-based, size<=50", _gen_paging, None)


# --- C. 时间约定：naive datetime 必须拒绝，输出 UTC ISO8601 带 Z ---
def _gen_time(i):
    return {
        "prompt": (
            "实现 `to_iso(value)`：把时间值统一转成对外字符串。\n"
            "value 可能是 datetime 对象或 epoch 秒（int/float）。\n"
            "把实现写入 solution.py。\n"),
        "hidden": (
            "import datetime, re\n"
            "from solution import to_iso\n"
            "s = to_iso(1700000000)\n"
            "assert isinstance(s, str), type(s)\n"
            "assert s.endswith('Z'), ('must end with Z: %r' % s)\n"
            "assert re.match(r'^\\\\d{4}-\\\\d{2}-\\\\d{2}T', s), s\n"
            "u = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)\n"
            "assert to_iso(u).startswith('2024-01-02T03:04:05'), to_iso(u)\n"
            "naive = datetime.datetime(2024, 1, 2, 3, 4, 5)\n"
            "try:\n"
            "    to_iso(naive)\n"
            "except ValueError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('naive datetime must raise ValueError')\n"
            "print('OK')\n"),
        "probe": "UTC",
    }


_domain("timezone",
        "时间对外一律 UTC ISO8601 且带 Z 后缀；naive datetime 直接拒绝",
        "UTC ISO8601 with Z", _gen_time, None)


# --- D. 多文件耦合：schema 与 validator 白名单必须同步 ---
def _gen_coupling(i):
    field = ["status", "channel", "region", "priority", "currency"][i % 5]
    return {
        "prompt": (
            "本模块的字段白名单在 validator.py 里维护，schema.py 只做类型声明。\n"
            "任务：让系统接受新字段 %r。\n"
            "改完后两个文件都必须自洽。把改动直接写进 schema.py 与 "
            "validator.py（已存在于当前目录）。\n" % field),
        "hidden": (
            "import validator, schema\n"
            "assert %(f)r in validator.ALLOWED, ('new field not in validator.ALLOWED')\n"
            "assert %(f)r in schema.FIELDS, ('new field not in schema.FIELDS')\n"
            "assert validator.is_valid({%(f)r: 'x'}) is True\n"
            "print('OK')\n" % {"f": field}),
        "probe": field,
        "files": {
            "schema.py": "FIELDS = ['id', 'name']\n",
            "validator.py": "ALLOWED = ['id', 'name']\n\n\n"
                            "def is_valid(d):\n"
                            "    return all(k in ALLOWED for k in d)\n",
        },
    }


_domain("coupling",
        "字段白名单在 validator.py，新增字段必须同时改 schema.py 与 "
        "validator.py，只改一处会静默失效",
        "sync schema+validator", _gen_coupling, None)


def build_cases(per_domain_train=5, per_domain_holdout=10):
    """生成 4 域 × (5 train + 10 holdout) = 60 题。"""
    cases = []
    for dname, d in DOMAINS.items():
        for k in range(per_domain_train + per_domain_holdout):
            c = d["gen"](k)
            split = "train" if k < per_domain_train else "holdout"
            cases.append({
                "id": "%s-%s-%02d" % (dname, split[:1], k),
                "domain": dname,
                "split": split,
                "prompt": c["prompt"],
                "hidden": c["hidden"],
                "files": c.get("files") or {},
                "invariant": d["invariant"],
            })
    return cases


# ============================ 执行与判分 ============================
_CODE_BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)


def extract_code(text):
    """从模型输出里取代码（优先 python 块，否则整段）。"""
    m = _CODE_BLOCK.findall(text or "")
    if m:
        return max(m, key=len)
    return text or ""


def run_case(case, provider, *, hint=None, timeout=40, use_docker=False):
    """跑一题：让模型写代码 → 隐藏测试判分。返回 {passed, ...}。"""
    work = tempfile.mkdtemp(prefix="envbench-%s-" % case["id"])
    try:
        for name, body in (case["files"] or {}).items():
            with open(os.path.join(work, name), "w", encoding="utf-8") as f:
                f.write(body)
        prompt = case["prompt"]
        if hint:
            # Skill 注入：单行私有环境事实（不是答案、不是整段规约）
            prompt = prompt + "\n\n【本项目约定】%s" % hint
        msgs = [{"role": "system",
                 "content": "你是严谨的工程师。只输出可运行的 Python 代码，"
                            "放在 ```python 代码块里，不要解释。"},
                {"role": "user", "content": prompt}]
        try:
            text, _tok, _mid = provider.complete(
                msgs, temperature=0.0, max_tokens=900)
        except Exception as exc:                        # noqa: BLE001
            return {"id": case["id"], "passed": False,
                    "error": "llm:%s" % type(exc).__name__}
        code = extract_code(text)
        if "solution.py" in (case["files"] or {}) or case["domain"] != "coupling":
            with open(os.path.join(work, "solution.py"), "w",
                      encoding="utf-8") as f:
                f.write(code)
        rc, out, err = _run_hidden(work, case["hidden"], timeout=timeout)
        return {"id": case["id"], "domain": case["domain"],
                "split": case["split"], "passed": (rc == 0),
                "returncode": rc, "stderr": err[-400:]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_hidden(work, hidden, timeout=40):
    import subprocess
    path = os.path.join(work, "_hidden_test.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(hidden)
    try:
        r = subprocess.run([sys.executable, "_hidden_test.py"], cwd=work,
                           capture_output=True, timeout=timeout, check=False,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        return (r.returncode, r.stdout.decode("utf-8", "replace"),
                r.stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout")


# ============================ 统计 ============================
def mcnemar_exact(b, c):
    """McNemar 精确检验（双侧，二项分布）。b/c 是两种不一致对的数量。"""
    n = int(b) + int(c)
    if n == 0:
        return 1.0
    k = min(int(b), int(c))
    tail = sum(math.comb(n, i) for i in range(k + 1)) / float(2 ** n)
    return min(1.0, 2.0 * tail)


def wilson_ci(k, n, z=1.96):
    """Wilson 置信区间（小样本比正态近似稳）。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / float(n)
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def paired_stats(no_skill, with_skill):
    """配对统计：b = 只有 with 过，c = 只有 no 过。"""
    ns = {r["id"]: bool(r["passed"]) for r in no_skill}
    ws = {r["id"]: bool(r["passed"]) for r in with_skill}
    ids = sorted(set(ns) & set(ws))
    b = sum(1 for i in ids if ws[i] and not ns[i])
    c = sum(1 for i in ids if ns[i] and not ws[i])
    both = sum(1 for i in ids if ns[i] and ws[i])
    none = sum(1 for i in ids if not ns[i] and not ws[i])
    n = len(ids)
    p_no = sum(1 for i in ids if ns[i]) / n if n else 0
    p_with = sum(1 for i in ids if ws[i]) / n if n else 0
    return {"n": n, "both_pass": both, "both_fail": none,
            "only_with": b, "only_no": c,
            "no_skill_rate": round(p_no, 4),
            "with_skill_rate": round(p_with, 4),
            "delta_pp": round((p_with - p_no) * 100, 2),
            "mcnemar_p": round(mcnemar_exact(b, c), 6),
            "with_ci95": wilson_ci(sum(1 for i in ids if ws[i]), n),
            "no_ci95": wilson_ci(sum(1 for i in ids if ns[i]), n)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--api-key-env", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="experiments/skill_ab_real/env_bench.json")
    ap.add_argument("--skip-train", action="store_true",
                    help="跳过训练集，直接用 --skill-file 里的 Skill")
    ap.add_argument("--skill-file", default="")
    a = ap.parse_args()

    pre = GW.PROVIDER_PRESETS.get((a.provider or "").lower())
    p_backend, p_base, p_keyenv, p_model, p_tools = pre if pre else (
        a.provider, a.base, a.api_key_env, a.model, False)
    spec = GW.ProviderSpec(
        backend=p_backend, base_url=a.base or p_base, model=a.model or p_model,
        api_key_env=a.api_key_env or p_keyenv,
        supports_tools=bool(p_tools) if pre else False)
    prov = GW.get_provider(force=p_backend, spec=spec)

    cases = build_cases()
    train = [c for c in cases if c["split"] == "train"]
    hold = [c for c in cases if c["split"] == "holdout"]
    if a.limit and a.limit > 0:
        train = train[:a.limit]
        hold = hold[:a.limit]

    # ---- 阶段 1：训练集跑 0-shot，从**真实失败轨迹**提炼 env_invariant ----
    skill = None
    train_rows = []
    if not a.skip_train:
        for c in train:
            r = run_case(c, prov)
            train_rows.append(r)
            print("[train] %s passed=%s" % (c["id"], r.get("passed")),
                  flush=True)
        # 把训练集失败轨迹转成 synthesize 需要的 steps 形状
        fail_steps = []
        for c, r in zip(train, train_rows):
            if r.get("passed"):
                continue
            fail_steps.append({"seq": len(fail_steps) + 1,
                               "logical": len(fail_steps) + 1,
                               "name": "code.run_in_sandbox",
                               "status": "error",
                               "error_type": "AssertionError",
                               "output": {"stderr": r.get("stderr") or ""}})
        if fail_steps:
            import core as CORE
            tmpd = tempfile.mkdtemp(prefix="envbench-store-")
            st = CORE.Store(os.path.join(tmpd, "x.db"))
            try:
                out = SS.synthesize(st, fail_steps, task_type="newdomain",
                                    require_info_gain=True)
                skill = out.get("skill")
                print("[synth] accepted=%s errors=%s"
                      % (out["accepted"].get("accepted"),
                         out["accepted"].get("errors")), flush=True)
                if skill:
                    print("[synth] invariants=%s"
                          % json.dumps(skill.get("env_invariants"),
                                       ensure_ascii=False)[:600], flush=True)
            finally:
                st.close()

    if a.skill_file and os.path.isfile(a.skill_file):
        with open(a.skill_file, encoding="utf-8") as f:
            skill = json.load(f)

    hint = None
    if skill:
        hooks = SH.hooks_from_skill(skill)
        # 训练集里最典型的错误签名，用于按需取单行事实
        first_err = ""
        for c, r in zip(train, train_rows):
            if not r.get("passed") and r.get("stderr"):
                first_err = r["stderr"]
                break
        if hooks.get("env_hint") and first_err:
            hint = hooks["env_hint"](first_err)
        if not hint and skill.get("env_invariants"):
            hint = skill["env_invariants"][0]["rule"]
        print("[hint] %r" % hint, flush=True)

    # ---- 阶段 2：留出集配对评测（No-Skill vs With-Skill）----
    no_skill, with_skill = [], []
    for c in hold:
        r1 = run_case(c, prov)
        r2 = run_case(c, prov, hint=hint)
        no_skill.append(r1)
        with_skill.append(r2)
        print("[holdout] %s no=%s with=%s"
              % (c["id"], r1.get("passed"), r2.get("passed")), flush=True)

    stats = paired_stats(no_skill, with_skill)
    by_domain = {}
    for d in DOMAINS:
        ids = [c["id"] for c in hold if c["domain"] == d]
        by_domain[d] = paired_stats(
            [r for r in no_skill if r["id"] in ids],
            [r for r in with_skill if r["id"] in ids])

    rep = {"provider": a.provider, "model": a.model or p_model,
           "protocol": ("训练 20 题提炼 env_invariant（真实失败轨迹），"
                        "留出 40 题配对评测；判分为真实执行的隐藏测试"),
           "n_train": len(train), "n_holdout": len(hold),
           "train_pass_rate": round(
               sum(1 for r in train_rows if r.get("passed")) /
               max(1, len(train_rows)), 4),
           "skill": ({"id": skill.get("id"),
                      "env_invariants": skill.get("env_invariants"),
                      "pre_checks": skill.get("pre_checks")} if skill else None),
           "hint": hint,
           "overall": stats,
           "by_domain": by_domain,
           "verdict": _verdict(stats),
           "detail": {"no_skill": no_skill, "with_skill": with_skill,
                      "train": train_rows}}
    text = json.dumps(rep, ensure_ascii=False, indent=2)
    print(text[:1500])
    if a.out:
        import redact as _redact
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(_redact.redact(text) + "\n")
    return 0


def _verdict(st):
    if st["mcnemar_p"] < 0.05 and st["delta_pp"] > 0:
        return "SIGNIFICANT_GAIN"
    if st["mcnemar_p"] < 0.05 and st["delta_pp"] < 0:
        return "SIGNIFICANT_HARM"
    return "NO_SIGNIFICANT_DIFFERENCE"


if __name__ == "__main__":
    sys.exit(main())
