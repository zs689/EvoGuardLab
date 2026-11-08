# -*- coding: utf-8 -*-
"""难度组基线诊断：只跑「无故障」用例，看模型到底错在哪。

为什么不直接用 real_model_bench 跑：那边一次跑 4 个模式 + 全量故障，
每次 100 秒起步，而调 prompt 需要反复试。这个脚本只跑纯能力基线，
一轮 6 个任务几秒钟，改完立刻能看结果。

跑法（在 EvoGuardLab 目录下，先起 Ollama）：
    python scripts/diag_hard_baseline.py

输出：每个任务的「真值 / 模型答案 / 差在哪」+ 汇总正确率。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

import agents    # noqa: E402
import core      # noqa: E402
import gateway   # noqa: E402
import runtime   # noqa: E402
import sim       # noqa: E402


def classify(agent, answer, expect):
    """把「错了」拆成三种性质不同的错 —— 对策完全不同。"""
    if agent == "calc":
        got = agents._num((answer or {}).get("total"))
        if got is None:
            return "没有输出 total"
        if abs(got - expect["total"]) < 0.02:
            return "正确"
        if abs(got - expect["subtotal"]) < 0.02:
            return "没算：直接抄了小计"
        if abs(got - (expect["subtotal"] - (expect["subtotal"] - expect["after_rebate"]))) < 0.02:
            return "只做了满减，没乘折扣"
        if abs(got - expect["after_rebate"]) < 0.02:
            return "满减后没有再乘折扣"
        if abs(got - round(expect["subtotal"] * 1.0, 2)) < 0.02:
            return "原价，两步都没做"
        return "算了但结果不对（差 %.2f）" % (got - expect["total"])
    # xref
    ok = agents.is_correct(agent, answer or {}, expect)
    if ok:
        return "正确"
    text = (answer or {}).get("text") or ""
    if not text:
        return "没有输出结论"
    return "结论/引用不对（期望 %s 出自 %s）" % (expect["must"], expect["doc"])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    gateway.reset_cache()
    provider = gateway.get_provider()
    try:
        models = provider.list_models()
    except AttributeError:
        models = ["mock"]
    except Exception as e:
        print("[×] 模型服务不可用：%s" % e)
        return 1
    print("[√] 模型 %s ｜ 后端 %s" % (models, provider.name))

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    store = core.Store(path, fast=True)

    ok_n = 0
    rows = []
    for agent in sim.HARD_AGENTS:
        for t in sim.hard_tasks(agent):
            exp = store.create_experiment("diag/" + agent, agent, t["task_id"],
                                          "none", 0, 7, "A")
            run = runtime.run_once(store, exp, "baseline")
            answer = core.loads(run.get("answer"), None)
            expect = core.loads(run.get("expect"), None)
            good = run["success"] == 1
            ok_n += 1 if good else 0
            rows.append((agent, t["task_id"], expect, answer,
                         classify(agent, answer, expect), good))

    print("")
    for agent, tid, expect, answer, why, good in rows:
        print("%-4s %-10s %s" % (agent, tid, "✓" if good else "✗"))
        if agent == "calc":
            print("     真值 total=%s（小计 %s → 满减后 %s）"
                  % (expect["total"], expect["subtotal"], expect["after_rebate"]))
            print("     模型 %s" % (answer.get("total") if isinstance(answer, dict) else answer))
        else:
            print("     真值 %s 出自 %s" % (expect["must"], expect["doc"]))
            print("     模型 %s" % (answer if answer else "（空）"))
        print("     判定：%s" % why)
    print("")
    print("== 基线正确率 %d/%d = %.1f%% ==" % (ok_n, len(rows), 100.0 * ok_n / max(1, len(rows))))
    store.close()
    os.unlink(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
