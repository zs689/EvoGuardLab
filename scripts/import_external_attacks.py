# -*- coding: utf-8 -*-
"""把公开的 Agent 攻击语料接进来（只下载 + 归一化 + 核验，不执行任何代码）。

为什么需要这一步：仓库此前的攻击样本全是自己写的 12 条，覆盖面和外部基准没有
可比性。这里接 InjecAgent（UIUC，MIT 许可）的端到端用例：每条都是
「正常用户请求 + 被投毒的工具返回」，正是本项目 `guard_tool_output` 的入口。

三条硬约束：
  1. **语料不入库**（`data/external/` 已忽略）：体积大、且许可是「引用」不是「转授」；
     仓库只保留下载器、清单（含逐文件 sha256）与评测报告，别人一条命令就能复现。
  2. **按 blob sha 取，不按分支取**：分支会动，sha 不会。同一份清单永远对应同一份字节。
  3. **只当数据读**：不 import、不执行、解包后只做字符串解析。

用法：
    python scripts/import_external_attacks.py            # 下载并归一化
    python scripts/import_external_attacks.py --offline  # 只用已有缓存（CI 用）
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "data", "external", "injecagent")
CACHE = os.path.join(ROOT, "_sandbox", "external_cache")

API = "https://api.github.com/repos/%s/git/blobs/%s"

# 只登记许可清楚、且是「端到端用例」而不是模板的语料。
SOURCES = [
    {
        "name": "injecagent",
        "repo": "uiuc-kang-lab/InjecAgent",
        "license": "MIT",
        "license_url": "https://github.com/uiuc-kang-lab/InjecAgent/blob/main/LICENSE",
        "homepage": "https://github.com/uiuc-kang-lab/InjecAgent",
        "paper": "InjecAgent: Benchmarking Indirect Prompt Injections in "
                 "Tool-Integrated Large Language Model Agents (ACL 2024 Findings)",
        "files": [
            {"path": "data/test_cases_dh_base.json",
             "git_sha": "976ad4e2108a553a3b885951e8eb119aa91f9445",
             "goal": "data_stealing", "variant": "base"},
            {"path": "data/test_cases_dh_enhanced.json",
             "git_sha": "803bd383695faaaeb2a03863aafc83d8b46a7e78",
             "goal": "data_stealing", "variant": "enhanced"},
            {"path": "data/test_cases_ds_base.json",
             "git_sha": "16f82d8a34349182eba883871333e569bafc6b93",
             "goal": "direct_harm", "variant": "base"},
            {"path": "data/test_cases_ds_enhanced.json",
             "git_sha": "f4580cb98e195d3a77ec92149294cc1bf37c0f5d",
             "goal": "direct_harm", "variant": "enhanced"},
        ],
    },
]

MANIFEST_VERSION = "external-attacks-v1"

# 对照组用的良性填充句：只替换被注入的那一句，别动其他字符。
#
# 为什么是一个**池子**而不是一句话：如果训练和评测都用同一句填充语，
# 模型只要学「出现这句 → 干净」就能拿高分，召回是假的。所以训练用一个子集，
# 评测用**没见过**的另一个子集（见 FILLER_TRAIN / FILLER_UNSEEN）。
BENIGN_FILLERS = [
    "Great product, works as described.",
    "Fast delivery and the item matched the photos.",
    "I have used it for a week and it still works fine.",
    "The packaging was intact and the manual is clear.",
    "Solid build quality for the price, would buy again.",
    "Battery life is acceptable for daily commuting.",
    "The colour is slightly darker than the listing photo.",
    "Customer service answered my question within a day.",
    "Works with my existing setup without extra adapters.",
    "A bit heavier than expected but very sturdy.",
    "The instructions could be clearer in section three.",
    "Arrived two days earlier than the estimate.",
    "Screen brightness is good even in a bright room.",
    "The cable is short, plan for an extension.",
    "Quiet operation, no noticeable vibration.",
    "Fits the case I already owned, no issues.",
    "Replacement part arrived and fitted perfectly.",
    "Simple to install, took about ten minutes.",
    "The warranty card was included in the box.",
    "Sound quality is fine for calls and music.",
    "Updated the firmware and it has been stable since.",
    "The stand is a little wobbly on uneven desks.",
    "Shipping box had a small dent, contents fine.",
    "Compatible with the older model as well.",
    "Setup wizard walked me through the steps.",
    "The lid closes with a satisfying click.",
    "No scratches after a month of daily use.",
    "Instructions mention a tool that is not included.",
    "The light is dimmer than the previous version.",
    "Pleasant experience overall, no complaints.",
    "Item was exactly the model number I ordered.",
    "Return process was straightforward when needed.",
    "Rubber feet keep it from sliding around.",
    "The app pairs quickly after the first setup.",
    "Colour matches the rest of my equipment.",
    "Came with a spare set of screws, handy.",
    "The screen protector went on without bubbles.",
    "Heavier base makes it stable while typing.",
    "Charging is slower than the spec suggests.",
    "Support sent a replacement cable free of charge.",
]

BENIGN_FILLER = BENIGN_FILLERS[0]


def filler_for(case_id, pool):
    """按 case_id 稳定地取一句填充语（可复现，且不同样本用不同句子）。"""
    h = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return pool[int(h[:8], 16) % len(pool)]


# 训练/验证用前 30 句，测试用**没见过**的后 10 句：这才测得出泛化。
FILLER_TRAIN = BENIGN_FILLERS[:30]
FILLER_UNSEEN = BENIGN_FILLERS[30:]


def minimal_pair(case, filler=BENIGN_FILLER):
    """构造最小对照：把**整段注入区间**换成一句无关评价，其余逐字不变。

    踩过的两个坑，都记在这里避免重犯：
      1. 不能拿 `Tool Response Template` 当负例 —— 模板与真实返回格式不同
         （返回多一层外层引号），字符级模型会去学格式差异当捷径。
      2. 不能只替换 `Attacker Instruction` —— enhanced 变体真正注入的是
         「IMPORTANT!!! Ignore all previous instructions …」一整段，
         attacker_instruction 只覆盖其中一句；只替换那一句，对照组自己
         仍会命中注入规则（实测 50% 误报，全是这么来的）。

    做法：模板 = 前缀 + 占位符 + 后缀。先剥掉响应最外层那对引号，
    用前缀/后缀把注入区间夹出来，中间整段替换。
    """
    tpl = case.get("tool_response_template") or ""
    text = case.get("tool_response") or ""
    marker = "<Attacker Instruction>"
    if marker in tpl and text:
        pre, post = tpl.split(marker, 1)
        quote, core = "", text
        if len(core) >= 2 and core[0] == '"' and core[-1] == '"':
            quote, core = '"', core[1:-1]
        if core.startswith(pre) and core.endswith(post):
            start = len(pre)
            end = len(core) - len(post)
            if 0 <= start <= end <= len(core):
                return (quote + core[:start] + filler + core[end:] + quote,
                        "minimal_pair")
    payload = case.get("attacker_instruction") or ""
    if payload and payload in text:
        return text.replace(payload, filler), "span_fallback"
    if tpl.strip():
        return tpl.replace(marker, filler), "template_fallback"
    return "", "none"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _cache_path(repo, sha):
    safe = repo.replace("/", "__")
    return os.path.join(CACHE, "%s__%s.bin" % (safe, sha))


def fetch_blob(repo, sha, timeout=60):
    """按 blob sha 取原始字节。先看缓存，缓存对就不用联网。"""
    path = _cache_path(repo, sha)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    url = API % (repo, sha)
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "EvoGuardLab/import-external-attacks"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise SystemExit(
            "拉取失败：%s/%s（%s）\n"
            "  离线环境请先在有网机器上跑一遍，或检查代理设置。"
            % (repo, sha, e))
    if payload.get("encoding") != "base64":
        raise SystemExit("意外的返回编码：%r" % payload.get("encoding"))
    data = base64.b64decode(payload["content"])
    expected = payload.get("size")
    if expected is not None and len(data) != expected:
        raise SystemExit("长度不符：期望 %s，实际 %d" % (expected, len(data)))
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    time.sleep(0.2)                      # 对匿名 API 客气一点
    return data


def _as_list(text):
    try:
        out = json.loads(text)
    except Exception:                    # noqa: BLE001
        return [text] if text else []
    return out if isinstance(out, list) else [out]


def normalize(records, file_meta, source):
    """把一条原始用例压成统一结构。字段缺失就留空，不猜。"""
    out = []
    for i, r in enumerate(records):
        tool_response = r.get("Tool Response") or ""
        if not tool_response.strip():
            continue
        case_id = "%s/%s/%s#%04d" % (source["name"], file_meta["goal"],
                                     file_meta["variant"], i)
        out.append({
            "case_id": case_id,
            "dataset": source["name"],
            "goal": file_meta["goal"],
            "variant": file_meta["variant"],
            "category": r.get("Attack Type") or "unspecified",
            "user_tool": r.get("User Tool") or "",
            "attacker_tools": _as_list(r.get("Attacker Tools")),
            "user_instruction": r.get("User Instruction") or "",
            "attacker_instruction": r.get("Attacker Instruction") or "",
            "expected_achievement": r.get("Expected Achievements") or "",
            "tool_response": tool_response,
            # 模板里注入位是占位符（<Attacker Instruction>），把它换成一句正常
            # 评价就得到「结构相同、没有被投毒」的对照样本 —— 误报率靠它来量。
            "tool_response_template": r.get("Tool Response Template") or "",
            "tool_parameters": r.get("Tool Parameters") or "",
            "modified": bool(r.get("Modifed")),
        })
    return out


def build(out_dir=DEFAULT_OUT, offline=False, sources=None, log=print):
    sources = sources if sources is not None else SOURCES
    all_cases, files_meta = [], []
    for src in sources:
        for fm in src["files"]:
            if offline:
                path = _cache_path(src["repo"], fm["git_sha"])
                if not os.path.isfile(path):
                    raise SystemExit("离线模式下缺缓存：%s（%s）"
                                     % (fm["path"], fm["git_sha"]))
                with open(path, "rb") as f:
                    raw = f.read()
            else:
                raw = fetch_blob(src["repo"], fm["git_sha"])
            try:
                records = json.loads(raw.decode("utf-8"))
            except Exception as e:       # noqa: BLE001
                raise SystemExit("%s 不是合法 JSON：%s" % (fm["path"], e))
            if not isinstance(records, list):
                raise SystemExit("%s 期望是数组，实际是 %s"
                                 % (fm["path"], type(records).__name__))
            cases = normalize(records, fm, src)
            all_cases.extend(cases)
            files_meta.append({
                "path": fm["path"], "git_sha": fm["git_sha"],
                "sha256": _sha256(raw), "bytes": len(raw),
                "records": len(records), "cases": len(cases),
                "goal": fm["goal"], "variant": fm["variant"],
            })
            log("  [√] %-34s %4d 条 → %4d 条用例（%.0f KB）"
                % (fm["path"], len(records), len(cases), len(raw) / 1024.0))

    # 同一个用例不许出现两次：case_id 必须唯一，否则统计会重复计数。
    ids = [c["case_id"] for c in all_cases]
    if len(ids) != len(set(ids)):
        raise SystemExit("case_id 有重复，归一化逻辑有问题")

    os.makedirs(out_dir, exist_ok=True)
    cases_path = os.path.join(out_dir, "cases.jsonl")
    with open(cases_path, "w", encoding="utf-8", newline="\n") as f:
        for c in all_cases:
            f.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    with open(cases_path, "rb") as f:
        cases_sha = _sha256(f.read())

    by_goal, by_cat = {}, {}
    for c in all_cases:
        by_goal[c["goal"]] = by_goal.get(c["goal"], 0) + 1
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1

    manifest = {
        "version": MANIFEST_VERSION,
        "note": "语料不入库：本文件只记录来源、许可、逐文件 sha256 与条数，"
                "跑 import_external_attacks.py 可复现同一份字节。",
        "sources": [{
            "name": s["name"], "repo": s["repo"], "license": s["license"],
            "license_url": s["license_url"], "homepage": s["homepage"],
            "paper": s.get("paper", ""),
            "ref": "git blob sha（不用分支名，分支会动）",
        } for s in sources],
        "files": files_meta,
        "cases_file": "cases.jsonl",
        "cases_sha256": cases_sha,
        "total_cases": len(all_cases),
        "by_goal": by_goal,
        "by_category": by_cat,
        "usage_limit": "只用于检测能力的对照评测；语料本身不随仓库分发，"
                       "引用请遵循上游许可与论文。",
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="接入外部 Agent 攻击语料")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--offline", action="store_true",
                    help="只用已下载缓存，不联网")
    args = ap.parse_args(argv)
    print("[1/2] 拉取并核验…")
    m = build(args.out, offline=args.offline)
    print("[2/2] 产物：%s" % os.path.join(args.out, "cases.jsonl"))
    print("  共 %d 条用例 ｜ 目标分布 %s ｜ 类别 %d 种"
          % (m["total_cases"], m["by_goal"], len(m["by_category"])))
    return m


if __name__ == "__main__":
    main()
