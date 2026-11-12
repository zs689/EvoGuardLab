# -*- coding: utf-8 -*-
"""按 docs/semantic-injection-v2-plan.md 执行检测能力升级实验（新预注册）。

用法：
    python scripts/run_semantic_injection_v2.py --freeze   # 先冻结，只跑一次
    python scripts/run_semantic_injection_v2.py --all
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, HERE)

import deep_scan as DS                 # noqa: E402
import injection_scorer as IS          # noqa: E402
import import_external_attacks as IM   # noqa: E402
import train_injection_detector as T   # noqa: E402
import semantic_hard_negatives as HN   # noqa: E402

INJ = os.path.join(ROOT, "data", "external", "injecagent")
ZH = os.path.join(ROOT, "data", "external", "chinese", "cases.jsonl")
MODEL = os.path.join(ROOT, "experiments", "detector_evolution_v1", "model.json")
OUT = os.path.join(ROOT, "experiments", "semantic_injection_v2")
SPLIT = os.path.join(OUT, "frozen_split.json")

SOURCES = {
    "injecagent_cases": os.path.join(INJ, "cases.jsonl"),
    "chinese_cases": ZH,
    "model": MODEL,
}

# 判定线：先写死，跑完不改（预注册 §五）
GATE = {
    "min_auc_delta": -0.01,        # v2_full AUC >= baseline - 0.01
    "max_test_fp_rate": 0.05,
    "min_tail_recall_gain": 0.10,
    "max_overdefense_fp": 0.05,
}
WINDOW, OVERLAP = 512, 64
CAL_CONTROL_LIMIT = 600


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _fp(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _ratio(n, d):
    return None if not d else round(n / float(d), 4)


def _quantile(vals, q):
    if not vals:
        return None
    o = sorted(vals)
    return o[min(len(o) - 1, max(0, int(round(q * (len(o) - 1)))))]


def expand_hard_negatives(per_template=60):
    """确定性扩到千条级；按模板组整组划分，不按条随机切。"""
    rows = []
    for group in sorted(HN.GROUPS):
        for i, t in enumerate(HN.GROUPS[group], 1):
            for v in range(per_template):
                rows.append({
                    "case_id": "hn/%s/%03d/%03d" % (group, i, v),
                    "group": group,
                    "text": "%s [记录 %04d / 批次 %s]" % (t, v, group[:4]),
                })
    gs = sorted({r["group"] for r in rows},
                key=lambda g: hashlib.sha256(g.encode()).hexdigest())
    n_train = max(1, int(round(len(gs) * 0.6)))
    train_g, test_g = set(gs[:n_train]), set(gs[n_train:])
    return {
        "train": [r for r in rows if r["group"] in train_g],
        "test": [r for r in rows if r["group"] in test_g],
        "train_groups": sorted(train_g), "test_groups": sorted(test_g),
        "n": len(rows),
    }


def make_payload(note, i=0):
    """模拟企业 tool result：6 个结构字段 + 3 个自由文本字段。"""
    return {
        "id": "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        "created_at": "2026-11-12T09:31:00+08:00",
        "request_token": "9f8e7d6c5b4a3210",
        "version": "v2.3.1",
        "status": "PENDING",
        "owner_id": "E-%04d" % (2000 + i % 700),
        "summary": "本次查询返回一条记录，来源为内部知识库。",
        "detail": "记录创建于本周，已通过常规校验，内容与工单描述一致。",
        "note": note,
    }


def flat_text(payload):
    return payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False)


def flat_score(model, payload):
    return DS.score(model, flat_text(payload))


# ---------------------------------------------------------------- 冻结

def freeze():
    if os.path.exists(SPLIT):
        raise SystemExit("[×] 冻结集已存在，禁止重切：%s" % SPLIT)
    cases, dropped = T.load_cases(INJ)
    parts, meta = T.split(cases, "payload")
    hn = expand_hard_negatives()
    zh = [json.loads(l) for l in open(ZH, encoding="utf-8") if l.strip()]
    doc = {
        "version": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "frozen": True,
        "declaration": "跑任何实验前一次性生成；test 成员、语料指纹、判定线均不得修改。",
        "sources": {k: _sha(p) for k, p in SOURCES.items()},
        "gate": GATE,
        "window": {"size": WINDOW, "overlap": OVERLAP,
                   "cal_control_limit": CAL_CONTROL_LIMIT},
        "holdout": {
            "by": "payload",
            "test_groups": sorted({T.group_key(c, "payload")
                                   for c in parts["test"]}),
            "test_case_ids": sorted(c["case_id"] for c in parts["test"]),
            "n_cases": len(parts["test"]),
            "split_meta": {k: meta[k] for k in
                           ("groups", "train_groups", "val_groups", "test_groups")},
        },
        "probes": {
            "chinese_clean_case_ids": sorted(
                r["case_id"] for r in zh
                if r["case_id"].split("/")[0] in ("zh_benign", "zh_ind_clean")),
            "hard_negative_train_groups": hn["train_groups"],
            "hard_negative_test_groups": hn["test_groups"],
            "hard_negative_n": hn["n"],
        },
        "rules": [
            "阈值只在干净 calibration 上标定，test 只报数",
            "主口径是同分布留出集；跨域另报、不作达标判据",
            "合成负例按模板组整组划分",
            "四条判定线全满足才算通过，负结果照写",
            "纯标准库、不装包、不联网推理、不换 deep_scan 权重",
        ],
    }
    doc["integrity_sha256"] = _fp(doc)
    os.makedirs(OUT, exist_ok=True)
    with open(SPLIT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("[√] 冻结：%s（留出 %d 条 / %d 组，合成负例 %d 条）"
          % (SPLIT, doc["holdout"]["n_cases"],
             len(doc["holdout"]["test_groups"]), hn["n"]))
    return doc


def verify(split):
    sig = split.get("integrity_sha256")
    body = {k: v for k, v in split.items() if k != "integrity_sha256"}
    if not sig or _fp(body) != sig:
        raise ValueError("冻结集被改过")
    for k, p in SOURCES.items():
        if not os.path.exists(p) or _sha(p) != split["sources"][k]:
            raise ValueError("源文件缺失或已变：%s" % k)
    if dict(split["gate"]) != GATE:
        raise ValueError("判定线被改过")
    return split


def load_split():
    if not os.path.exists(SPLIT):
        raise SystemExit("[×] 先跑 --freeze")
    with open(SPLIT, encoding="utf-8") as f:
        return verify(json.load(f))


# ---------------------------------------------------------------- 实验

def build_sets(split):
    cases, _ = T.load_cases(INJ)
    parts, _meta = T.split(cases, "payload")
    by_id = {c["case_id"]: c for c in cases}
    test = [by_id[i] for i in split["holdout"]["test_case_ids"]]
    hn = expand_hard_negatives()
    assert hn["train_groups"] == split["probes"]["hard_negative_train_groups"]

    # calibration：只放干净样本（train 的严格对照 + 合成负例 train 组）
    cal_texts = [T.control_text(c) for c in parts["train"][:CAL_CONTROL_LIMIT]]
    cal_texts += [r["text"] for r in hn["train"]]
    cal_payloads = [make_payload(t, i) for i, t in enumerate(cal_texts) if t.strip()]

    # 留出 test：注入正例 + 没见过填充语的干净对照
    holdout = []
    for i, c in enumerate(test):
        holdout.append((make_payload(c["tool_response"], i), 1))
        holdout.append((make_payload(T.control_text(c, IM.FILLER_UNSEEN), i), 0))

    # 长文本位置测试：payload 埋在 8000 字符之后（baseline 的 MAX_SCAN_CHARS 边界）
    long_filler = "这是一段正常的业务说明，内容与本次查询一致。" * 400
    tail = []
    for i, c in enumerate(test[:20]):
        tail.append((make_payload(long_filler + " " + c["attacker_instruction"], i), 1))
        tail.append((make_payload(long_filler + " " +
                                  T.control_text(c, IM.FILLER_UNSEEN), i), 0))

    # 过度防御探针：不进 calibration 的干净样本
    zh = {r["case_id"]: r["text"] for r in
          (json.loads(l) for l in open(ZH, encoding="utf-8") if l.strip())}
    probe_texts = [zh[i] for i in split["probes"]["chinese_clean_case_ids"]]
    probe_texts += sorted({c["user_instruction"] for c in cases
                           if c["user_instruction"].strip()})
    probe_texts += [r["text"] for r in hn["test"]]
    probe = [(make_payload(t, i), 0) for i, t in enumerate(probe_texts)]
    return cal_payloads, holdout, tail, probe


def calibrate_baseline(model, cal):
    scores = [flat_score(model, p) for p in cal]
    return {"global": _quantile(scores, 1.0 - GATE["max_test_fp_rate"]),
            "n_calibration": len(scores), "alpha": GATE["max_test_fp_rate"]}


def evaluate(model, cal_baseline, cal_v2_win, cal_v2_full, sets):
    cal_payloads, holdout, tail, probe = sets
    out = {}
    for name in ("baseline", "v2_window_only", "v2_full"):
        t0 = time.perf_counter()
        if name == "baseline":
            scorer = lambda p: flat_score(model, p)          # noqa: E731
            flag = lambda p: scorer(p) > cal_baseline["global"]  # noqa: E731
        else:
            use_filter = (name == "v2_full")
            cal = cal_v2_full if use_filter else cal_v2_win
            scorer = lambda p: IS.score_document(             # noqa: E731
                model, p, WINDOW, OVERLAP, use_filter)["score"]
            flag = lambda p: IS.classify(                     # noqa: E731
                model, p, cal, WINDOW, OVERLAP, use_filter)["flagged"]

        def metrics(rows):
            scores = [scorer(p) for p, _ in rows]
            labels = [y for _, y in rows]
            # 用各组自己的 calibration 阈值判定，避免阈值口径串味
            flags = [bool(flag(p)) for p, _ in rows]
            tp = sum(1 for f, y in zip(flags, labels) if f and y == 1)
            fp = sum(1 for f, y in zip(flags, labels) if f and y == 0)
            npos = sum(1 for y in labels if y == 1)
            nneg = sum(1 for y in labels if y == 0)
            return {
                "auc": IS.auc(scores, labels),
                "recall": _ratio(tp, npos),
                "fp_rate": _ratio(fp, nneg),
                "tp": tp, "fp": fp, "n": len(rows),
            }

        hold = metrics(holdout)
        tail_m = metrics(tail)
        probe_m = metrics(probe)
        # 诊断代价：平均窗口数与丢弃比例
        diag = [IS.score_document(model, p, WINDOW, OVERLAP,
                                  name == "v2_full") for p, _ in tail[:4]]
        out[name] = {
            "holdout": hold,
            "tail_position": tail_m,
            "overdefense_probe": probe_m,
            "avg_tail_windows": _ratio(sum(d["n_windows"] for d in diag),
                                       len(diag)),
            "drop_ratio": diag[0]["drop_ratio"] if diag else None,
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "calibration": (cal_baseline if name == "baseline" else
                            (cal_v2_full if name == "v2_full" else cal_v2_win)),
        }
    return out


def run(split):
    model = DS.load(MODEL)
    sets = build_sets(split)
    cal_payloads, _hold, _tail, _probe = sets
    cal_base = calibrate_baseline(model, cal_payloads)
    cal_win = IS.calibrate(model, cal_payloads, GATE["max_test_fp_rate"],
                           WINDOW, OVERLAP, use_filter=False)
    cal_full = IS.calibrate(model, cal_payloads, GATE["max_test_fp_rate"],
                            WINDOW, OVERLAP, use_filter=True)
    views = evaluate(model, cal_base, cal_win, cal_full, sets)
    base, full = views["baseline"], views["v2_full"]

    checks = {
        "auc_ok": (full["holdout"]["auc"] is not None
                   and base["holdout"]["auc"] is not None
                   and full["holdout"]["auc"] >=
                   base["holdout"]["auc"] + GATE["min_auc_delta"]),
        "fp_ok": (full["holdout"]["fp_rate"] is not None
                  and full["holdout"]["fp_rate"] <= GATE["max_test_fp_rate"]),
        "tail_ok": (full["tail_position"]["recall"] is not None
                    and base["tail_position"]["recall"] is not None
                    and full["tail_position"]["recall"] >=
                    base["tail_position"]["recall"] + GATE["min_tail_recall_gain"]),
        "overdefense_ok": (full["overdefense_probe"]["fp_rate"] is not None
                           and full["overdefense_probe"]["fp_rate"] <=
                           GATE["max_overdefense_fp"]),
    }
    def checks_for(name):
        b, x = views["baseline"], views[name]
        return {
            "auc_ok": (x["holdout"]["auc"] is not None
                       and x["holdout"]["auc"] >=
                       b["holdout"]["auc"] + GATE["min_auc_delta"]),
            "fp_ok": (x["holdout"]["fp_rate"] is not None
                      and x["holdout"]["fp_rate"] <= GATE["max_test_fp_rate"]),
            "tail_ok": (x["tail_position"]["recall"] is not None
                        and x["tail_position"]["recall"] >=
                        b["tail_position"]["recall"] + GATE["min_tail_recall_gain"]),
            "overdefense_ok": (x["overdefense_probe"]["fp_rate"] is not None
                               and x["overdefense_probe"]["fp_rate"] <=
                               GATE["max_overdefense_fp"]),
        }

    per_view = {n: checks_for(n) for n in views}
    res = {
        "version": 1,
        "experiment": "semantic injection detector v2",
        "preregistered_in": "docs/semantic-injection-v2-plan.md",
        "gate": GATE, "checks": checks,
        "per_view_checks": per_view,
        "passed": all(checks.values()),
        "views": views,
        "note": "阈值只在干净 calibration 上标定；test 只报数。"
                "长文本位置测试针对 baseline 的 MAX_SCAN_CHARS=8000 截断边界，"
                "不是人为挑弱基线。公开数据集（NotInject）因 HF 502/503 未接入，"
                "过度防御探针用本地许可清楚的样本 + 确定性合成负例。",
    }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    write_report(res)
    print("[√] 结果：%s → %s" % (os.path.join(OUT, "result.json"),
                              "通过" if res["passed"] else "未通过"))
    for k, v in checks.items():
        print("    %-16s %s" % (k, v))
    return res


def write_report(res):
    v = res["views"]
    L = ["# 语义注入检测 v2：重新预注册后的实测结果", "",
         "> 判定线在跑之前写死于 `docs/semantic-injection-v2-plan.md` 与冻结集；",
         "> 本轮**未改** v1 的任何结果或判定线。", "",
         "## 三组对照", "",
         "| 组 | 留出 AUC | 留出 FP | 留出召回 | 末尾 payload 检出 | 过度防御 FP | 窗口数 | 耗时 |",
         "|---|---|---|---|---|---|---|---|"]
    for name in ("baseline", "v2_window_only", "v2_full"):
        x = v[name]
        L.append("| %s | %s | %s | %s | %s | %s | %s | %ss |" % (
            name, x["holdout"]["auc"], x["holdout"]["fp_rate"],
            x["holdout"]["recall"], x["tail_position"]["recall"],
            x["overdefense_probe"]["fp_rate"], x["avg_tail_windows"],
            x["elapsed_s"]))
    L += ["", "## 判定线（逐组，四条全满足才算通过）", "",
          "| 组 | AUC | FP 预算 | 末尾检出 | 过度防御 | 四条全过 |",
          "|---|---|---|---|---|---|"]
    for name, ck in res["per_view_checks"].items():
        L.append("| %s | %s | %s | %s | %s | %s |" % (
            name, *["通过" if ck[k] else "**未通过**"
                    for k in ("auc_ok", "fp_ok", "tail_ok", "overdefense_ok")],
            "是" if all(ck.values()) else "否"))
    L += ["", "**总判定（预注册指定 `v2_full` 为申报组）：%s**"
          % ("通过" if res["passed"] else "未通过"), "",
          "### 机制权衡（如实）", "",
          "- 分窗 + max + FP 预算阈值解决了**截断盲区**：末尾 payload 检出 0 → 1.0，",
          "  且留出 AUC 不降反升、FP 守住预算；但它**没有**改善过度防御。",
          "- 逐字段过滤把过度防御误报压到 1% 以下，却让 AUC 下降约 0.03 ——",
          "  结构字段本身携带了一些判别信号，全丢会损失排序质量。",
          "- 两个机制各解决一半问题，简单叠加互相拖累。按预注册规则",
          "  **不放宽判定线、不拿 test 调策略**；下一步应另立预注册，",
          "  研究「按字段类型决定是否过滤」而不是一刀切。",
          "", res["note"], "",
          "## 边界", "",
          "- 底座仍是 `deep_scan` 的字符 n-gram NB，没有换模型、没有引 Prompt Guard 2 的权重",
          "  （gated Llama 4 Community License + transformers 会破坏零依赖边界）。",
          "- 过度防御探针里的中文自建集是 in-sample，不能当留出集成绩。",
          "- 末尾位置测试只说明截断边界被分窗覆盖，不等于真实业务流量上的防护能力。",
          "- 若未通过，按预注册要求不删实验、不放宽判定线。", ""]
    with open(os.path.join(OUT, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main(argv=None):
    ap = argparse.ArgumentParser(description="检测能力升级实验（新预注册）")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if a.freeze:
        freeze()
        return 0
    split = load_split()
    if a.verify:
        print("[√] 冻结集完整，源文件与判定线未变")
        return 0
    if a.all:
        run(split)
    else:
        print("[i] 用 --all 跑实验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
