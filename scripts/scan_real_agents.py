# -*- coding: utf-8 -*-
"""用静态检查器扫真实开源 Agent 框架，产出可复现实测数据。

**这份脚本验证的是「泛用性」，不是「抓框架 bug」**：
  - 被扫对象是各框架的官方示例 / cookbook，不是框架库源码本身；
  - 从源码启发式提取的定义 confidence 一律 low，结论只是线索；
  - 教学示例为了简洁通常不写 timeout / 审批 / 预算，命中这些规则
    **不等于框架有缺陷**。报告里会明确写这一点。

所以真正有价值的指标是：
  1. 提取器能不能从不同框架的代码风格里拿到 prompt / tools（覆盖能力）；
  2. 有没有规则命中率接近 100%（那就是噪音，不是发现）；
  3. 检查器会不会崩、会不会误把多个 Agent 混成一个。

安全：只读文本，不执行、不 import、不安装任何被检代码。
跑法：python scripts/scan_real_agents.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import config                      # noqa: E402
from evoguard_inspect.static import loader, model, rules as SR  # noqa: E402

S = "_sandbox"
SPECS = [
    ("OpenAI Agents SDK", "OpenAI · 大厂", S + "/openai-agents-python/openai-agents-python-main/examples", "split", 20),
    ("OpenAI Swarm", "OpenAI · 大厂", S + "/swarm/swarm-main/examples", "split", 20),
    ("Google ADK", "Google · 大厂", S + "/adk/adk-python-main/contributing/samples", "split", 20),
    # LangGraph 的顶层 examples/ 只有 4 个通用脚本（无字面量 Agent 定义），
    # 真正的图示例在 libs/cli/examples/graphs/。按文件拆，别按目录合并。
    ("LangGraph", "LangChain · 主流框架", S + "/langgraph/langgraph-main/libs/cli/examples", "file", 12),
    ("smolagents", "Hugging Face · 大厂", S + "/smolagents/smolagents-main/examples", "split", 20),
    ("Agno", "Agno · 小厂", S + "/agno/agno-main/cookbook", "split", 20),
    ("Microsoft AutoGen", "Microsoft · 大厂", S + "/autogen_mini", "file", 12),
    ("PydanticAI", "Pydantic · 小厂", S + "/pydanticai_mini", "file", 12),
    ("CrewAI", "CrewAI · 小厂", S + "/crewai_mini", "file", 12),
]
OUT = os.path.join(ROOT, "experiments", "inspect", "real_agents_scan.json")
MAX_DEFS = 20


def defs_from_files(root, cap):
    """每个文件当一个 Agent 定义：mini 仓库只有少量代表性文件，按文件拆最准。"""
    out = []
    for rel, text in loader._iter_texts(root):
        if len(out) >= cap:
            break
        d = model.from_repo_files([(rel, text)], source=os.path.join(root, rel))
        d.name = os.path.basename(rel)
        d.confidence = "low"
        if d.system_prompt or d.tools:
            out.append(d)
    return out


def main():
    cfg = config.DEFAULT
    results, noisy = [], []
    for name, vendor, rel, mode, cap in SPECS:
        root = os.path.join(ROOT, rel)
        if not os.path.isdir(root):
            results.append({"name": name, "vendor": vendor, "status": "missing",
                            "path": rel})
            print("[×] %-22s 目录不存在，跳过：%s" % (name, rel))
            continue
        try:
            defs = (loader.load_repo_split(root) if mode == "split"
                    else defs_from_files(root, cap))
        except Exception as e:
            results.append({"name": name, "vendor": vendor, "status": "error",
                            "error": str(e)})
            print("[×] %-22s 加载失败：%s" % (name, e))
            continue
        defs = [d for d in defs if d.system_prompt or d.tools][:MAX_DEFS]
        if not defs:
            results.append({"name": name, "vendor": vendor, "status": "no_definition",
                            "path": rel})
            print("[!] %-22s 没提取到任何 Agent 定义" % name)
            continue

        issues = []
        for d in defs:
            try:
                issues.extend(SR.check(d, cfg) or [])
            except Exception as e:
                issues.append({"rule_id": "scan_crashed", "severity": "info",
                               "detail": str(e)})
        by_rule, by_sev = {}, {}
        for i in issues:
            by_rule[i["rule_id"]] = by_rule.get(i["rule_id"], 0) + 1
            by_sev[i.get("severity", "info")] = by_sev.get(i.get("severity", "info"), 0) + 1
        # 命中率 ≥ 80% 的规则：大概率是噪音，单独列出来提醒人工看
        high = sorted([r for r, n in by_rule.items() if n >= max(1, int(len(defs) * 0.8))],
                      key=lambda r: -by_rule[r])
        if high:
            noisy.append((name, high))

        rec = {
            "name": name, "vendor": vendor, "status": "ok", "mode": mode,
            "path": rel, "defs": len(defs),
            "with_prompt": sum(1 for d in defs if d.system_prompt),
            "with_tools": sum(1 for d in defs if d.tools),
            "tools_total": sum(len(d.tools) for d in defs),
            "prompt_chars": sum(len(d.system_prompt) for d in defs),
            "confidence": sorted({d.confidence for d in defs}),
            "issues": len(issues),
            "by_severity": by_sev,
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "high_hit_rules": high,
            "sample_defs": [d.name for d in defs[:6]],
        }
        results.append(rec)
        print("[√] %-22s %-24s 定义 %2d (prompt %2d / tools %2d) 问题 %3d  高命中规则 %s"
              % (name, vendor, rec["defs"], rec["with_prompt"], rec["with_tools"],
                 rec["issues"], ",".join(high[:2]) or "-"))

    total_defs = sum(r.get("defs", 0) for r in results)
    total_issues = sum(r.get("issues", 0) for r in results)
    payload = {
        "version": 1,
        "purpose": "验证静态检查器对多种真实框架示例的适配能力；"
                   "结论是线索，不代表框架缺陷（confidence=low）",
        "safety": "只读文本，未执行/未安装任何被检代码",
        "rules_available": len(SR.RULES),
        "frameworks": len([r for r in results if r["status"] == "ok"]),
        "total_defs": total_defs,
        "total_issues": total_issues,
        "results": results,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n合计：%d 个框架、%d 个 Agent 定义、%d 条线索 → %s"
          % (payload["frameworks"], total_defs, total_issues, os.path.relpath(OUT, ROOT)))
    if noisy:
        print("\n[!] 以下规则的命中率接近 100%，需要当作噪音复核（不是发现）：")
        for n, rules in noisy:
            print("    %-22s %s" % (n, ", ".join(rules)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
