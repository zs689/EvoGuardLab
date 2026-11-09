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
import os
import sys

from . import config, coverage, report
from .rules import registry, trace_rules
from .static import loader as static_loader
from .static import rules as static_rules

# 退出码：给 CI 用的三种结果必须能分开。
#   0 = 跑完且没有超过阈值的发现
#   2 = 输入解析失败（至少一份输入没能读成轨迹/定义）
#   3 = 跑完了，但发现数达到 --fail-on 指定的级别
SEVERITY_ORDER = {"info": 1, "warn": 2, "error": 3}


def _fail_on_triggered(by_sev, fail_on):
    if not fail_on:
        return False
    want = SEVERITY_ORDER[fail_on]
    return any(by_sev.get(s, 0) for s in SEVERITY_ORDER
               if SEVERITY_ORDER[s] >= want)


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


def run_static(args, cfg=None):
    from . import report as _report
    cfg = cfg or config.load(args.config)
    defs = []
    load_failures = []
    for p in args.agent:
        try:
            if args.split_dir and os.path.isdir(p):
                defs.extend(static_loader.load_repo_split(p))
            else:
                defs.append(static_loader.load_any(p))
        except Exception as e:
            print("[×] 加载失败 %s：%s" % (p, e))
            load_failures.append({"path": p, "error": "%s: %s" % (type(e).__name__, e)})
    if not defs:
        print("[×] 没有可分析的 Agent 定义")
        return 2
    rules = static_rules.select(groups=args.group, only=args.only, skip=args.skip)
    if not rules:
        print("[×] 规则过滤后一条都没剩下")
        return 2
    issues = []
    for d in defs:
        issues.extend(static_rules.check(d, cfg, rules))
    by_sev, by_rule, by_group = _report.summarize(issues)
    cov = coverage.analyze_static(
        defs, issues, cfg,
        static_rules.select_ids(groups=args.group, only=args.only, skip=args.skip))

    print("[√] 分析 %d 个 Agent 定义 ｜ 静态规则 %d/%d 条生效（可执行 %d 条）"
          % (len(defs), len(rules), len(static_rules.RULES), cov["runnable_rules"]))
    for d in defs:
        print("    - %-28s %s  tools=%d  prompt=%d 字  confidence=%s"
              % (d.name[:28], d.source_kind, len(d.tools),
                 len(d.system_prompt), d.confidence))
    print("    发现 %d 个问题：error %d / warn %d / info %d"
          % (len(issues), by_sev.get("error", 0), by_sev.get("warn", 0),
             by_sev.get("info", 0)))
    for rid, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        m = static_rules.RULES.get(rid, {})
        print("    - %-28s %2d  [%s] %s" % (rid, n, m.get("group", "-"),
                                            m.get("title", "")))
    print("    %s" % coverage.VERDICT_TEXT[cov["verdict"]])
    if cov["skipped_rules"]:
        print("    跳过 %d 条（输入不足）：" % cov["skipped_rules"]
              + "、".join(r["rule_id"] for r in cov["rules"] if not r["runnable"]))
    if args.out:
        j, m = _report.write_static(args.out, defs, issues, load_failures, cov)
        print("    报告：%s" % m)
        print("    明细：%s" % j)
    if load_failures:
        print("    [!] %d 个输入没能加载（结论只覆盖加载成功的对象）" % len(load_failures))
        if args.strict_input:
            return 2
    if _fail_on_triggered(by_sev, args.fail_on):
        print("    [×] 发现达到 --fail-on %s 阈值" % args.fail_on)
        return 3
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="inspect",
                                 description="Agent 离线检查：只读轨迹，不执行 Agent")
    # action="append"+nargs="*"：`--trace a --trace b` 与 `--trace a b` 都能用。
    # 只用 nargs="*" 时重复写同一个参数会覆盖前一次 —— 前面的文件被静默丢掉，
    # 报告看起来只检查了一部分输入，这正是这个工具最不该犯的错。
    ap.add_argument("--trace", action="append", nargs="*", default=None,
                    help="轨迹文件（OTLP JSON 或 LangSmith runs），可重复")
    ap.add_argument("--agent", action="append", nargs="*", default=None,
                    help="Agent 定义：JSON 文件或代码仓库目录（只读文本，不执行），可重复")
    ap.add_argument("--list-static-rules", action="store_true",
                    help="列出静态规则库后退出")
    ap.add_argument("--split-dir", action="store_true",
                    help="把 --agent 目录下的一级子目录各当一个 Agent（仓库里通常有多个）")
    ap.add_argument("--format", default=None, choices=["otel", "langsmith"])
    ap.add_argument("--config", default=None, help="配置文件（JSON），用于适配不同 Agent")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--json", action="store_true", help="终端输出 JSON")
    ap.add_argument("--list-rules", action="store_true", help="列出规则库后退出")
    ap.add_argument("--group", nargs="*", default=None,
                    help="只跑这些分组：answer tool flow resource structure")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些规则 id")
    ap.add_argument("--skip", nargs="*", default=None, help="跳过这些规则 id")
    ap.add_argument("--assurance", action="store_true",
                    help="输出三维保障报告（可靠 / 防护 / 可运行）")
    ap.add_argument("--fail-on", default=None, choices=["info", "warn", "error"],
                    help="出现该级别及以上的发现时退出码 3（给 CI 卡质量用）")
    ap.add_argument("--strict-input", action="store_true",
                    help="任一份输入解析失败就以退出码 2 结束（默认只在终端提示）")
    ap.add_argument("--policy", default=None,
                    help="CI 发布策略 JSON（配合 --assurance）：显式声明风险容忍，"
                         "把报告判定与流水线是否阻断分开保存")
    args = ap.parse_args(argv)
    args.trace = [p for group in (args.trace or []) for p in group]
    args.agent = [p for group in (args.agent or []) for p in group]

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.list_rules:
        print_rules(args.group)
        return 0

    if args.list_static_rules:
        from collections import defaultdict
        by = defaultdict(list)
        for rid, m in static_rules.RULES.items():
            by[m["group"]].append((rid, m))
        for g, title in static_rules.GROUPS.items():
            rows = sorted(by.get(g, []))
            print("\n[%s] %s（%d 条）" % (g, title, len(rows)))
            for rid, m in rows:
                print("  %-28s %-6s %s" % (rid, m["severity"], m["title"]))
        print("\n共 %d 条静态规则" % len(static_rules.RULES))
        return 0

    # ---- 静态检查分支（只读定义，不执行 Agent）----
    if args.agent:
        return run_static(args, cfg=None)

    if not args.trace:
        print("[×] 没有输入：用 --trace 指定轨迹、用 --agent 指定定义，"
              "或用 --list-rules / --list-static-rules 看规则库")
        return 2

    try:
        cfg = config.load(args.config)
    except Exception as e:
        print("[×] 配置加载失败：%s" % e)
        return 2

    traces, load_failures = report.inspect_files_safe(args.trace, args.format)
    for f in load_failures:
        print("[!] 输入解析失败：%s（%s）" % (f["path"], f["error"]))

    if not traces:
        print("[×] 没有解析出任何轨迹。检查文件是不是空、或格式不认识。")
        return 2

    rules = registry.select(groups=args.group, only=args.only, skip=args.skip)
    rule_ids = registry.select_ids(groups=args.group, only=args.only, skip=args.skip)
    if not rules:
        print("[×] 规则过滤后一条都没剩下，检查 --group/--only/--skip")
        return 2

    issues = trace_rules.check_all(traces, cfg, rules)
    by_sev, by_rule, by_group = report.summarize(issues)
    cov = coverage.analyze(traces, issues, cfg, rule_ids)

    if args.assurance:
        from . import assurance
        data = assurance.build(traces, issues, cfg, cov)
        text = assurance.render(data)
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            j = os.path.join(args.out, "assurance.json")
            m = os.path.join(args.out, "assurance.md")
            with open(j, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            with open(m, "w", encoding="utf-8") as f:
                f.write(text)
            print("报告：%s" % m)
            print("明细：%s" % j)
        else:
            print(text)
        print("[保障] 总判定：%s" % assurance.VERDICT_CN[data["verdict"]])
        if args.policy:
            from . import ci_policy
            policy = ci_policy.load_policy(args.policy)
            decision = ci_policy.evaluate(data, policy, len(load_failures))
            if args.out:
                with open(os.path.join(args.out, "ci_decision.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(decision, f, ensure_ascii=False, indent=2)
                print("CI 决策：%s" % os.path.join(args.out, "ci_decision.json"))
            print("[CI] 报告判定 %s ｜ 流水线 %s ｜ 退出码 %d"
                  % (decision["report_verdict"], decision["ci_action"],
                     decision["exit_code"]))
            for r in decision["reasons"]:
                print("   - %s" % r)
            if load_failures and args.strict_input:
                return 2
            return decision["exit_code"]
        if load_failures and args.strict_input:
            return 2
        return 3 if data["verdict"] in (assurance.FAIL,) else 0

    if args.json:
        print(json.dumps(report.to_json(traces, issues, cov, load_failures),
                         ensure_ascii=False, indent=2))
    else:
        print("[√] 解析 %d 条轨迹，共 %d 步 ｜ 规则 %d/%d 条生效（可执行 %d 条）"
              % (len(traces), sum(len(t) for t in traces),
                 len(rules), len(registry.RULES), cov["runnable_rules"]))
        print("    发现 %d 个问题：error %d / warn %d / info %d"
              % (len(issues), by_sev.get("error", 0), by_sev.get("warn", 0),
                 by_sev.get("info", 0)))
        for rid, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
            m = registry.RULES.get(rid, {})
            print("    - %-26s %2d  [%s] %s" % (rid, n, m.get("group", "-"),
                                                m.get("title", "")))
        print("    %s" % coverage.VERDICT_TEXT[cov["verdict"]])
        if cov["skipped_rules"]:
            print("    跳过 %d 条（输入不足）：" % cov["skipped_rules"]
                  + "、".join(r["rule_id"] for r in cov["rules"] if not r["runnable"]))

    if args.out:
        j, m = report.write(args.out, traces, issues, ", ".join(args.trace),
                            cov, load_failures)
        print("    报告：%s" % m)
        print("    明细：%s" % j)
    if load_failures and args.strict_input:
        print("[×] --strict-input：有 %d 份输入没能解析" % len(load_failures))
        return 2
    if _fail_on_triggered(by_sev, args.fail_on):
        print("[×] 发现达到 --fail-on %s 阈值" % args.fail_on)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
