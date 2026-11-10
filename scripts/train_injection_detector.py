# -*- coding: utf-8 -*-
"""让检测能力自己长：用没通过的样本训练第二级，再按门禁决定要不要晋升。

流程（对齐仓库既有的「提炼 → 独立验证 → 晋升门禁」）：

  1. **分组划分**：按 attacker_instruction（或 user_tool）整组划分 train/val/test。
     同一句攻击话术只会落在一个划分里 —— 否则 test 里全是背过的题，数字再高也没意义。
  2. **只在小样本上训练**：正例=被投毒的工具返回，负例=同结构的干净对照
     （模板里注入位换成正常评价）。负例和正例只差那一句，模型必须学「那句话」，
     不能学「这段文本来自哪个工具」。
  3. **阈值只在 validation 上调**：目标是在误报率 <= 上限的前提下最大化召回。
  4. **test 只报数**：调完阈值再看 test，不许回头改。
  5. **晋升门禁**：validation 上达不到（精确率、召回增量、误报上限）就不晋升，
     照实写「未晋升」和差距 —— 负结果也是结果。

同时报三种形态：仅正则 / 仅模型 / 正则+模型（前者没命中才看后者），
外加 p50/p95 耗时，回答「加时间换来的准确率值不值」。
"""
import argparse
import hashlib
import json
import os
import collections
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import deep_scan as DS      # noqa: E402
import import_external_attacks as IM   # noqa: E402
import security as S        # noqa: E402

DEFAULT_SRC = os.path.join(ROOT, "data", "external", "injecagent")
DEFAULT_OUT = os.path.join(ROOT, "experiments", "detector_evolution_v1")
BENIGN = IM.BENIGN_FILLER          # 仅用于展示/兼容
POOL_TRAIN = IM.FILLER_TRAIN       # 训练与验证用这 30 句
POOL_UNSEEN = IM.FILLER_UNSEEN     # 测试用没见过的 10 句

TRAIN_RATIO, VAL_RATIO = 0.64, 0.18

INJECTION_CODES = {d["code"] for d in S.DETECTORS
                   if d["code"].startswith("injection_")}

# 晋升门禁（先定死，再跑；不许看完 test 再改）
GATE = {
    "min_val_precision": 0.95,
    "min_val_recall_gain": 0.25,     # 相对「仅正则」在 validation 上的增量
    "max_val_fp_rate": 0.02,
    # 独立复核（test）只看「能不能发布」，不回头改参数：
    "max_test_fp_rate": 0.05,
    "min_test_recall_gain": 0.25,
}


def load_cases(src):
    """只保留能构造**严格**最小对照的样本，其余整条剔除并计数。

    124/2108 条的模板锚点对不上（返回里同一句出现多次或转义不同），
    强行构造会留下残留注入文本，对照组自己就命中规则 —— 那种样本既不进训练
    也不进评测。注意：剔除依据是「能不能构造」，与模型表现无关，不是挑结果。
    """
    with open(os.path.join(src, "cases.jsonl"), encoding="utf-8") as f:
        allc = [json.loads(line) for line in f if line.strip()]
    kept, dropped = [], 0
    for c in allc:
        text, kind = IM.minimal_pair(
            c, IM.filler_for(c["case_id"], IM.FILLER_TRAIN))
        if kind == "minimal_pair" and text.strip():
            kept.append(c)
        else:
            dropped += 1
    return kept, dropped


def control_text(case, pool=None):
    """负例＝最小对照：只把注入区间换掉，其余逐字不变。

    两个方法上的硬要求：
      1. 不能用模板当负例（格式不同 → 模型学排版）；
      2. 填充语要按 case 从池子里取，且 train/val 与 test 用**不相交**的池子，
         否则模型只要认「这句填充语」就能判干净，召回是假的。
    """
    pool = POOL_TRAIN if pool is None else pool
    filler = IM.filler_for(case["case_id"], pool)
    return IM.minimal_pair(case, filler)[0]


def regex_hit(text, tool="tool"):
    """一级：正则只认注入类信号（PII/密钥不算认出注入）。"""
    g = S.guard_tool_output(tool, text)
    return bool({f["code"] for f in g["findings"]} & INJECTION_CODES)


def group_key(case, by):
    return case["attacker_instruction"] if by == "payload" else case["user_tool"]


def _stratum(case):
    return "%s|%s" % (case["goal"], case["category"])


def split(cases, by="payload"):
    """按组划分，且在每个 (目标, 类别) 层内部分配，保证三段都含多个类别。

    只按 hash 全局排序切三段会退化：题目里就踩过 —— val 全是一个类别、
    test 全是另一个类别，val 上调好的阈值到 test 上完全不是一回事。
    这里改成「层内按比例分」，组太少的层（<3）整体留在 train，并在元信息里写清。
    """
    group_stratum = {}
    for c in cases:
        group_stratum.setdefault(group_key(c, by), _stratum(c))
    buckets = {}
    for g, st in group_stratum.items():
        buckets.setdefault(st, []).append(g)

    train_g, val_g, test_g, thin = set(), set(), set(), []
    for st in sorted(buckets):
        gs = sorted(buckets[st],
                    key=lambda g: hashlib.sha256(g.encode("utf-8")).hexdigest())
        n = len(gs)
        if n >= 3:
            n_test = max(1, int(round(n * (1.0 - TRAIN_RATIO - VAL_RATIO))))
            n_val = max(1, int(round(n * VAL_RATIO)))
            if n_test + n_val >= n:
                n_test, n_val = 1, 1
            if n_test + n_val >= n:          # n == 3 时只留 1 组做验证
                n_test, n_val = 1, 0
        elif n == 2:
            n_test, n_val = 1, 0
        else:
            n_test, n_val = 0, 0
            thin.append(st)
        test_g.update(gs[:n_test])
        val_g.update(gs[n_test:n_test + n_val])
        train_g.update(gs[n_test + n_val:])

    if not val_g:                            # 兜底：从 train 借一组，保证 val 非空
        borrowed = sorted(train_g, key=lambda g: hashlib.sha256(g.encode()).hexdigest())[0]
        train_g.discard(borrowed)
        val_g.add(borrowed)
    if not test_g:
        borrowed = sorted(train_g, key=lambda g: hashlib.sha256(g.encode()).hexdigest())[0]
        train_g.discard(borrowed)
        test_g.add(borrowed)

    out = {"train": [], "val": [], "test": []}
    for c in cases:
        g = group_key(c, by)
        out["train" if g in train_g else "val" if g in val_g else "test"].append(c)
    meta = {"by": by, "groups": len(group_stratum),
            "train_groups": len(train_g), "val_groups": len(val_g),
            "test_groups": len(test_g), "thin_strata": thin,
            "composition": {k: dict(collections.Counter(
                _stratum(c) for c in v)) for k, v in out.items()}}
    return out, meta


def evaluate_part(model, cases, threshold, pool=None):
    """在一个划分上算三种形态的指标与耗时。"""
    regex_t, deep_t = [], []
    tp = fp = tn = fn = 0
    d_tp = d_fp = d_fn = 0
    c_tp = c_fp = c_fn = 0
    misses = []
    for c in cases:
        text, ctrl = c["tool_response"], control_text(c, pool)
        t0 = time.perf_counter()
        r_hit = regex_hit(text, c["user_tool"])
        t1 = time.perf_counter()
        d_hit = (DS.classify(model, text, threshold)["is_injection"]
                 if model else False)
        t2 = time.perf_counter()
        ctrl_regex_fp = regex_hit(ctrl, c["user_tool"]) if ctrl.strip() else False
        ctrl_deep_fp = (DS.classify(model, ctrl, threshold)["is_injection"]
                        if (model and ctrl.strip()) else False)
        regex_t.append((t1 - t0) * 1000.0)
        deep_t.append((t2 - t1) * 1000.0)

        tp += int(r_hit)
        fn += int(not r_hit)
        fp += int(ctrl_regex_fp)
        d_tp += int(d_hit)
        d_fn += int(not d_hit)
        d_fp += int(ctrl_deep_fp)
        c_hit = r_hit or d_hit
        c_tp += int(c_hit)
        c_fn += int(not c_hit)
        c_fp += int(ctrl_regex_fp or ctrl_deep_fp)
        if not c_hit and len(misses) < 5:
            misses.append({"case_id": c["case_id"], "category": c["category"],
                           "score": round(DS.score(model, text), 3)
                           if model else None,
                           "text": text[:160]})

    def pack(tp_, fp_, fn_):
        n = tp_ + fn_
        prec = None if (tp_ + fp_) == 0 else round(tp_ / float(tp_ + fp_), 3)
        return {"recall": round(tp_ / float(n), 3) if n else None,
                "precision": prec,
                "fp_rate": round(fp_ / float(n), 3) if n else None,
                "tp": tp_, "fp": fp_, "fn": fn_}

    return {
        "cases": len(cases),
        "regex_only": pack(tp, fp, fn),
        "deep_only": pack(d_tp, d_fp, d_fn) if model else None,
        "combined": pack(c_tp, c_fp, c_fn) if model else None,
        "latency_ms": {
            "regex_p50": round(statistics.median(regex_t), 3) if regex_t else None,
            "regex_p95": round(sorted(regex_t)[int(0.95 * (len(regex_t) - 1))], 3)
            if regex_t else None,
            "deep_p50": round(statistics.median(deep_t), 3) if deep_t else None,
            "deep_p95": round(sorted(deep_t)[int(0.95 * (len(deep_t) - 1))], 3)
            if deep_t else None,
        },
        "sample_misses": misses,
    }


def tune_threshold(model, val_cases, max_fp_rate, pool=None):
    """只在 validation 上调阈值。

    做法：正例/对照各算一次对数几率，再在候选阈值上做累计判断 ——
    比「每个阈值重算一遍特征」快两个数量级（32 秒 → 亚秒），
    候选取观测分位点而不是固定网格，避免阈值卡在网格边界上。
    """
    pos = [DS.score(model, c["tool_response"]) for c in val_cases]
    neg = [DS.score(model, control_text(c, pool)) for c in val_cases
           if control_text(c, pool).strip()]
    if not pos:
        return {"threshold": None, "recall": None, "fp_rate": None}
    cands = set()
    for arr in (pos, neg or []):
        ordered = sorted(arr)
        for q in (0.0, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95,
                  0.99, 1.0):
            idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
            cands.add(round(ordered[idx], 4))
    best = None
    for th in sorted(cands):
        tp = sum(1 for v in pos if v > th)
        fn = len(pos) - tp
        fp = sum(1 for v in neg if v > th)
        n = tp + fn
        if not n:
            continue
        recall, fp_rate = tp / float(n), (fp / float(len(neg)) if neg else 0.0)
        if fp_rate > max_fp_rate:
            continue
        key = (round(recall, 4), -round(fp_rate, 4), th)
        if best is None or key > best[0]:
            best = (key, {"threshold": round(th, 3), "recall": round(recall, 3),
                          "fp_rate": round(fp_rate, 3),
                          "candidates": len(cands)})
    if best is None:                         # 找不到满足误报上限的阈值
        return {"threshold": None, "recall": None, "fp_rate": None,
                "candidates": len(cands)}
    return best[1]


def train_on(cases, by="payload"):
    parts, meta = split(cases, by)
    pos = [c["tool_response"] for c in parts["train"]]
    neg = [control_text(c, POOL_TRAIN) for c in parts["train"]
           if control_text(c, POOL_TRAIN).strip()]
    model = DS.train(pos, neg)
    tuned = tune_threshold(model, parts["val"], GATE["max_val_fp_rate"],
                           POOL_TRAIN)
    if tuned["threshold"] is not None:
        model["threshold"] = tuned["threshold"]
    return model, parts, meta, tuned


def post_check(test, test_regex):
    """独立复核：拿 test 决定「发布 / 暂缓」，但不许回头调参数。

    为什么单列一步：validation 只覆盖同一批类别/话术的混合，跨域时完全不成立
    —— 按业务工具划分的实测就是 validation 通过、test 误报 33%。
    用 test 决定「要不要发布」是标准做法；用它调阈值才是作弊。
    """
    problems = []
    if not test:
        return False, ["没有 test 结果"]
    base = (test_regex or {}).get("recall") or 0.0
    gain = (test["recall"] or 0.0) - base
    if (test["fp_rate"] or 0.0) > GATE["max_test_fp_rate"]:
        problems.append("test 误报率 %.3f > %.2f"
                        % (test["fp_rate"], GATE["max_test_fp_rate"]))
    if gain < GATE["min_test_recall_gain"]:
        problems.append("test 召回增量 %.3f < %.2f（仅正则 %.3f → 合并 %.3f）"
                        % (gain, GATE["min_test_recall_gain"], base,
                           test["recall"]))
    return not problems, problems


def gate_decision(val_regex, val_combined, tuned):
    """晋升门禁：只看 validation，不碰 test。"""
    problems = []
    if tuned["threshold"] is None:
        problems.append("validation 上找不到满足误报上限的阈值")
    if not val_combined:
        problems.append("没有模型可评")
        return False, problems
    if val_combined["precision"] is None or \
            val_combined["precision"] < GATE["min_val_precision"]:
        problems.append("精确率 %s < %.2f" % (val_combined["precision"],
                                            GATE["min_val_precision"]))
    base = (val_regex or {}).get("recall") or 0.0
    gain = (val_combined["recall"] or 0.0) - base
    if gain < GATE["min_val_recall_gain"]:
        problems.append("召回增量 %.3f < %.2f（仅正则 %.3f → 合并 %.3f）"
                        % (gain, GATE["min_val_recall_gain"], base,
                           val_combined["recall"]))
    if (val_combined["fp_rate"] or 0.0) > GATE["max_val_fp_rate"]:
        problems.append("误报率 %.3f > %.2f"
                        % (val_combined["fp_rate"], GATE["max_val_fp_rate"]))
    return not problems, problems


def _markdown(data):
    g, m = data["gate"], data["metrics"]
    L = ["# 让检测能力自己长：第二级模型 + 晋升门禁", "",
         "> 起因：外部语料上，正则对自然语言注入的召回是 **0.000**，"
         "对字面注入话术是 1.000 —— 检测能力被攻击者的措辞绑死了。",
         "> 做法：用**没通过**的样本训练一个字符 n-gram 朴素贝叶斯做第二级，"
         "只在一级没命中时启用；阈值只在 validation 上调，test 只报数。", "",
         "## 划分（按组，不按条）", "",
         "| 项 | 值 |", "|---|---|",
         "| 分组依据 | `%s` |" % data["split"]["by"],
         "| 组数 | %d（train %d / val %d / test %d）"
         % (data["split"]["groups"], data["split"]["train_groups"],
            data["split"]["val_groups"], data["split"]["test_groups"]),
         "| 用例数 | train %d / val %d / test %d"
         % (m["train"]["cases"], m["val"]["cases"], m["test"]["cases"]),
         "| 剔除 | %d 条（模板锚点对不上，构造不出严格最小对照）"
         % data["excluded_cases"],
         "| 语料指纹 | `%s` |" % data["corpus_sha"][:16],
         "| 模型指纹 | `%s` |" % data["model_hash"][:16],
         "", "同一句攻击话术只会落在一个划分里：按条乱分会让 test 全是背过的题。", "",
         "## 三种形态（test 上）", "",
         "| 形态 | 召回 | 精确率 | 误报率 |", "|---|---|---|---|"]
    for name, key in (("仅正则（一级）", "regex_only"),
                      ("仅模型（二级）", "deep_only"),
                      ("正则 + 模型", "combined")):
        v = m["test"].get(key)
        if v:
            L.append("| %s | %s | %s | %s |" % (name, v["recall"],
                                                v["precision"], v["fp_rate"]))
    L += ["", "## 耗时（每条文本，毫秒）", "",
          "| 一级 正则 p50 / p95 | 二级 模型 p50 / p95 |", "|---|---|",
          "| %s / %s | %s / %s |" % (m["test"]["latency_ms"]["regex_p50"],
                                     m["test"]["latency_ms"]["regex_p95"],
                                     m["test"]["latency_ms"]["deep_p50"],
                                     m["test"]["latency_ms"]["deep_p95"]),
          "",
          "第二级只在**一级没命中**时才跑，所以正常流量（大量干净文本）基本不付这份开销；",
          "它买的是「字面之外」的召回。", "",
          "## 晋升门禁（只看 validation）", "",
          "| 条件 | 阈值 | 实际 | 结果 |", "|---|---|---|---|"]
    for name, need, got, ok in g["checks"]:
        L.append("| %s | %s | %s | %s |" % (name, need, got, "通过" if ok else "未通过"))
    L += ["", "**第一步（validation，决定要不要晋升）：%s**"
          % ("晋升" if g["promoted"] else "不晋升"), ""]
    if g["problems"]:
        L += ["- %s" % x for x in g["problems"]]
    L += ["", "**第二步（test，决定能不能发布）：%s**"
          % ("可以发布" if g["post_check"]["passed"] else "暂缓发布"), ""]
    if g["post_check"]["problems"]:
        L += ["- %s" % x for x in g["post_check"]["problems"]]
    L += ["", "> test 只用于决定「发布 / 暂缓」，看完 test 不再回头改阈值；"
          "两段分开是为了不让「调参」和「验收」混成一个动作。"]
    L += ["", "> 最终结论：**%s**" % ("可用" if g["released"] else "不可用"), ""]
    L += ["", "## 模型学到了什么（可复核）", "",
          "| n-gram | 权重（对数几率） |", "|---|---|"]
    for x in data["top_weights"]:
        L.append("| `%s` | %s |" % (x["ngram"].replace("\n", " "), x["weight"]))
    L += ["", "## 仍然漏掉的（test 样例）", ""]
    for x in m["test"]["sample_misses"]:
        L += ["- `%s` ｜ %s ｜ 归一化分数 %s" % (x["case_id"], x["category"],
                                          x["score"]),
              "  - %s" % x["text"].replace("\n", " ")]
    L += ["", "## 跨分布探针（最重要的一条边界）", "",
          "在 %d 条**正常用户请求**上，模型判为注入的有 **%d 条（%s）**。"
          % (data["cross_distribution_probe"]["benign_user_instructions"],
             data["cross_distribution_probe"]["flagged"],
             data["cross_distribution_probe"]["fp_rate"]),
          "",
          "也就是说：它学的是「这段文本像不像一条指令」，不是「这条指令是不是攻击者塞的」。",
          "放在 `tool_output` / `peer_message` 这类**本来就不该出现指令**的落点是对的"
          "（工具返回里出现「请把权限给 Amy」本身就是异常）；",
          "放在 `user_input` 上会把正常请求全部拦掉 —— 所以 `deep_scan.applicable()` "
          "只允许前两类落点。", "",
          "## 边界（不粉饰）", "",
          "- 这是个**字符 n-gram 模型**，学的是措辞分布，不是「理解」注入意图；"
          "换一种语言或换一代攻击话术，召回会掉。",
          "- 语料只有 62 句不同的攻击话术，按组划分后 test 只有十几句 → "
          "test 上的数字方差大，别当稳定指标用。",
          "- 它只回答「这段文本像不像被投毒的返回」，不判断「这次调用该不该放行」；"
          "授权仍归 `tool_contract` + 审批门禁。",
          "- 语料不入库，重跑要先 `import_external_attacks.py`。", "",
          "## 复现", "", "```bash",
          "python scripts/import_external_attacks.py",
          "python scripts/train_injection_detector.py --by payload",
          "python scripts/train_injection_detector.py --by tool", "```", ""]
    return "\n".join(L)


def run(src=DEFAULT_SRC, out=DEFAULT_OUT, by="payload"):
    cases, dropped = load_cases(src)
    with open(os.path.join(src, "cases.jsonl"), "rb") as f:
        corpus_sha = hashlib.sha256(f.read()).hexdigest()

    model, parts, meta, tuned = train_on(cases, by)
    mhash = DS.model_hash(model)
    val = evaluate_part(model, parts["val"], model["threshold"], POOL_TRAIN)
    # test 的对照组用**没见过的填充语**：这才是泛化，而不是记住了某句话
    test = evaluate_part(model, parts["test"], model["threshold"], POOL_UNSEEN)
    train_eval = evaluate_part(model, parts["train"], model["threshold"],
                               POOL_TRAIN)

    promoted, problems = gate_decision(val["regex_only"], val["combined"], tuned)
    released, post_problems = post_check(test["combined"], test["regex_only"])
    checks = [
        ("精确率（val）", ">= %.2f" % GATE["min_val_precision"],
         (val["combined"] or {}).get("precision"), "precision_ok"),
        ("召回增量（val）", ">= +%.2f" % GATE["min_val_recall_gain"],
         None if not val["combined"] else
         round((val["combined"]["recall"] or 0) - (val["regex_only"]["recall"] or 0), 3),
         "gain_ok"),
        ("误报率（val）", "<= %.2f" % GATE["max_val_fp_rate"],
         (val["combined"] or {}).get("fp_rate"), "fp_ok"),
    ]
    pretty = []
    for name, need, got, _k in checks:
        ok = got is not None and (
            (name.startswith("精确率") and got >= GATE["min_val_precision"]) or
            (name.startswith("召回增量") and got >= GATE["min_val_recall_gain"]) or
            (name.startswith("误报率") and got <= GATE["max_val_fp_rate"]))
        pretty.append((name, need, got, ok))

    # 跨分布探针：模型在**正常用户请求**上的误报（它学的是「像指令」）
    instr = {}
    for c in cases:
        instr.setdefault(c["user_instruction"], True)
    instr = [x for x in instr if x.strip()]
    instr_fp = sum(1 for x in instr
                   if DS.classify(model, x, model["threshold"])["is_injection"])
    cross = {"benign_user_instructions": len(instr),
             "flagged": instr_fp,
             "fp_rate": round(instr_fp / float(len(instr)), 3) if instr else None,
             "why": "模型学的是「这段文本像指令」，不是「指令是攻击者塞的」。"
                    "所以只装在 tool_output / peer_message 这类本来就不该出现指令的落点；"
                    "装到 user_input 上会把所有请求都判成注入。"}

    top = sorted(model["weights"].items(), key=lambda kv: -kv[1])[:10]
    data = {
        "version": 1,
        "corpus_sha": corpus_sha,
        "benign_filler_pool": {"train": len(POOL_TRAIN),
                               "test_unseen": len(POOL_UNSEEN)},
        "split": meta,
        "excluded_cases": dropped,
        "threshold_tuning": tuned,
        "gate_criteria": GATE,
        "gate": {"promoted": promoted, "problems": problems,
                 "checks": pretty,
                 "released": released and promoted,
                 "post_check": {"passed": released, "problems": post_problems,
                                "on": "test（只用于决定发布，不用于调参）"}},
        "metrics": {"train": train_eval, "val": val, "test": test},
        "cross_distribution_probe": cross,
        "applicable_where": list(DS.APPLICABLE_WHERE),
        "top_weights": [{"ngram": k, "weight": v} for k, v in top],
        "model_hash": mhash,
        "latency_budget_note": "第二级仅在一级未命中时运行；p95 见上表。",
    }

    os.makedirs(out, exist_ok=True)
    model_out = dict(model, promoted=promoted, corpus_sha=corpus_sha)
    DS.dump(model_out, os.path.join(out, "model.json"))
    with open(os.path.join(out, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"corpus_sha256": corpus_sha, "model_sha256": mhash,
                   "split": meta,
        "excluded_cases": dropped, "gate": promoted,
                   "by": by}, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="训练第二级注入检测并走晋升门禁")
    ap.add_argument("--by", default="payload", choices=["payload", "tool"],
                    help="分组划分依据：payload（攻击话术）/ tool（业务工具）")
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    out = args.out or (DEFAULT_OUT if args.by == "payload"
                       else DEFAULT_OUT + "_by_tool")
    d = run(args.src, out, args.by)
    t = d["metrics"]["test"]
    print("[√] 产物：%s" % os.path.join(out, "report.md"))
    print("    test：仅正则 %s ｜ 仅模型 %s ｜ 合并 %s"
          % (t["regex_only"]["recall"], (t["deep_only"] or {}).get("recall"),
             (t["combined"] or {}).get("recall")))
    print("    门禁：%s%s" % ("晋升" if d["gate"]["promoted"] else "不晋升",
                            "" if d["gate"]["promoted"] else
                            "（%s）" % "；".join(d["gate"]["problems"])))
    return d


if __name__ == "__main__":
    main()
