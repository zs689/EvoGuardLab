# -*- coding: utf-8 -*-
"""汇总官方 AgentDojo harness 的本地小规模运行结果。

官方 CLI：`python -m agentdojo.scripts.benchmark --model LOCAL ...`
后端 `LOCAL_LLM_PORT=11434`（本机 Ollama / qwen3:4b），包隔离装在
`_sandbox/agentdojo_pkg`（不污染项目依赖）。

**这是小规模接通验证，不是官方榜单成绩**：4 个环境各 1 个 user task +
1 个 injection task、attack=direct；官方榜单是 97 用户任务 + 629 安全用例、
且用的是闭源前沿模型。这里只证明链路能跑通并留下可核对的数字。
"""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "experiments", "agentdojo_official_v1", "runs")
OUT = os.path.join(ROOT, "experiments", "agentdojo_official_v1", "report.md")


def collect():
    rows = []
    for path in sorted(glob.glob(os.path.join(RUNS, "**", "*.json"),
                                 recursive=True)):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        rows.append({
            "file": os.path.relpath(path, RUNS).replace("\\", "/"),
            "suite": d.get("suite_name"),
            "user_task": d.get("user_task_id"),
            "injection_task": d.get("injection_task_id"),
            "attack": d.get("attack_type"),
            "utility": d.get("utility"),
            "security": d.get("security"),
            "error": d.get("error"),
            "duration": d.get("duration"),
            "version": d.get("benchmark_version"),
            "package": d.get("agentdojo_package_version"),
        })
    return rows


def _md(rows):
    L = ["# 官方 AgentDojo harness：本地小规模跑通", "",
         "> **不是官方榜单成绩**：4 个环境各 1 个 user task + 1 个 injection task、"
         "`--attack direct`、后端是本机 Ollama `qwen3:4b`。",
         "> 官方榜单是 97 用户任务 + 629 安全用例、闭源前沿模型；"
         "两边环境、模型、样本量都不同，**不能跨环境比大小**。",
         "", "## 逐条结果（来自官方 CLI 写出的 json）", "",
         "| 环境 | user task | 攻击 | 注入任务 | utility | security | 错误 |",
         "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | `%s` | %s | %s | %s | %s | %s |"
                 % (r["suite"], r["user_task"], r["attack"] or "none",
                    r["injection_task"] or "—",
                    "—" if r["utility"] is None else r["utility"],
                    "—" if r["security"] is None else r["security"],
                    (str(r["error"])[:60] if r["error"] else "—")))
    L += ["", "## 怎么读", "",
          "- `injection_task_X/none/none.json` 是官方 harness 顺带跑的"
          "「把注入任务当用户任务」基线；`user_task_0/direct/...` 才是带攻击的评测。",
          "- 本地 4B 模型不按 AgentDojo 期望的工具调用格式作答，"
          "多数环境 utility 为 0；security 高只说明**它没照做注入**，"
          "不是「防护有效」—— 一个什么都没干成的 Agent 天然不会被攻击成功。",
          "- 官方 harness 已接通（安装、4 个 suite、本地后端、结果落盘），"
          "要拿可比成绩需要换官方评测模型并跑全量任务集。",
          "", "## 边界", "",
          "- 包 `agentdojo` 隔离装在 `_sandbox/agentdojo_pkg`（MIT），"
          "不进仓库依赖；运行结果放在 `experiments/agentdojo_official_v1/runs/`。",
          "- 没跑全量、没跑全部攻击变体、没跑防御变体。", ""]
    return "\n".join(L)


def main():
    rows = collect()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(_md(rows))
    print("[√] 产物：%s ｜ %d 条官方结果" % (OUT, len(rows)))
    return rows


if __name__ == "__main__":
    main()
