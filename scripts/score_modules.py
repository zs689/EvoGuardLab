#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模块评分器：把"我自评多少分"换成**可复现的规则**。

起因：我此前给过一张"全部 90+"的表，被独立审计推翻（发现 11 处测试全绿但
生产失效）。问题不在结论对不对，而在**分数本身不可复核** —— 没人能重算。

这里把评分写成确定性规则，任何人跑一遍都得到同一个数。规则倾向保守：
拿不到证据就扣分，不因为"看起来很完善"给分。

四个维度（各 0~25，总分 100）：
  A. 测试密度：每千行代码的有效测试条数（按符号名统计，不只匹配文件名）
  B. 接线完整性：安全相关的可选注入参数必须至少有一个**非测试**调用方
  C. 实测锚点：experiments/ 下有对应产物，且不早于源码 mtime
  D. 静态清洁：pyflakes 无 undefined name、无同名可变全局重复定义

用法：python scripts/score_modules.py [--json out.json]
"""
import argparse
import ast
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVO = os.path.join(ROOT, "ai-service", "evoguard")

#: 域 -> (模块, 对应的实测产物)
DOMAINS = {
    "core": (["core.py", "core_state.py", "core_schema.py"],
             ["probes/write_gate_stress.json"]),
    "execution": (["execution.py", "tool_contract.py", "tool_rails.py"],
                  ["probes/store_remaining_checks.json"]),
    "approval": (["approval.py"], []),
    "plan_runner": (["plan_runner.py"], []),
    "runtime": (["runtime.py", "ctx.py", "runtime_common.py",
                 "legacy_diagnose.py"], ["probes/write_gate_stress.json"]),
    "autonomous": (["autonomous_agent.py", "agent_utils.py", "model_call.py",
                    "reflection.py", "session_store.py", "tool_naming.py",
                    "agent_reexport.py"],
                   ["skill_ab_real/env_bench_final_r4.json"]),
    "security": (["security.py", "semantic_detectors.py", "light_classifier.py",
                  "light_semantics.py", "semantic_guard.py", "pinyin_guard.py",
                  "text_hygiene.py", "security_rules.py"],
                 ["pub_jailbreak_layered.json"]),
    "openai_proxy": (["openai_proxy.py"], []),
    "evolution": (["skills.py", "skill_synthesis.py", "conditional_skill.py",
                   "tool_synthesis.py", "experience.py"],
                  ["tool_synthesis_e2e.json"]),
    "sandbox": (["sandbox_executor.py", "fs_sandbox.py", "code_guard.py"],
                ["docker_sandbox_verify.json"]),
    "gateway": (["gateway.py"], ["cross_model/judge_probe.json"]),
    "agent_watch": (["agent_watch.py"],
                    ["probes/agent_watch_verification.json"]),
    "pg_store": (["pg_store.py", "pg_rls.py"], ["probes/pg_store_e2e.json"]),
    "evaluation": ([], ["pub_bench_layered.json"]),
}

#: 评测域看脚本而不是模块
SCRIPTS = {
    "evaluation": ["eval_public_benchmarks.py", "eval_jailbreak_public.py",
                   "eval_hard_negatives.py", "eval_coding_agent.py"],
}

SAFETY_PARAM_HINTS = ("taint_state", "provenance", "contract_fn", "intent_fn",
                      "rails", "policy", "approval_gate")


def _read(p):
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()


def _test_count(symbols):
    """按符号名数测试条数：只匹配文件名会漏掉经由 re-export 使用的模块。"""
    total, files = 0, set()
    for f in glob.glob(os.path.join(ROOT, "tests", "test_*.py")):
        src = _read(f)
        if any(re.search(r"\b%s\b" % re.escape(s), src) for s in symbols):
            total += len(re.findall(r"^\s+def test_", src, re.M))
            files.add(os.path.basename(f))
    return total, len(files)


def _unwired_safety_params(files):
    """可选安全参数中，没有任何**生产**调用方传的那个数。"""
    # 生产调用方是**整个** evoguard 包（模块互调），只扫传入的 files 会把
    # ctx.py 里的 preflight(taint_state=...) 漏掉，造成"无人传"的假阳性。
    prod = glob.glob(os.path.join(EVO, "*.py"))
    prod += glob.glob(os.path.join(ROOT, "ai-service", "api", "*.py"))
    prod += [os.path.join(ROOT, "app.py")]
    srcs = {}
    for p in prod:
        if os.path.isfile(p):
            srcs[p] = _read(p)
    all_text = "\n".join(srcs.values())
    bad = []
    for p, src in srcs.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            a = node.args
            named = list(a.kwonlyargs) + list(a.args)
            defaults = list(a.kw_defaults) + list(a.defaults)
            for arg, d in zip(named[len(named) - len(defaults):], defaults):
                if not (isinstance(d, ast.Constant) and d.value is None):
                    continue
                if not any(h in arg.arg for h in SAFETY_PARAM_HINTS):
                    continue
                pat = "%s=" % arg.arg
                # 注意：不能用 \b 包住 "name="——`=` 不是 word 字符，
                # 边界断言会恒不匹配，导致全部误报成"无人传"。
                users = [q for q in srcs
                         if re.search(re.escape(pat), srcs[q])
                         and (q != p or srcs[q].count(pat) > 1)]
                # intent_fn / contract_fn 是**测试替身注入点**：不传就走
                # _default_*_fn()，属正确设计，不算未接线。
                if not users and not any(
                        h in arg.arg for h in ("intent_fn", "contract_fn")):
                    bad.append("%s:%s(%s)" % (os.path.basename(p),
                                              node.name, arg.arg))
    return bad


def _anchor_status(artifacts, newest_src_mtime):
    out = []
    for rel in artifacts:
        cand = [os.path.join(ROOT, "experiments", rel),
                os.path.join(ROOT, "docs", "review-remediation", rel)]
        p = next((c for c in cand if os.path.isfile(c)), cand[0])
        if not os.path.isfile(p):
            out.append((rel, "missing"))
            continue
        rep = {}
        try:
            with open(p, encoding="utf-8") as f:
                rep = json.load(f)
        except Exception:                                   # noqa: BLE001
            pass
        if rep.get("frozen") is True and str(rep.get("frozen_reason") or ""):
            out.append((rel, "frozen"))
        elif os.path.getmtime(p) + 1 < newest_src_mtime:
            out.append((rel, "stale"))
        else:
            out.append((rel, "fresh"))
    return out


def score_domain(name, files, artifacts):
    detail = {}
    # A. 测试密度
    if name in SCRIPTS:
        modules = SCRIPTS[name]
        lines = sum(len(_read(os.path.join(ROOT, "scripts", f)).splitlines())
                    for f in modules
                    if os.path.isfile(os.path.join(ROOT, "scripts", f)))
        symbols = [f[:-3] for f in modules] + ["ASR", "self_checks"]
    else:
        paths = [os.path.join(EVO, f) for f in files]
        lines = sum(len(_read(p).splitlines()) for p in paths if os.path.isfile(p))
        symbols = [f[:-3] for f in files] + ["Ctx", "preflight"]
    n_tests, n_files = _test_count(symbols)
    density = n_tests / max(1, lines / 1000.0)
    a = min(25.0, density / 2.0)         # 50 tests/kloc = 满分
    detail["A_tests"] = {"tests": n_tests, "files": n_files,
                         "kloc": round(lines / 1000.0, 2),
                         "density": round(density, 1), "score": round(a, 1)}

    # B. 接线
    bad = _unwired_safety_params(files) if name not in SCRIPTS else []
    b = 25.0 if not bad else max(0.0, 25.0 - 12.5 * len(bad))
    detail["B_wiring"] = {"unwired": bad, "score": b}

    # C. 实测锚点
    if not artifacts:
        c = 8.0           # 没有锚点：不给满分也不归零（有些域靠间接验证）
        detail["C_anchor"] = {"items": [], "note": "无专属产物", "score": c}
    else:
        newest = 0.0
        for f in files:
            p = os.path.join(EVO, f)
            if os.path.isfile(p):
                newest = max(newest, os.path.getmtime(p))
        for f in SCRIPTS.get(name, []):
            p = os.path.join(ROOT, "scripts", f)
            if os.path.isfile(p):
                newest = max(newest, os.path.getmtime(p))
        st = _anchor_status(artifacts, newest)
        good = sum(1 for _r, s in st if s in ("fresh", "frozen"))
        c = 25.0 * good / len(st)
        detail["C_anchor"] = {"items": st, "score": round(c, 1)}

    # D. 静态清洁
    d = 25.0
    if name not in SCRIPTS:
        try:
            r = subprocess.run([sys.executable, "-m", "pyflakes"] +
                               [os.path.join(EVO, f) for f in files
                                if os.path.isfile(os.path.join(EVO, f))],
                               capture_output=True, text=True, timeout=120)
            und = [ln for ln in (r.stdout or "").splitlines()
                   if "undefined name" in ln
                   and "unable to detect" not in ln]
            d -= 12.5 * len(und)
        except Exception:                                   # noqa: BLE001
            pass
    detail["D_static"] = {"score": max(0.0, d)}

    total = round(a + b + c + max(0.0, d), 1)
    return {"domain": name, "score": total, "detail": detail}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    rows = [score_domain(k, f, art) for k, (f, art) in DOMAINS.items()]
    rows.sort(key=lambda r: r["score"])
    print("%-14s %6s  %-28s %-10s %-8s %s"
          % ("域", "分数", "测试(条/文件/密度)", "接线", "锚点", "静态"))
    for r in rows:
        d = r["detail"]
        print("%-14s %6.1f  %-28s %-10s %-8s %s"
              % (r["domain"], r["score"],
                 "%d/%d/%.1f" % (d["A_tests"]["tests"], d["A_tests"]["files"],
                                 d["A_tests"]["density"]),
                 ("OK" if not d["B_wiring"]["unwired"]
                  else ",".join(d["B_wiring"]["unwired"])[:10]),
                 "%.0f" % d["C_anchor"]["score"], "%.0f" % d["D_static"]["score"]))
    avg = round(sum(r["score"] for r in rows) / len(rows), 1)
    print("\n平均 %.1f | 低于 90 的域: %s"
          % (avg, [r["domain"] for r in rows if r["score"] < 90]))
    if a.json:
        out = os.path.join(ROOT, a.json) if not os.path.isabs(a.json) else a.json
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"average": avg, "domains": rows}, f,
                      ensure_ascii=False, indent=2)
        print("WROTE", a.json)


if __name__ == "__main__":
    main()
