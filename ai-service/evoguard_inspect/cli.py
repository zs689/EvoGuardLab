# -*- coding: utf-8 -*-
"""命令行入口。

    python -m evoguard_inspect.cli --trace <file.json> [--format otel|langsmith] [--out DIR]
    python -m evoguard_inspect.cli --trace a.json b.json --out out/

跑法示例（在 ai-service 目录下）：
    cd ai-service && python -m evoguard_inspect.cli --trace ../tests/fixtures/otel_sample.json
"""
import argparse
import sys

from . import report
from .rules import trace_rules


def main(argv=None):
    ap = argparse.ArgumentParser(prog="inspect",
                                 description="Agent 离线检查：只读轨迹，不执行 Agent")
    ap.add_argument("--trace", nargs="+", required=True, help="轨迹文件（OTLP JSON 或 LangSmith runs）")
    ap.add_argument("--format", default=None, choices=["otel", "langsmith"],
                    help="强制指定格式；不填则自动识别")
    ap.add_argument("--out", default=None, help="输出目录（默认只打印到终端）")
    ap.add_argument("--json", action="store_true", help="终端输出 JSON 而不是摘要")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        traces = report.inspect_files(args.trace, args.format)
    except Exception as e:
        print("[×] 加载失败：%s" % e)
        return 2

    if not traces:
        print("[×] 没有解析出任何轨迹。检查文件是不是空、或格式不认识。")
        return 2

    issues = trace_rules.check_all(traces)
    by_sev, by_rule = report.summarize(issues)

    if args.json:
        import json
        print(json.dumps(report.to_json(traces, issues), ensure_ascii=False, indent=2))
    else:
        print("[√] 解析 %d 条轨迹，共 %d 步" % (len(traces), sum(len(t) for t in traces)))
        print("    发现 %d 个问题：error %d / warn %d / info %d"
              % (len(issues), by_sev.get("error", 0), by_sev.get("warn", 0),
                 by_sev.get("info", 0)))
        for rid, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
            meta = trace_rules.RULES.get(rid, {})
            print("    - %-24s %2d  %s" % (rid, n, meta.get("title", "")))

    if args.out:
        j, m = report.write(args.out, traces, issues, ", ".join(args.trace))
        print("    报告：%s" % m)
        print("    明细：%s" % j)
    return 0


if __name__ == "__main__":
    sys.exit(main())
