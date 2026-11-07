# -*- coding: utf-8 -*-
"""把「C 到底命中了哪条技能」摊开看 —— 只做归因，不跑模型。

为什么需要它：C 与 D 的差别只在 `skills.lookup()` 的匹配条件
（C 只看 agent，D 要 agent + 故障类型）。所以 C 的成功率不是模型行为，
而是「同 agent 下它取到了哪条技能」。不看这一步，
「C 在复合故障上 40%」这种数字就只能靠猜——而猜出来的解读通常是错的。

跑法（在 EvoGuardLab 目录下，且 real_model_bench 已经跑完）：
    python ai-service/real_model_attribution.py

输出：
    屏幕表格 + experiments/real_model/attribution.md
"""
import collections
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "experiments", "real_model")
CHECKPOINT = os.path.join(OUT, "runs.jsonl")
DB = os.path.join(ROOT, "data", "real_model.db")
TARGET = os.path.join(OUT, "attribution.md")


def load_done():
    done = {}
    if not os.path.exists(CHECKPOINT):
        return done
    with open(CHECKPOINT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            done[r["key"]] = r
    return done


def load_recoveries():
    """(suite, agent, mode) -> [skill_id, ...]，以及 run 级明细。

    一次 run 可能有多个恢复记录（策略链逐步写审计），这里只取每条记录
    实际用到的 skill_id，用来回答「这一组用例到底吃的是哪条技能」。
    """
    if not os.path.exists(DB):
        return {}, {}
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    # 技能表：把不透明的 id 翻译成「故障类型:策略」，否则表格读不懂
    label = {}
    try:
        for row in c.execute("SELECT id,task_type,fault_type,strategy,status,version "
                             "FROM skills"):
            label[row["id"]] = "%s/%s→%s(%s v%s)" % (
                row["task_type"], row["fault_type"], row["strategy"],
                row["status"], row["version"])
    except Exception:
        pass

    q = """SELECT e.name, u.mode, r.strategy, r.params
           FROM recoveries r
           JOIN runs u ON u.id = r.run_id
           JOIN experiments e ON e.id = u.experiment_id
           WHERE e.name LIKE 'real/%'"""
    per_run = collections.defaultdict(list)
    for row in c.execute(q):
        parts = row["name"].split("/")
        if len(parts) < 4:
            continue
        suite, agent = parts[1], parts[2]
        p = {}
        if row["params"]:
            try:
                p = json.loads(row["params"])
            except Exception:
                p = {}
        sid = p.get("skill_id") or ""
        per_run[(suite, agent, row["mode"])].append({
            "skill_id": label.get(sid, sid) or "(无技能·按根因查表)",
            "chain": p.get("chain") or [row["strategy"]],
        })
    c.close()
    return per_run, label


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    done = load_done()
    rec, _label = load_recoveries()
    if not done:
        print("[×] 没有检查点，先跑 real_model_bench.py")
        return 1

    # 1) 按 (suite, agent, mode) 统计：成功/总数 + 命中的技能
    groups = collections.defaultdict(lambda: {"n": 0, "ok": 0})
    for key, r in done.items():
        suite, agent, _task, _fault, mode = key.split("|")
        g = groups[(suite, agent, mode)]
        g["n"] += 1
        g["ok"] += 1 if r["success"] else 0
    for (suite, agent, mode), g in groups.items():
        hits = rec.get((suite, agent, mode), [])
        g["skills"] = collections.Counter(x["skill_id"] for x in hits)
        g["chains"] = collections.Counter(json.dumps(x["chain"], ensure_ascii=False)
                                          for x in hits)

    # 2) 复合故障按模式 × 模式类型（故障组合）统计
    pat = collections.defaultdict(lambda: {"n": 0, "ok": 0})
    for key, r in done.items():
        suite, _agent, _task, fault, mode = key.split("|")
        if suite != "composite":
            continue
        p = pat[(fault, mode)]
        p["n"] += 1
        p["ok"] += 1 if r["success"] else 0

    L = ["# C / D 命中归因（不是新实验，只是把已有 run 的原因摊开）", "",
         "> 由 `ai-service/real_model_attribution.py` 从 `runs.jsonl` + `data/real_model.db` 生成。",
         "> 用途：解释「C 为什么在这个数字上」，避免把匹配问题误读成模型问题。", ""]

    L.append("## 一、每个 agent 在 C / D 下命中的技能")
    L.append("")
    L.append("| 组 | 模式 | 成功/总数 | 命中的技能（次数） | 策略链（次数） |")
    L.append("|---|---|---|---|---|")
    for suite in ("single", "composite"):
        for agent in ("order", "kb", "planner"):
            for mode in ("C", "D"):
                g = groups.get((suite, agent, mode))
                if not g or not g["n"]:
                    continue
                sk = ", ".join("%s×%d" % (k or "-", v)
                               for k, v in g["skills"].most_common()) or "-"
                ch = " / ".join("%s×%d" % (k, v) for k, v in g["chains"].most_common()) or "-"
                L.append("| %s/%s | %s | %d/%d | %s | %s |"
                         % (suite, agent, mode, g["ok"], g["n"], sk, ch))
    L.append("")

    L.append("## 二、复合故障：按故障组合看各模式")
    L.append("")
    L.append("| 故障组合 | A | B | C | D |")
    L.append("|---|---|---|---|---|")
    for fault in sorted({f for (f, _m) in pat}):
        cells = []
        for mode in ("A", "B", "C", "D"):
            p = pat.get((fault, mode))
            cells.append("%d/%d" % (p["ok"], p["n"]) if p else "-")
        L.append("| %s | %s | %s | %s | %s |" % (fault, cells[0], cells[1],
                                                 cells[2], cells[3]))
    L.append("")

    with open(TARGET, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    # 屏幕输出：只打关键几张表
    print("== 命中归因 ==")
    for suite in ("single", "composite"):
        for agent in ("order", "kb", "planner"):
            for mode in ("C", "D"):
                g = groups.get((suite, agent, mode))
                if not g or not g["n"]:
                    continue
                sk = ", ".join("%s×%d" % (k or "-", v)
                               for k, v in g["skills"].most_common())
                print("  %-9s %-8s %s  %d/%d  技能: %s"
                      % (suite, agent, mode, g["ok"], g["n"], sk))
    print("  写入 %s" % TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
