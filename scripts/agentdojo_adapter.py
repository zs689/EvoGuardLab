# -*- coding: utf-8 -*-
"""AgentDojo 适配器：从攻击模板源码生成样本，并做跨数据集检验。

为什么需要适配器：AgentDojo 的攻击不是现成的数据集，而是
`src/agentdojo/attacks/*.py` 里的 Python 字符串常量（`FixedJailbreakAttack`
的模板、`_JB_STRING` / `_DOS_STRING` 类属性）。要样本就得解析源码。

三条硬约束：
  1. **只按 git blob sha 取**（分支会动、sha 不会），逐文件记录 sha256；
  2. **只用 AST 静态解析**字符串常量 —— 不 import、不执行、不 pip install；
  3. 生成的样本是 **「AgentDojo 模板 × InjecAgent 攻击目标」的组合样本**，
     不是 AgentDojo 原始端到端任务（那些任务要跑它自己的 4 个环境）。
     报告里必须这么写，不能冒充官方基准成绩。

用法：
    python scripts/agentdojo_adapter.py                 # 下载 + 生成 + 评测
    python scripts/agentdojo_adapter.py --offline
"""
import argparse
import ast
import base64
import hashlib
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import security as S  # noqa: E402

CACHE = os.path.join(ROOT, "_sandbox", "agentdojo")
DEFAULT_OUT = os.path.join(ROOT, "experiments", "agentdojo_eval_v1")
CORPUS_OUT = os.path.join(ROOT, "data", "external", "agentdojo")
INJ_CASES = os.path.join(ROOT, "data", "external", "injecagent", "cases.jsonl")

REPO = "ethz-spylab/agentdojo"
LICENSE = "MIT"
LICENSE_URL = "https://github.com/ethz-spylab/agentdojo/blob/main/LICENSE"
API = "https://api.github.com/repos/%s/git/blobs/%s" % (REPO, "%s")

FILES = [
    {"path": "src/agentdojo/attacks/base_attacks.py",
     "git_sha": "2a2f641c"},
    {"path": "src/agentdojo/attacks/baseline_attacks.py",
     "git_sha": "1fed06e8"},
    {"path": "src/agentdojo/attacks/dos_attacks.py",
     "git_sha": "9f4f4eaf"},
    {"path": "src/agentdojo/attacks/important_instructions_attacks.py",
     "git_sha": "13122863"},
]

# 内容不雅、且与检测能力无关，解析时跳过并在报告里写明。
SKIP_CLASSES = {"SwearwordsDoSAttack", "ManualAttack"}

INJECTION_CODES = {d["code"] for d in S.DETECTORS
                   if d["code"].startswith("injection_")}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _cache_path(fm):
    return os.path.join(CACHE, fm["git_sha"] + ".py")


def fetch(fm, offline=False, timeout=40):
    path = _cache_path(fm)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read(), "cache"
    if offline:
        raise SystemExit("离线模式缺缓存：%s" % fm["path"])
    # 短 sha 需要先从 contents API 补全
    d = os.path.dirname(fm["path"])
    listing = json.load(urllib.request.urlopen(
        "https://api.github.com/repos/%s/contents/%s" % (REPO, d),
        timeout=timeout))
    name = os.path.basename(fm["path"])
    full = next(x["sha"] for x in listing if x["name"] == name)
    blob = json.load(urllib.request.urlopen(API % full, timeout=timeout))
    raw = base64.b64decode(blob["content"])
    if blob.get("size") != len(raw):
        raise SystemExit("长度不符：%s" % fm["path"])
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    return raw, full


def _string(node):
    """只取字符串常量；隐式拼接会被 AST 合成一个 Constant。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):          # f-string：不解析，避免猜
        return None
    return None


def extract_templates(source, source_name):
    """AST 静态提取 {class_name, name, template}。**不执行任何代码**。"""
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name in SKIP_CLASSES:
            continue
        attack_name, template = None, None
        for item in node.body:
            if isinstance(item, ast.Assign):
                for t in item.targets:
                    if isinstance(t, ast.Name) and t.id == "name":
                        attack_name = _string(item.value)
                    elif isinstance(t, ast.Name) and t.id in (
                            "_JB_STRING", "_DOS_STRING"):
                        template = _string(item.value) or template
        if template is None:
            # FixedJailbreakAttack 子类把模板写在 super().__init__(...) 第一参
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                if (isinstance(fn, ast.Attribute) and fn.attr == "__init__"
                        and sub.args):
                    cand = _string(sub.args[0])
                    if cand and ("{goal}" in cand or len(cand) > 20):
                        template = cand
                        break
        if template and attack_name:
            out.append({"class_name": node.name, "name": attack_name,
                        "template": template, "source_file": source_name})
    return out


def fill(template, goal, user="Emma Johnson", model="GPT-4"):
    """只替换登记过的占位符，其余花括号原样保留。"""
    return (template.replace("{goal}", goal)
                    .replace("{user}", user)
                    .replace("{model}", model))


def load_goals(limit=12):
    """攻击目标取自 InjecAgent 的 attacker_instruction（去重、取前 limit 条）。"""
    if not os.path.isfile(INJ_CASES):
        raise SystemExit("先跑 import_external_attacks.py 生成 InjecAgent 语料")
    seen, goals = set(), []
    with open(INJ_CASES, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            g = (c.get("attacker_instruction") or "").strip()
            if g and g not in seen:
                seen.add(g)
                goals.append(g)
            if len(goals) >= limit:
                break
    return goals


def build_cases(offline=False):
    files_meta, templates = [], []
    for fm in FILES:
        raw, full = fetch(fm, offline)
        src = raw.decode("utf-8")
        found = extract_templates(src, os.path.basename(fm["path"]))
        templates.extend(found)
        files_meta.append({"path": fm["path"], "git_sha": full,
                           "sha256": _sha256(raw), "bytes": len(raw),
                           "templates": len(found)})
    goals = load_goals()
    cases = []
    for t in templates:
        for i, g in enumerate(goals):
            cases.append({
                "case_id": "agentdojo/%s#%02d" % (t["name"], i),
                "dataset": "agentdojo_templates", "kind": "attack",
                "template_name": t["name"], "class_name": t["class_name"],
                "source_file": t["source_file"], "goal_id": i,
                "text": fill(t["template"], g), "goal": g,
            })
    return cases, templates, files_meta, goals


def evaluate(cases, model=None):
    rows = []
    for c in cases:
        g = S.guard_tool_output("kb.search", c["text"])
        codes = {f["code"] for f in g["findings"]}
        lvl1 = bool(codes & INJECTION_CODES)
        lvl2 = None
        if model is not None:
            import deep_scan
            lvl2 = deep_scan.classify(model, c["text"])["is_injection"]
        rows.append({
            "case_id": c["case_id"], "template_name": c["template_name"],
            "lvl1_injection": lvl1,
            "lvl2_injection": lvl2,
            "codes": sorted(codes),
        })
    return rows


def negative_controls():
    """一组**正常文本**，用来给二级模型的召回配一个误报数。

    只报召回会自欺：这个模型在 16/16 条正常用户请求上都判注入
    （见 docs/detector-evolution.md），所以对攻击拿 1.0 并不稀奇。
    """
    texts = []
    if os.path.isfile(INJ_CASES):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import import_external_attacks as IM
        seen = set()
        with open(INJ_CASES, encoding="utf-8") as f:
            for line in f:
                c = json.loads(line)
                ctrl, kind = IM.minimal_pair(
                    c, IM.filler_for(c["case_id"], IM.FILLER_UNSEEN))
                if kind == "minimal_pair" and ctrl not in seen:
                    seen.add(ctrl)
                    texts.append({"source": "clean_tool_output", "text": ctrl})
                u = (c.get("user_instruction") or "").strip()
                if u and u not in seen:
                    seen.add(u)
                    texts.append({"source": "benign_user_request", "text": u})
                if len(texts) >= 36:
                    break
    return texts[:36]


def summarize(cases, rows, neg_rows=None):
    by = {}
    for c in cases:
        by.setdefault(c["template_name"], []).append(c)
    out = {}
    for name, cs in sorted(by.items()):
        rs = [r for r in rows if r["template_name"] == name]
        n = len(rs)
        out[name] = {
            "cases": n,
            "lvl1_recall": round(sum(r["lvl1_injection"] for r in rs) / float(n), 3),
            "lvl2_recall": (round(sum(bool(r["lvl2_injection"]) for r in rs)
                                  / float(n), 3)
                            if all(r["lvl2_injection"] is not None for r in rs)
                            else None),
            "combined_recall": round(
                sum(r["lvl1_injection"] or bool(r["lvl2_injection"]) for r in rs)
                / float(n), 3),
            "codes": sorted({x for r in rs for x in r["codes"]}),
        }
    neg = {"cases": 0, "lvl1_fp": None, "lvl2_fp": None}
    if neg_rows is not None:
        neg = {
            "cases": len(neg_rows),
            "lvl1_fp": round(sum(r["lvl1_injection"] for r in neg_rows)
                             / float(len(neg_rows)), 3) if neg_rows else None,
            "lvl2_fp": round(sum(bool(r["lvl2_injection"]) for r in neg_rows)
                             / float(len(neg_rows)), 3) if neg_rows else None,
            "by_source": {
                src: {"cases": len(sub),
                      "lvl2_fp": round(sum(bool(r["lvl2_injection"]) for r in sub)
                                       / float(len(sub)), 3)}
                for src in sorted({r["source"] for r in neg_rows})
                for sub in [[x for x in neg_rows if x["source"] == src]]
            } if neg_rows else {},
        }
    return {
        "templates": len(out), "cases": len(rows), "by_template": out,
        # 空样本不许崩，也不许编一个 0.0 当结论
        "overall_lvl1": (round(sum(r["lvl1_injection"] for r in rows)
                               / float(len(rows)), 3) if rows else None),
        "overall_combined": (round(
            sum(r["lvl1_injection"] or bool(r["lvl2_injection"]) for r in rows)
            / float(len(rows)), 3) if rows else None),
        "negative_controls": neg,
    }


def _markdown(data):
    s = data["summary"]
    L = ["# AgentDojo 攻击模板：覆盖矩阵 + 跨数据集检验", "",
         "> 语料：`ethz-spylab/agentdojo`（**MIT**），按 git blob sha 取 4 个源码文件，"
         "AST 静态解析字符串常量，不 import、不执行。",
         "> 生成 %d 条**组合样本**：%d 个模板 × %d 个攻击目标（目标取自 InjecAgent）。"
         % (s["cases"], s["templates"], data["goals"]),
         "> **这不是 AgentDojo 官方端到端成绩**：那些任务要跑它自己的 4 个环境与工具集，"
         "这里只测「同样一句攻击，换一种包装，检测件还认不认得」。", "",
         "## 每个模板被认出多少", "",
         "| 模板 | 样本 | 一级正则 | 二级模型 | 合并 | 命中信号 |",
         "|---|---|---|---|---|---|"]
    for k, v in s["by_template"].items():
        L.append("| `%s` | %d | %s | %s | %s | %s |"
                 % (k, v["cases"], v["lvl1_recall"],
                    v["lvl2_recall"] if v["lvl2_recall"] is not None else "N/A",
                    v["combined_recall"],
                    ", ".join("`%s`" % x for x in v["codes"][:4]) or "—"))
    L += ["", "整体：一级 %s ｜ 合并 %s" % (s["overall_lvl1"],
                                          s["overall_combined"]), "",
          "## 正常文本上的误报（和召回必须一起看）", "",
          "| 对照 | 条数 | 一级误报 | 二级误报 |", "|---|---|---|---|",
          "| 合计 | %d | %s | %s |" % (s["negative_controls"]["cases"],
                                      s["negative_controls"]["lvl1_fp"],
                                      s["negative_controls"]["lvl2_fp"])]
    for src, v in s["negative_controls"].get("by_source", {}).items():
        L.append("| `%s` | %d | — | %s |" % (src, v["cases"], v["lvl2_fp"]))
    L += ["",
          "**二级模型在攻击模板上拿 1.0 召回，不说明它跨语料泛化好**："
          "它在正常文本上同样大面积误报。它学的是「这段文本像不像指令」，"
          "不是「是不是攻击者塞的」。召回 1.0 与误报并列才有意义。", "",
          "## 跨数据集检验怎么读", "",
          "- 二级模型只在 **InjecAgent** 上训练，从未见过 AgentDojo 的模板；"
          "这里的命中率是真正的跨语料泛化，不是同一批数据的另一份切分。",
          "- 一级正则只认字面注入话术，`important_instructions` / `direct` / `dos` "
          "这类没有关键词的模板会漏 —— 和 InjecAgent base 变体的 0 召回是同一个问题。",
          "- 组合样本不是独立攻击向量：同一个模板换 goal 不算新的攻击手法，"
          "不能拿去凑「30 个独立向量」。", "",
          "## 边界", "",
          "- 模板里的 `{goal}` 用 InjecAgent 的攻击句填充，因此是**两套公开语料的组合**；"
          "结论只适用于「措辞包装」这一维。",
          "- `SwearwordsDoSAttack` 内容不雅且与检测能力无关，解析时跳过；"
          "`ManualAttack` 需要人工输入，也跳过。",
          "- 语料与样本都不入库（`data/external/` 已忽略），清单记录逐文件 sha256。", ""]
    return "\n".join(L)


def run(out=DEFAULT_OUT, corpus_out=CORPUS_OUT, offline=False):
    cases, templates, files_meta, goals = build_cases(offline)
    model_path = os.path.join(ROOT, "experiments", "detector_evolution_v1",
                              "model.json")
    model = None
    if os.path.isfile(model_path):
        with open(model_path, encoding="utf-8") as f:
            model = json.load(f)
    rows = evaluate(cases, model)
    neg_rows = evaluate(
        [{"case_id": "neg#%03d" % i, "template_name": "negative_control",
          "text": n["text"]} for i, n in enumerate(negative_controls())],
        model) if model is not None else None
    if neg_rows:
        for r, n in zip(neg_rows, negative_controls()):
            r["source"] = n["source"]
    summary = summarize(cases, rows, neg_rows)

    os.makedirs(corpus_out, exist_ok=True)
    with open(os.path.join(corpus_out, "cases.jsonl"), "w", encoding="utf-8",
              newline="\n") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "version": "agentdojo-templates-v1",
        "repo": REPO, "license": LICENSE, "license_url": LICENSE_URL,
        "files": files_meta, "templates": len(templates),
        "skipped_classes": sorted(SKIP_CLASSES),
        "goal_source": "InjecAgent attacker_instruction (12 distinct)",
        "cases": len(cases),
        "note": "组合样本，不是 AgentDojo 端到端任务；只做 AST 静态解析，不执行源码。",
    }
    with open(os.path.join(corpus_out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")

    data = {"version": 1, "summary": summary, "goals": len(goals),
            "rows": rows, "model_used": model is not None,
            "model_hash": (None if model is None else
                           __import__("deep_scan").model_hash(model)),
            "manifest": manifest}
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="AgentDojo 模板适配器 + 覆盖评测")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--corpus-out", default=CORPUS_OUT)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args(argv)
    d = run(args.out, args.corpus_out, args.offline)
    s = d["summary"]
    print("[√] 产物：%s" % os.path.join(args.out, "report.md"))
    print("    %d 个模板 × %d 个目标 = %d 条 ｜ 一级 %s ｜ 合并 %s"
          % (s["templates"], d["goals"], s["cases"], s["overall_lvl1"],
             s["overall_combined"]))
    return d


if __name__ == "__main__":
    main()
