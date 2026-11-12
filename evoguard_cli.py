#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EvoGuardLab 命令行入口。

装好之后可以直接用：

    pip install -e .
    evoguard demo          # 跑一遍完整的防御演示（会真的执行/拦截工具）
    evoguard scan "文本"    # 扫一段文本，输出命中项
    evoguard eval          # 跑全部可复现实验，打印关键数字
    evoguard test          # 跑回归测试
    evoguard config        # 打印本次生效的安全配置

设计取向：**没有依赖**。整套东西只用标准库，所以在一台装不上 torch 的
Windows 机器上也能跑出同一批数字 —— 这是可复现主张的一部分，不是偷懒。
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
EVO = os.path.join(ROOT, "ai-service", "evoguard")
if EVO not in sys.path:
    sys.path.insert(0, EVO)
if os.path.join(ROOT, "ai-service") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "ai-service"))


def _run(script, *args):
    path = os.path.join(ROOT, "scripts", script)
    return subprocess.call([sys.executable, path, *args], cwd=ROOT)


def cmd_demo(_):
    """完整防御演示：同一批攻击在五档配置下的真实执行后果。"""
    return _run("defense_demo.py")


def cmd_scan(a):
    import security
    text = a.text if a.text is not None else sys.stdin.read()
    if a.tool:
        # 给了工具名就走完整的返回值守卫（含「动作-职责不符」这一层）。
        # 只调 scan() 会漏掉职责判断 —— 那正是自然语言间接注入唯一能抓住的信号。
        r = security.guard_tool_output(a.tool, text, where=a.where)
        findings = r["findings"]
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
        print("decision=%s  executed=%s  effect=%s"
              % (r["decision"], r["executed"], r["effect"]))
    else:
        findings = security.scan(text, a.where)
        if a.json:
            print(json.dumps(findings, ensure_ascii=False, indent=2))
            return 0
    if not findings:
        print("no findings (mode=%s)" % security.mode())
        return 0
    for f in findings:
        print("[%s] %s @%s  %s" % (f["severity"], f["code"], f["where"],
                                   f.get("match", "")[:60]))
    print("\n%d finding(s). 注意：这是检测建议，不代表已拦截。" % len(findings))
    return 0


def cmd_intent(a):
    """执行前意图门禁（需要本地模型；没有模型会转 review，不会静默放行）。"""
    import security
    print(json.dumps(
        security.guard_tool_intent(a.agent, a.tool, json.loads(a.args), a.goal),
        ensure_ascii=False, indent=2))
    return 0


def cmd_eval(_):
    """跑全部可复现实验。每个脚本自己写 experiments/ 下的产物。"""
    scripts = [
        ("action_scope", "eval_action_scope.py"),
        ("chinese corpus", "eval_chinese_real.py"),
        ("external attacks", "eval_external_attacks.py"),
        ("jailbreak prompts", "eval_jailbreak_prompts.py"),
        ("labeled set", "eval_labeled.py"),
        ("ablation", "ablation_v1.py"),
    ]
    # data/external/ 被 .gitignore 排除（许可 + 体积），clean clone 上没有语料。
    # 这时不要抛 traceback：跳过依赖语料的四项，其余两项照跑，并把前置条件说清。
    need_corpus = {"eval_action_scope.py", "eval_chinese_real.py",
                   "eval_external_attacks.py", "eval_jailbreak_prompts.py"}
    if not os.path.isfile(os.path.join(ROOT, "data", "external", "injecagent",
                                       "cases.jsonl")):
        print("[i] 外部语料未导入：跳过 4 项依赖 data/external/ 的评测；")
        print("    labeled set / ablation 两项仍会跑。")
        print("    跑全量需先导入语料（见 README「复现分层」）。")
        scripts = [s for s in scripts if s[1] not in need_corpus]
    failed = []
    for name, script in scripts:
        print("== %s ==" % name)
        if _run(script) != 0:
            failed.append(script)
    if failed:
        print("\n失败：%s" % "、".join(failed))
        return 1
    return 0


def cmd_test(_):
    return subprocess.call([sys.executable, "-m", "unittest",
                            "discover", "-s", "tests", "-q"], cwd=ROOT)


def cmd_serve(a):
    app = os.path.join(ROOT, "ai-service", "api", "app.py")
    return subprocess.call([sys.executable, app, "--port", str(a.port)],
                           cwd=ROOT)


def cmd_config(_):
    import baseline
    import execution
    import security
    cfg = {
        "security": baseline.effective_security_config(),
        "security_mode": security.mode(),
        "exec": execution.build_config(
            agent_type="order",
            allowed_tools=security.allowed_tools("order"),
            write_tools={"order.settle"},
            has_approval_backend=False),
    }
    cfg["exec"] = execution.export(cfg["exec"])
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="evoguard",
        description="EvoGuardLab — 校验「拒绝是否真的发生在副作用之前」")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("demo", help="跑完整防御演示")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("scan", help="扫描一段文本")
    p.add_argument("text", nargs="?", help="要扫描的文本；省略则读 stdin")
    p.add_argument("--where", default="text", help="落点：text/user_input/tool_output")
    p.add_argument("--tool", help="工具名；给了就走完整返回值守卫（含职责判断）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("intent", help="执行前意图门禁（需本地模型）")
    p.add_argument("agent")
    p.add_argument("tool")
    p.add_argument("args", help='JSON 对象，例如 {"order_id":"A1001"}')
    p.add_argument("goal", help="用户最初的目标")
    p.set_defaults(func=cmd_intent)

    p = sub.add_parser("eval", help="跑全部可复现实验")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("test", help="跑回归测试")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("serve", help="启动本地看板")
    p.add_argument("--port", type=int, default=8770)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("config", help="打印生效的安全配置")
    p.set_defaults(func=cmd_config)

    a = ap.parse_args(argv)
    if not getattr(a, "func", None):
        ap.print_help()
        return 0
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
