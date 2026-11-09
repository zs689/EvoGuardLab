# -*- coding: utf-8 -*-
"""生成带 ground truth 的标注集：每条轨迹的标签来自**执行时的注入状态**，不是人读出来的。

三类标签怎么来（全部由构造保证，可复核）：
  clean     —— 这次运行**没有注入任何故障**，且执行成功
  recovered —— 注入了故障，恢复后最终成功（最容易误伤的正是这一类）
  failed    —— 注入了故障，最终没救回来

为什么不用「人工阅读轨迹打标」：那是主观的，而且标的人就是写规则的人，必然偏。
用注入状态当标签，至少 ground truth 是可复现的。代价是：它只覆盖「已知故障」，
覆盖不了规则设计之外的问题 —— 所以它算**回归护栏**，不是真实分布的准确率。

跑法（在 EvoGuardLab 目录下）：
    python scripts/build_labeled_set.py
产物：
    tests/fixtures/labeled/labels.json
    tests/fixtures/labeled/traces/*.json   （OTLP 格式，检查器走的代码路径与检查别人时一致）
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core        # noqa: E402
import runtime     # noqa: E402
import sim         # noqa: E402
from evoguard_inspect.loaders import evoguard as eg  # noqa: E402

OUT = os.path.join(ROOT, "tests", "fixtures", "labeled")
TRACES = os.path.join(OUT, "traces")

# 每个 agent 取前 2 个 dev 任务；故障挑「规则表能救」与「救不回」各一类，
# 让 failed / recovered 两类都有。
PLAN = [
    ("order", "tool_timeout"),        # 规则表有 retry -> 可恢复
    ("order", "silent_corruption"),   # 需要 cross_verify
    ("kb", "tool_exception"),
    ("kb", "stale_data"),
    ("planner", "tool_timeout"),
    ("planner", "incorrect_planning"),
]
MODES = ("A", "D")


def main():
    os.environ["AGENTLAB_LLM"] = "mock"      # 确定性，不依赖模型服务
    if os.path.isdir(TRACES):
        shutil.rmtree(TRACES)
    os.makedirs(TRACES)

    tmp = os.path.join(OUT, "_build.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    store = core.Store(tmp, fast=True)

    entries = []
    for agent, fault in PLAN:
        for task in sim.dev_tasks(agent)[:2]:
            for mode in MODES:
                # 1) 无注入，A 模式 -> clean
                cases = [(mode, "none"), (mode, fault)]
                for m, ft in cases:
                    exp = store.create_experiment(
                        name="%s/%s/%s/%s" % (agent, task["task_id"], ft, m),
                        agent_type=agent, task_id=task["task_id"], fault_type=ft,
                        fault_target=0, seed=7, recovery_mode=m)
                    run = runtime.run_once(store, exp, m)
                    success = run["success"] == 1

                    if ft == "none":
                        label, why = "clean", "未注入故障"
                    elif success:
                        label, why = "recovered", "注入了 %s，恢复后成功" % ft
                    else:
                        label, why = "failed", "注入了 %s，未恢复" % ft

                    traces = eg.from_store(store, run["id"])
                    if not traces:
                        continue
                    key = "%s_%s_%s_%s" % (agent, task["task_id"], ft, m)
                    key = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in key)
                    path = os.path.join(TRACES, key + ".json")
                    eg.dump_otel(traces, path, agent_name=agent)
                    entries.append({
                        "key": key,
                        "file": os.path.relpath(path, ROOT).replace(os.sep, "/"),
                        "label": label,
                        "agent": agent,
                        "fault": ft,
                        "mode": m,
                        "success": success,
                        "provenance": "execution_ground_truth",
                        "note": why,
                    })
    store.close()
    os.remove(tmp)

    # 手工构造的攻击轨迹单列：它的标签来自「已知塞了四类攻击」
    entries.append({
        "key": "attacked_demo",
        "file": "tests/fixtures/trace_attack.json",
        "label": "failed",
        "agent": "support-agent",
        "fault": "multi_attack",
        "mode": "-",
        "success": False,
        "provenance": "hand_constructed",
        "note": "注入、越权、外带、XSS 四类特征齐全",
    })

    doc = {
        "version": 2,
        "protocol": "trace-label-v2",
        "backend": "mock",
        "classes": {
            "clean": "未注入故障且成功；期望零 error 级发现",
            "recovered": "注入故障但恢复成功；期望零 error 级发现（误伤记在这里）",
            "failed": "注入故障且未恢复；期望至少一条 error 级发现",
            "unjudgeable": "输入不足，不计入统计",
        },
        "limits": [
            "标签来自执行时的注入状态，可复现，但只覆盖已知故障类型",
            "backend=mock：测的是规则集在确定性环境下的行为，不是模型能力",
            "覆盖不到「规则设计之外」的问题，因此不能当真实分布的准确率对外引用",
        ],
        "entries": entries,
    }
    json.dump(doc, open(os.path.join(OUT, "labels.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    from collections import Counter
    print("写出 %d 条 -> %s" % (len(entries), os.path.join(OUT, "labels.json")))
    print(Counter(e["label"] for e in entries))


if __name__ == "__main__":
    main()
