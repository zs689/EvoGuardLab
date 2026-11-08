# -*- coding: utf-8 -*-
"""命令行入口。

    python -m evoguard_inspect.cli --trace <file.json> [更多文件...]
    python -m evoguard_inspect.cli --list-rules            # 列出规则库
    python -m evoguard_inspect.cli --trace x.json --config inspect.json
    python -m evoguard_inspect.cli --trace x.json --group tool flow   # 只看某几组
    python -m evoguard_inspect.cli --trace x.json --only pii_leak --skip loop_detected

跑法示例（在 ai-service 目录下）：
    cd ai-service && python -m evoguard_inspect.cli --trace ../tests/fixtures/trace_synthetic_bad.json
"""
import argparse
import json
import sys

from . import config, report
from .rules import registry, trace_rules


def print_rules(groups=None):
    from collections import defaultdict
    by = defaultdict(list)
    for rid, m in registry.RULES.items():
        by[m["group"]].append((rid, m))
    for g, title in registry.GROUPS.items():
        if groups and g not in groups:
            continue
        rows = sorted(by.get(g, []))
        print("\n[%s] %s（%d 条）" % (g, title, len(rows)))
        for rid, m in rows:
            print("  %-26s %-6s %s" % (rid, m["severity"], m["title"]))
    print("\n共 %d 条规则" % len(registry.RULES))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="inspect",
                                 description="Agent 离线检查：只读轨迹，不执行 Agent")
    ap.add_argument("--trace", nargs="*", default=[],
                    help="轨迹文件（OTLP JSON 或 LangSmith runs）")
    ap.add_argument("--format", default=None, choices=["otel", "langsmith"])
    ap.add_argument("--config", default=None, help="配置文件（JSON），用于适配不同 Agent")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--json", action="store_true", help="终端输出 JSON")
    ap.add_argument("--list-rules", action="store_true", help="列出规则库后退出")
    ap.add_argument("--group", nargs="*", default=None,
                    help="只跑这些分组：answer tool flow resource structure")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些规则 id")
    ap.add_argument("--skip", nargs="*", default=None, help="跳过这些规则 id")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.list_rules:
        print_rules(args.group)
        return 0

    if not args.trace:
        print("[×] 没有输入：用 --trace 指定轨迹文件，或用 --list-rules 看规则库")
        return 2

    try:
        cfg = config.load(args.config)
    except Exception as e:
        print("[×] 配置加载失败：%s" % e)
        return 2

    try:
        traces = report.inspect_files(args.trace, args.format)
    except Exception as e:
        print("[×] 加载失败：%s" % e)
        return 2

    if not traces:
        print("[×] 没有解析出任何轨迹。检查文件是不是空、或格式不认识。")
        return 2

    rules = registry.select(groups=args.group, only=args.only, skip=args.skip)
    if not rules:
        print("[×] 规则过滤后一条都没剩下，检查 --group/--only/--skip")
        return 2

    issues = trace_rules.check_all(traces, cfg, rules)
    by_sev, by_rule, by_group = report.summarize(issues)

    if args.json:
        print(json.dumps(report.to_json(traces, issues), ensure_ascii=False, indent=2))
    else:
        print("[√] 解析 %d 条轨迹，共 %d 步 ｜ 规则 %d/%d 条生效"
              % (len(traces), sum(len(t) for t in traces),
                 len(rules), len(registry.RULES)))
        print("    发现 %d 个问题：error %d / warn %d / info %d"
              % (len(issues), by_sev.get("error", 0), by_sev.get("warn", 0),
                 by_sev.get("info", 0)))
        for rid, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
            m = registry.RULES.get(rid, {})
            print("    - %-26s %2d  [%s] %s" % (rid, n, m.get("group", "-"),
                                                m.get("title", "")))

    if args.out:
        j, m = report.write(args.out, traces, issues, ", ".join(args.trace))
        print("    报告：%s" % m)
        print("    明细：%s" % j)
    return 0


if __name__ == "__main__":
    sys.exit(main())
