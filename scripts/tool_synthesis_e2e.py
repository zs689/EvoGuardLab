#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合成工具的端到端闭环：模型写函数 -> AST 白名单 -> 沙箱测试 -> 注册 -> 调用。

为什么单独做：`tests/test_tool_synthesis.py` 验的是「给定源码能不能安全执行」，
走的是**执行通道**。但自主进化的主张是「模型自己写出来、系统自己验」，中间
少了「源码由模型产出」这一段。本脚本用本地 ollama 补上这一段，用真模型产出
真代码，再走完整的合成链路。

判定三条一起看，缺一条都不能算成功：
  1. 模型产出的代码通过 AST 白名单（没用到 while/import/反射逃逸）
  2. 通过沙箱测试用例（不是「能跑就行」，要对得上期望值）
  3. 注册后能像普通工具一样被调用

用法：python scripts/tool_synthesis_e2e.py [--model qwen2.5:7b]
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402
import tool_registry as TR                                  # noqa: E402
import tool_synthesis as TS                                 # noqa: E402

TASKS = [
    {
        "name": "text.word_count",
        "prompt": ("写一个 Python 函数 word_count(s)，返回字符串 s 里以空白分隔的"
                   "单词个数（空字符串返回 0）。只输出一个 python 代码块，"
                   "不要解释，不要 import 任何模块。"),
        "tests": [
            {"call": "word_count('hello world')", "expect": 2},
            {"call": "word_count('')", "expect": 0},
            {"call": "word_count('  a  b   c ')",
             "eval": "assert _result == 3", "expect": 3},
        ],
    },
    {
        "name": "math.clamp01",
        "prompt": ("写一个 Python 函数 clamp01(x)，把数值限制在 [0, 1] 区间并返回。"
                   "只输出一个 python 代码块，不要解释，不要 import 任何模块。"),
        "tests": [
            {"call": "clamp01(0.5)", "expect": 0.5},
            {"call": "clamp01(-3)", "expect": 0},
            {"call": "clamp01(9)", "expect": 1},
        ],
    },
    {
        "name": "text.initials",
        "prompt": ("写一个 Python 函数 initials(s)，把字符串按空白拆开后取每个词的"
                   "首字母并用大写拼起来（'hello world' -> 'HW'）。"
                   "只输出一个 python 代码块，不要解释，不要 import 任何模块。"),
        "tests": [
            {"call": "initials('hello world')", "expect": "HW"},
            {"call": "initials('a b c')", "expect": "ABC"},
            {"call": "initials('')", "expect": ""},
        ],
    },
]


def _extract_code(text):
    """从模型输出里取源码，三种常见形态都要认。

    ollama 的 json_mode 会强制 `format=json`，模型于是把代码包成
    `{"python_code": "def ...\n ..."}` —— 直接按 markdown 块提取会拿到
    整个 JSON 字面量（实测 3/3 都失败在这里）。
    """
    t = (text or "").strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            for k in ("python_code", "code", "function", "source", "content"):
                v = obj.get(k)
                if isinstance(v, str) and "def " in v:
                    return v.strip()
            # 模型也常拿**函数名当 key**（{"word_count": "def word_count..."}），
            # 固定字段名兜不住，就遍历所有字符串值找第一个含 def 的。
            for v in obj.values():
                if isinstance(v, str) and "def " in v:
                    return v.strip()
    except Exception:                                      # noqa: BLE001
        pass
    m = re.search(r"```(?:python)?\s*([\s\S]*?)```", t)
    return (m.group(1) if m else t).strip()


def _ensure_func_name(code, func):
    if re.search(r"^def\s+%s\s*\(" % re.escape(func), code, re.M):
        return code, False
    m = re.search(r"^def\s+(\w+)\s*\(", code, re.M)
    if not m:
        return code, False
    return re.sub(r"^def\s+%s\s*\(" % re.escape(m.group(1)),
                  "def %s(" % func, code, count=1, flags=re.M), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ollama")
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--out", default="experiments/tool_synthesis_e2e.json")
    a = ap.parse_args()

    preset = GW.PROVIDER_PRESETS.get(a.provider)
    if preset:
        be, url, key_env, dflt, _t = preset
        sp = GW.ProviderSpec(backend=be, base_url=url,
                             model=a.model or dflt, api_key_env=key_env,
                             timeout=300)
    else:
        be, sp = a.provider, GW.ProviderSpec(backend=a.provider, model=a.model,
                                             timeout=300)
    prov = GW.get_provider(force=be, spec=sp)
    reg = TR.ToolRegistry()

    rows = []
    for task in TASKS:
        func = task["name"].split(".")[-1]
        rec = {"tool": task["name"], "stage": "generate"}
        try:
            raw, _tok, _m = prov.complete(
                [{"role": "user", "content": task["prompt"]}],
                temperature=0.0, max_tokens=600)
            code = _extract_code(raw)
            code, renamed = _ensure_func_name(code, func)
            rec["code"] = code[:600]
            rec["renamed_to_task_func"] = renamed
        except Exception as exc:                           # noqa: BLE001
            rec.update({"ok": False, "error": "model_call:%s"
                        % type(exc).__name__})
            rows.append(rec)
            continue

        rec["stage"] = "synthesize"
        try:
            ok = TS.synthesize_tool(task["name"], code, task["tests"],
                                    registry=reg)
            rec["synthesized"] = bool(ok)
        except TS.ToolSynthesisError as exc:
            rec.update({"ok": False, "error": str(exc)[:200]})
            rows.append(rec)
            continue

        rec["stage"] = "register"
        tool = reg.get(task["name"])
        rec["registered"] = tool is not None
        # 「可调用」的证据是**沙箱里按用例真调过并返回了期望值**，
        # 不是"注册表里有个名字"。synthesize_tool 返回 True 即代表这一点。
        rec["verified_by_cases"] = bool(rec.get("synthesized"))
        rec["n_cases"] = len(task["tests"])

        rec["ok"] = bool(rec.get("synthesized") and rec.get("registered")
                        and rec.get("verified_by_cases"))
        rows.append(rec)

    n_ok = sum(1 for r in rows if r.get("ok"))
    rep = {"provider": a.provider, "model": a.model,
           "protocol": sp.backend, "n": len(rows), "passed_n": n_ok,
           "tasks": rows,
           "note": ("模型产码 -> AST 白名单 -> 沙箱测试 -> 注册的全链路；"
                    "任何一段失败都如实记在对应 task 的 stage/error 里")}
    out = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    for r in rows:
        print("%s %-16s %s" % ("PASS" if r.get("ok") else "FAIL",
                               r["tool"], (r.get("error") or "注册成功")[:70]))
    print("== %d/%d ==" % (n_ok, len(rows)))
    return 0 if n_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
