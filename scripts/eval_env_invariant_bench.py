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
import sandbox_executor as SBX                              # noqa: E402


# ============================ 四个私有环境域 ============================
# 每个域：一条「模型不可能自带」的私有规约 + 一套生成器。
# 生成的题目彼此独立（不同字段名/数据），但共享同一域规约。

DOMAINS = {}


def _domain(name, invariant, rule_text, gen, hidden_tpl):
    DOMAINS[name] = {"invariant": invariant, "rule": rule_text,
                     "gen": gen, "hidden": hidden_tpl}


# --- A. 金额单位约定：对外展示也用「分」，不做 /100 换算 ---
def _gen_unit(i):
    amt = [12345, 99900, 250, 7, 100000, 45600, 3000][i % 7]
    return {
        "prompt": (
            "实现 `format_price(cents)`：把内部金额格式化成对外展示的字符串。\n"
            "参数 cents 是 int，表示金额。返回 str。\n"
            "把实现写入 solution.py。\n"),
        "hidden": (
            "from solution import format_price\n"
            "s = format_price({amt})\n"
            "assert isinstance(s, str), type(s)\n"
            "assert '{amt}' in s, ('unit mismatch: expected cents %s, got %r"
            " -> did you convert to yuan?' % ('{amt}', s))\n"
            "assert '{yuan}' not in s, ('looks like yuan conversion"
            " (divide by 100): %r' % s)\n"
            "print('OK')\n").format(amt=amt, yuan="%.2f" % (amt / 100.0)),
        "probe": str(amt),
    }


_domain("unit",
        "对外展示的金额单位同样是「分」，不做除以 100 的换算",
        "unit is cents", _gen_unit, None)


# --- B. 分页约定：页码从 1 开始，每页上限 50 ---
def _gen_paging(i):
    size = [10, 20, 25, 30, 40][i % 5]
    return {
        "prompt": (
            "实现 `paginate(items, cursor, limit)`：游标分页，返回"
            " (本页元素, next_cursor)。\n"
            "cursor 为 None 表示第一页。items 是 list。\n"
            "把实现写入 solution.py。\n"),
        "hidden": (
            "problems = []\n"
            "from solution import paginate\n"
            "items = list(range(100))\n"
            "try:\n"
            "    page, nxt = paginate(items, None, {size})\n"
            "except Exception as exc:\n"
            "    problems.append('first page failed: %r' % exc)\n"
            "else:\n"
            "    if page != items[0:{size}]:\n"
            "        problems.append('wrong first page')\n"
            "    if not (isinstance(nxt, str) and nxt.startswith('c_')):\n"
            "        problems.append('cursor must start with c_ prefix, got %r' % nxt)\n"
            "    else:\n"
            "        try:\n"
            "            page2, nxt2 = paginate(items, nxt, {size})\n"
            "        except Exception as exc:\n"
            "            problems.append('cursor not accepted: %r' % exc)\n"
            "        else:\n"
            "            if page2 != items[{size}:{size2}]:\n"
            "                problems.append('cursor did not advance')\n"
            "            if nxt2 is None:\n"
            "                problems.append('non-last page must have a cursor')\n"
            "            # 尾页判定必须用真正走到末尾的短列表（100 条的第 2 页不是尾页）\n"
            "short = list(range(20))\n"
            "_, sn1 = paginate(short, None, {size})\n"
            "sp2, sn2 = paginate(short, sn1, {size})\n"
            "if sn2 is not None:\n"
            "    problems.append('last page must return next_cursor=None')\n"
            "try:\n"
            "    paginate(items, None, 38)\n"
            "except Exception:\n"
            "    pass\n"
            "else:\n"
            "    problems.append('limit exceeds 37 must raise ValueError')\n"
            "try:\n"
            "    paginate(items, 'bad', {size})\n"
            "except Exception:\n"
            "    pass\n"
            "else:\n"
            "    problems.append('invalid cursor must raise ValueError')\n"
            "if problems:\n"
            "    raise AssertionError('; '.join(problems))\n"
            "print('OK')\n").format(size=size, size2=size * 2),
        "probe": "cursor",
    }


_domain("pagination",
        "本项目游标分页：cursor 必须以 c_ 开头、尾页 next_cursor 必须是 None"
        "（不是空串）、每页上限是反直觉的 37 条（不是 50/100），"
        "超过与非法 cursor 一律抛 ValueError",
        "c_ cursor + None tail + limit<=37", _gen_paging, None)


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
            "assert s.endswith('Z'), ('timestamp must be ISO8601 UTC ending with Z: %r' % s)\n"
            "assert re.match(r'^\\d{4}-\\d{2}-\\d{2}T', s), s\n"
            "u = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)\n"
            "assert to_iso(u).startswith('2024-01-02T03:04:05'), to_iso(u)\n"
            "naive = datetime.datetime(2024, 1, 2, 3, 4, 5)\n"
            "try:\n"
            "    to_iso(naive)\n"
            "except ValueError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('naive datetime (no timezone) must raise ValueError')\n"
            "print('OK')\n"),
        "probe": "UTC",
    }


_domain("timezone",
        "时间对外一律 UTC ISO8601 且带 Z 后缀；naive datetime 直接拒绝",
        "UTC ISO8601 with Z", _gen_time, None)


# --- D. 多文件耦合：schema 与 validator 白名单必须同步 ---
def _gen_coupling(i):
    field = ["status", "channel", "region", "priority", "currency"][i % 5]
    registry_src = (
        "# 字段声明\n"
        "FIELDS = ['id', 'name']\n"
        "# 必填字段表：值为 True 表示该字段不允许为空\n"
        "REQUIRED = {'id': True, 'name': True}\n"
        "\n\n"
        "def is_valid(d):\n"
        "    for k, v in d.items():\n"
        "        if k not in FIELDS:\n"
        "            return False\n"
        "        if REQUIRED.get(k) and v in (None, ''):\n"
        "            return False\n"
        "    return True\n")
    return {
        "prompt": (
            "下面是本项目的 registry.py：\n"
            "```python\n%s```\n"
            "请把完整实现（含 add_field）写回 registry.py，调用后让新字段在\n"
            "系统中与既有字段**同等对待**地正式生效。\n"
            "注意：登记处都是 list / dict（list 没有 add 方法）。\n"
            "只输出一个 ```python 代码块，内容是完整的 registry.py。\n"
            % registry_src),
        "hidden": (
            "import registry\n"
            "registry.add_field({f!r})\n"
            "assert {f!r} in registry.FIELDS, ('FIELDS missing field')\n"
            "assert registry.is_valid({{{f!r}: 'x'}}) is True\n"
            "assert registry.is_valid({{{f!r}: ''}}) is False, (\n"
            "    'field missing from REQUIRED table -> empty value accepted, '\n"
            "    'not treated like existing fields')\n"
            "print('OK')\n").format(f=field),
        "probe": field,
        "target_file": "registry.py",
        "assertions": [{
            "kind": "ast_symbol_sync", "file": "registry.py",
            "source_symbol": "FIELDS", "mirror_symbol": "REQUIRED",
            "message": "FIELDS 改了但 REQUIRED 没同步",
        }],
        "files": {"registry.py": registry_src},
    }


_domain("coupling",
        "新字段必须同时加入 FIELDS 与 ALLOWED 两处；只改 FIELDS 会静默失效",
        "sync FIELDS+ALLOWED", _gen_coupling, None)


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
                "target_file": c.get("target_file") or "solution.py",
                "assertions": c.get("assertions") or [],
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


def _symbol_lines(path):
    """AST 只读查模块级符号定义行号（失败返回空 dict）。"""
    import ast as _ast
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            tree = _ast.parse(f.read())
        for node in tree.body:
            names = []
            if isinstance(node, _ast.Assign):
                names = [t.id for t in node.targets
                         if isinstance(t, _ast.Name)]
            elif isinstance(node, _ast.AnnAssign) and \
                    isinstance(node.target, _ast.Name):
                names = [node.target.id]
            for n in names:
                out[n] = getattr(node, "lineno", 0)
    except Exception:                                      # noqa: BLE001
        pass
    return out


def coupling_probe(work, *, precise=False):
    """运行时契约探针：实际执行一次 add_field，检查两处登记是否同步。

    MOD-EVO-01 补救：静态 AST 断言看不到「运行时修改模块对象」，必须
    在运行时验证。只返回**检查结果**（哪个表没登记），不含修复代码，
    不是答案剧透；失败信息精确，便于模型一次修对。
    """
    import subprocess as _sp
    probe_src = (
        "import registry\n"
        "from solution import add_field\n"
        "p='__evoguard_probe__'\n"
        "add_field(p)\n"
        "bad=[]\n"
        "if p not in getattr(registry,'FIELDS',[]):\n"
        "    bad.append('FIELDS 未登记新字段')\n"
        "if not getattr(registry,'REQUIRED',{}).get(p):\n"
        "    bad.append('REQUIRED 未登记新字段（空值会被放行）')\n"
        "print('PROBE:'+('|'.join(bad) if bad else 'OK'))\n")
    path = os.path.join(work, "_coupling_probe.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(probe_src)
    try:
        r = _sp.run([sys.executable, "_coupling_probe.py"], cwd=work,
                    capture_output=True, timeout=20, check=False,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    except _sp.TimeoutExpired:
        return {"ok": False, "problems": ["探针超时"]}
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    line = next((x for x in out.splitlines() if x.startswith("PROBE:")), "")
    if line:
        detail = line[len("PROBE:"):]
        problems = [] if detail == "OK" else detail.split("|")
        if precise and problems:
            # ZERO-05-T2：用 AST 找真实定义行号（不执行代码），把
            # "哪个文件的哪一行少了哪个符号"直接告诉模型，别让它猜。
            loc = _symbol_lines(os.path.join(work, "registry.py"))
            decorated = []
            for msg in problems:
                if "FIELDS" in msg or "REQUIRED" in msg:
                    decorated.append(
                        "registry.py:L%s 定义 FIELDS、L%s 定义 REQUIRED；"
                        "两处必须同步（%s）"
                        % (loc.get("FIELDS", "?"),
                           loc.get("REQUIRED", "?"), msg))
                else:
                    decorated.append(msg)
            problems = decorated
        return {"ok": not problems, "problems": problems}
    return {"ok": False,
            "problems": [err.strip().splitlines()[-1][:140]
                         if err.strip() else "探针无输出"]}


def run_case(case, provider, *, hint=None, timeout=40, use_docker=False,
             assertions=None, root=None, max_runtime_fix=2,
             spec_hint=False):
    """跑一题：让模型写代码 → 隐藏测试判分。返回 {passed, ...}。"""
    work = tempfile.mkdtemp(prefix="envbench-%s-" % case["id"])
    try:
        for name, body in (case["files"] or {}).items():
            with open(os.path.join(work, name), "w", encoding="utf-8") as f:
                f.write(body)
        prompt = case["prompt"]
        if spec_hint and case.get("domain") == "coupling":
            prompt += ("\n\n【项目规约·字段登记】本系统一个字段由多处登记表"
                       "共同定义；新增字段只有在**所有**登记表（FIELDS 列表"
                       "与 REQUIRED 字典）都出现，才与既有字段同等生效。"
                       "少登记任何一处，校验会静默放过空值。")
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
        target = case.get("target_file") or "solution.py"
        sol = os.path.join(work, target)
        # DEEP-02-T1：写前门禁（Pre-Commit Gate）。不同步的拟写代码
        # **绝不落盘**，把精确缺失符号+行号+最小补丁回灌给模型重试。
        import skill_hooks as _SH
        # 显式传 [] 表示「无 Skill、无断言」；只有 None 才回退到题目默认
        checks = case.get("assertions") or [] if assertions is None else assertions
        fixed = 0
        verdict_errors = []
        if checks:
            pcv = _SH.run_pre_commit_check(
                checks, root=work, tool="fs.write",
                args={"path": target, "content": code})
            while pcv.get("blocked") and fixed < 2:
                feedback = json.dumps({
                    "error": "pre_commit_blocked",
                    "failures": pcv.get("failures") or [],
                    "fixes": pcv.get("fixes") or []}, ensure_ascii=False)
                repair = list(msgs) + [{"role": "user", "content":
                    "写前门禁拒绝落盘（工作区未被污染）。按 fixes 一次性修正"
                    "后重新输出完整代码：" + feedback}]
                try:
                    text2, _t2, _m2 = provider.complete(
                        repair, temperature=0.0, max_tokens=900)
                except Exception:                          # noqa: BLE001
                    break
                code = extract_code(text2)
                fixed += 1
                pcv = _SH.run_pre_commit_check(
                    checks, root=work, tool="fs.write",
                    args={"path": target, "content": code})
            verdict_errors = list(pcv.get("failures") or [])
        # 只有通过写前门禁（或没有断言）才允许落盘
        with open(sol, "w", encoding="utf-8") as f:
            f.write(code)
        sym = SBX.check_symbols(work, files=[target])
        verdict_errors += list(sym["errors"])
        rc, out, err = _run_hidden(work, case["hidden"], timeout=timeout)
        # MOD-AGENT-01-T1：失败后把**干净的 traceback**回灌一次（不是长篇
        # 反思模板）。这是正常修 bug 闭环，不是提示词剧透。
        runtime_fix = 0
        fix_reason = None
        while rc != 0 and runtime_fix < max_runtime_fix:
            feedback = err[-700:]
            if case.get("domain") == "coupling":
                # 运行时探针：把「哪个表没登记」这种精确检查结果回灌。
                # 只给检查结论，不给修复代码 —— 不是答案剧透。
                pr = coupling_probe(work, precise=True)
                if pr["problems"]:
                    feedback = ("运行时契约检查未通过：" +
                                json.dumps(pr["problems"], ensure_ascii=False)
                                + "\n测试错误：" + err[-300:])
            retry = list(msgs) + [
                {"role": "user", "content":
                 "上次实现未通过测试。请只输出修正后的完整 Python 代码：\n"
                 + feedback}]
            try:
                text3, _t3, _m3 = provider.complete(
                    retry, temperature=0.0, max_tokens=900)
            except Exception as exc:                       # noqa: BLE001
                # 不静默吞：API 失败必须和「模型修不对」分开记，
                # 否则会把基础设施故障算成模型能力不足。
                fix_reason = "api_error:" + type(exc).__name__
                break
            if not text3:
                fix_reason = "empty_model_output"
                break
            with open(sol, "w", encoding="utf-8") as f:
                f.write(extract_code(text3))
            rc, out, err = _run_hidden(work, case["hidden"],
                                       timeout=timeout)
            runtime_fix += 1
        return {"id": case["id"], "domain": case["domain"],
                "split": case["split"], "passed": (rc == 0),
                "returncode": rc, "stderr": err[-400:],
                "assertion_repairs": fixed,
                "assertion_ok": not verdict_errors,
                "runtime_fix": runtime_fix,
                "fix_reason": fix_reason}
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
    ap.add_argument("--spec-hint", dest="spec_hint", action="store_true",
                    default=True,
                    help="coupling 域显式注入「字段需在多个登记表同步」规约"
                         "（默认开；--no-spec-hint 复现旧口径）")
    ap.add_argument("--no-spec-hint", dest="spec_hint",
                    action="store_false", help="关闭规约明示，复现旧 56.7%% 口径")
    ap.add_argument("--repeat", type=int, default=1,
                    help="重复轮数（消除单模型单轮噪声，MOD-AGENT-01-T3）")
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

    # ---- 阶段 1：按域训练 0-shot，从**真实失败轨迹**提炼 env_invariant ----
    # 真实系统里 Skill 按 task_type/fault_type 检索，因此每个域独立合成一个
    # Skill，留出评测时按题所属域注入对应 Skill（不跨域混用）。
    import core as CORE
    tmpd = tempfile.mkdtemp(prefix="envbench-store-")
    st = CORE.Store(os.path.join(tmpd, "x.db"))
    skills, hints, train_rows = {}, {}, []
    assertions_by_domain = {}
    try:
        for d in DOMAINS:
            d_train = [c for c in train if c["domain"] == d]
            d_rows, fail_steps = [], []
            sk = None
            # NEXT-01：多轮累积探索。第 1 轮 0-shot，之后每轮携带已学到的
            # 全部规约重跑训练集，逼出下一层断言；只在训练集内迭代。
            for rnd in range(1, 4):
                cur_hint = None
                if sk and SS.active_invariants(sk):
                    cur_hint = "；".join(
                        str(x.get("rule") or "")
                        for x in SS.active_invariants(sk))
                # 训练集也关闭运行时自愈：否则模型靠反馈自己修对，
                # 后续轮次就暴露不出下一层规约（多轮累积会空转）。
                round_rows = [run_case(c, prov, hint=cur_hint,
                                       spec_hint=a.spec_hint,
                                       max_runtime_fix=0)
                              for c in d_train]
                d_rows.extend(round_rows)
                if rnd == 1:
                    train_rows.extend(round_rows)
                fresh = [{"seq": len(fail_steps) + i + 1,
                          "logical": len(fail_steps) + i + 1,
                          "name": "code.run_in_sandbox",
                          "status": "error",
                          "error_type": "AssertionError",
                          "output": {"stderr": r.get("stderr") or ""}}
                         for i, r in enumerate(round_rows)
                         if not r.get("passed")]
                fail_steps.extend(fresh)
                if rnd == 1:
                    out = {"accepted": {"accepted": False}}
                    if fresh:
                        out = SS.synthesize(st, fail_steps, task_type=d,
                                            require_info_gain=True)
                        sk = out.get("skill")
                        if sk:
                            SS._sk.save(st, sk)
                    print("[synth:%s] r1 accepted=%s inv=%s"
                          % (d, out["accepted"].get("accepted"),
                             len((sk or {}).get("env_invariants") or [])),
                          flush=True)
                elif fresh and sk:
                    ref = SS.refine_skill(st, sk["id"], fresh,
                                          require_info_gain=True)
                    if ref["accepted"]["accepted"]:
                        sk = ref["skill"]
                    print("[refine:%s] r%d added=%s total_inv=%s"
                          % (d, rnd, len(ref["added"]),
                             len((sk or {}).get("env_invariants") or [])),
                          flush=True)
                if not fresh:
                    break
            skills[d] = sk
            # 完整规约集：**所有**累积规约一次注入（单条会漏分层规约）
            h = None
            if sk and SS.active_invariants(sk):
                h = "；".join(str(x.get("rule") or "")
                             for x in SS.active_invariants(sk))
            hints[d] = h
            # 模型用 add_field() 在运行时改结构，字面声明依旧同步——
            # ast_symbol_sync 看不到。必须检查函数体里**两处登记都写了**。
            assertions_by_domain[d] = ([{
                "kind": "regex_must_match", "file": "registry.py",
                "pattern": r"FIELDS\s*(\.\s*append|\+=|\.extend)",
                "message": "add_field 必须修改 FIELDS",
            }, {
                "kind": "regex_must_match", "file": "registry.py",
                "pattern": (r"REQUIRED\s*(\[[^]]+\]\s*=|\.\s*update|"
                            r"\.setdefault)"),
                "message": "add_field 必须同步修改 REQUIRED 必填表",
            }] if d == "coupling" else [])
            print("[hint:%s] %r" % (d, h), flush=True)
    finally:
        st.close()

    if a.skill_file and os.path.isfile(a.skill_file):
        with open(a.skill_file, encoding="utf-8") as f:
            hints = json.load(f).get("hints", hints)

    # ---- 阶段 2：留出集配对评测（No-Skill vs With-Skill，按域注入）----
    no_skill, with_skill = [], []
    for c in hold:
        # No-Skill = 没有该 Skill，也就没有它携带的写前断言（工具基线）
        r1 = run_case(c, prov, assertions=[], max_runtime_fix=0,
                      spec_hint=a.spec_hint)
        r2 = run_case(c, prov, hint=hints.get(c["domain"]),
                      spec_hint=a.spec_hint,
                      assertions=(assertions_by_domain.get(c["domain"])
                                  if c["domain"] == "coupling" else None),
                      max_runtime_fix=0)
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
           "protocol": ("按域各用 5 道训练题 0-shot 失败轨迹提炼 env_invariant，"
                        "40 道未见留出题配对评测；判分为真实执行的隐藏测试；"
                        "统计用 McNemar 精确检验"),
           "n_train": len(train), "n_holdout": len(hold),
           "train_pass_rate": round(
               sum(1 for r in train_rows if r.get("passed")) /
               max(1, len(train_rows)), 4),
           "skills": {d: ({"id": (skills.get(d) or {}).get("id"),
                           "env_invariants": (skills.get(d) or {}).get("env_invariants"),
                           "pre_checks": (skills.get(d) or {}).get("pre_checks")}
                          if skills.get(d) else None) for d in DOMAINS},
           "hints": hints,
           "post_write_assertions": assertions_by_domain,
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
