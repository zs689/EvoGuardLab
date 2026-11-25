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
import base64
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
EVO = os.path.join(ROOT, "ai-service", "evoguard")
IS_CHECKOUT = os.path.isdir(EVO)


def _installed_asset_root():
    """wheel 安装后，data-files 落在 sys.prefix/share/evoguardlab。"""
    p = os.path.join(sys.prefix, "share", "evoguardlab")
    return p if os.path.isdir(p) else ""


def _asset_root():
    return ROOT if IS_CHECKOUT else _installed_asset_root()


def _add_evoguard_path():
    """checkout 与 wheel 两种布局都要能让 `import security` 找到顶层模块。

    以前只往 sys.path 塞 `ROOT/ai-service/evoguard`，装到 site-packages
    后那个目录根本不存在 —— `config` / `scan` 直接 ModuleNotFoundError。
    wheel 场景改为把已安装的 `evoguard` 包目录加进来（EG-004）。
    """
    if IS_CHECKOUT:
        for p in (EVO, os.path.dirname(EVO)):
            if p not in sys.path:
                sys.path.insert(0, p)
        return
    try:
        import evoguard as _pkg
        p = os.path.dirname(_pkg.__file__)
        if p not in sys.path:
            sys.path.insert(0, p)
    except ImportError:
        pass


_add_evoguard_path()


def _run(script, *args):
    base = _asset_root()
    if not base:
        print("[×] 找不到运行资产目录。请用源码 checkout，或重新安装 wheel。")
        return 2
    path = os.path.join(base, "scripts", script)
    if not os.path.isfile(path):
        print("[×] `%s` 是源码 checkout 专用命令（需要 tests/ 或 experiments/ 等"
              "仓库资产，wheel 不分发它们）。" % script)
        print("    从仓库根目录运行：python evoguard_cli.py %s"
              % os.path.splitext(script)[0])
        return 2
    cwd = ROOT if IS_CHECKOUT else os.getcwd()
    return subprocess.call([sys.executable, path, *args], cwd=cwd)


def _checkout_only(name):
    print("[×] `%s` 只在源码 checkout 下可用：它需要仓库内的脚本/测试/实验产物，"
          "这些不在 wheel 分发清单里。" % name)
    print("    从仓库根目录运行：python evoguard_cli.py %s" % name)
    return 2


def cmd_demo(_):
    """完整防御演示：同一批攻击在五档配置下的真实执行后果。

    wheel 场景把产物写到当前目录下的隔离文件夹，不往只读的 site-packages
    或用户的仓库里塞东西（EG-004：用用户数据目录保存可变文件）。
    """
    if IS_CHECKOUT:
        return _run("defense_demo.py")
    return _run("defense_demo.py", "--out",
                os.path.join(os.getcwd(), "evoguard_demo_out"))


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


def cmd_inspect(a):
    """离线检查 Agent 定义/轨迹；--format sarif 输出 SARIF 2.1.0。

    用显式声明的参数转发，而不是 argparse.REMAINDER —— 后者在嵌套
    subparsers 下会把选项留给它自己的 parser，wheel 安装后实测报
    "unrecognized arguments"（源码直调 IC.main 的单测发现不了）。
    """
    import evoguard_inspect.cli as IC
    argv = []
    for attr, flag in (("trace", "--trace"), ("agent", "--agent"),
                       ("format", "--format"), ("output", "--output"),
                       ("config", "--config")):
        v = getattr(a, attr, None)
        if v:
            argv += [flag, v]
    if getattr(a, "list_static_rules", False):
        argv.append("--list-static-rules")
    if getattr(a, "out", None):
        argv += ["--out", a.out]
    return IC.main(argv)


def cmd_mcp_proxy(_a):
    """PROD-02-T2：MCP stdio 安全代理。"""
    import mcp_proxy as MP
    return MP.serve_stdio()


def cmd_react(a):
    """INT-01-T2：CLI 驱动真实多轮 ReAct Agent。"""
    import autonomous_agent as AA
    import tool_registry as TR
    reg = TR.ToolRegistry()
    for spec in (a.tool or []):
        try:
            name, _, schema = spec.partition("=")
            reg.from_json_schema(name, json.loads(schema) if schema else {},
                                 source="cli")
        except ValueError:
            print("工具定义不合法：%r" % spec)
            return 2
    if not (a.tool or []):
        reg = TR.REGISTRY
    calls = []

    def executor(tool, args):
        calls.append({"tool": tool, "args": args})
        return {"ok": True, "stub": True}

    mem_store = None
    if not a.no_memory:
        import core
        mem_store = core.Store(a.memory_db or
                               os.path.join(tempfile.gettempdir(),
                                            "evoguard-memory.db"))
    try:
        out = AA.ReActAgent(agent_type=a.agent_type, goal=a.goal, registry=reg,
                            executor=executor if a.executor == "stub" else None,
                            max_steps=a.max_steps, memory_store=mem_store,
                            memory_task_type=a.memory_task_type).run()
    finally:
        if mem_store is not None:
            mem_store.close()
    if a.json:
        print(json.dumps({"answer": out["answer"],
                          "stopped_by": out["stopped_by"],
                          "contract": out["contract"],
                          "calls": calls}, ensure_ascii=False, indent=2))
    else:
        print("answer=%s" % out["answer"])
        print("stopped_by=%s" % out["stopped_by"])
        print("executed=%s effect=%s" % (out["contract"]["executed"],
                                         out["contract"]["effect"]))
        for c in calls:
            print("  called: %s %s" % (c["tool"], c["args"]))
    return 0


def cmd_synth(a):
    """INT-01-T2：从轨迹 JSON 合成候选 Skill 并跑沙箱门禁（不自动晋升）。"""
    import core
    import skill_synthesis as SS
    with open(a.trace, encoding="utf-8") as f:
        data = json.load(f)
    steps = data.get("steps") if isinstance(data, dict) else data
    import tempfile
    # core.Store 需要真实文件路径（:memory: 时 dirname 为空会 makedirs('') 失败）
    db = a.db or tempfile.mktemp(prefix="evo-synth-", suffix=".db")
    store = core.Store(db)
    try:
        out = SS.synthesize(store, steps, task_type=a.task_type)
    finally:
        store.close()
    if a.json:
        print(json.dumps({"diagnosis": out["diagnosis"],
                          "accepted": out["accepted"],
                          "skill_id": (out["skill"] or {}).get("id")},
                         ensure_ascii=False, indent=2, default=str))
    else:
        d = out["diagnosis"]
        print("root_cause=%s first_bad_step=%s"
              % (d.get("root_cause"), d.get("first_bad_step")))
        print("accepted=%s" % out["accepted"].get("accepted"))
        if not out["accepted"].get("accepted"):
            print("errors=%s" % out["accepted"].get("errors"))
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
    if not IS_CHECKOUT:
        return _checkout_only("eval")
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
    if not IS_CHECKOUT:
        return _checkout_only("test")
    return subprocess.call([sys.executable, "-m", "unittest",
                            "discover", "-s", "tests", "-q"], cwd=ROOT)


def cmd_serve(a):
    asset = _asset_root()
    if IS_CHECKOUT:
        app = os.path.join(ROOT, "ai-service", "api", "app.py")
        return subprocess.call([sys.executable, app, "--port", str(a.port)],
                               cwd=ROOT)
    app = os.path.join(asset, "api", "app.py")
    if not asset or not os.path.isfile(app):
        print("[×] wheel 里缺少 api/app.py，无法启动看板。请重新安装。")
        return 2
    # wheel 场景显式把只读资产与可写数据目录分开：源码树只读，
    # 业务库写到当前工作目录（审查 EG-011：可变文件不进安装目录）。
    env = dict(os.environ)
    data = os.path.join(os.getcwd(), "evoguard_data")
    env["AGENTLAB_ASSET_ROOT"] = asset
    env["AGENTLAB_ROOT"] = asset
    env["AGENTLAB_DATA_ROOT"] = data
    env["AGENTLAB_DB"] = os.path.join(data, "evoguard.db")
    return subprocess.call([sys.executable, app, "--port", str(a.port)],
                           cwd=os.getcwd(), env=env)


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
    # 主动说出「当前配置下还缺什么」：只给键名、后果与建议，不含任何值。
    # 让人跑一次 config 就看到风险，而不是读文档才知道某个开关干什么。
    cfg["gaps"] = baseline.describe_gaps()
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_receipt(a):
    """离线验签一条 Action Receipt：未签名/篡改都必须非零退出。"""
    import receipt as _rc
    try:
        raw = a.file_or_token
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8") as f:
                r = json.load(f)
        else:
            pad = "=" * (-len(raw) % 4)
            r = json.loads(base64.urlsafe_b64decode(raw + pad).decode("utf-8"))
    except Exception as exc:                                # noqa: BLE001
        print("无法解析回执：%s" % exc)
        return 2
    v = _rc.verify(r, key=a.key or None)
    print("status=%s valid=%s reason=%s"
          % (v["status"], v["valid"], v["reason"]))
    return 0 if v["valid"] else 1


def cmd_scorecard(a):
    """分数是「下一轮先补哪」的排序依据，不是认证。--min 用于回归守门：
    这一轮比上一轮低了就非零退出，避免在没有任何人注意的情况下退步。

    wheel 场景不适用：scorecard 读的是仓库里的实验产物与文档，
    装好的包里没有这些证据，跑出来只会是一个没有意义的低分。
    """
    if not IS_CHECKOUT:
        return _checkout_only("scorecard")
    from enterprise import scorecard
    report = scorecard.evaluate(ROOT)
    text = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            if a.json else scorecard.render_markdown(report))
    if a.out:
        out = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
        d = os.path.dirname(out)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        print("[√] %s（总分 %s/100）" % (out, report["total"]))
    else:
        # 不用 print：render_markdown 已经以换行结尾，print 再补一个就成了
        # 多一个空行 —— stdout 和 --out 写出同一份报告会差一个字节。
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    if a.min is not None and report["total"] < a.min:
        print("\n[×] 总分 %s 低于守门线 %s：本轮相对基线退步了。"
              % (report["total"], a.min))
        return 1
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

    p = sub.add_parser("inspect",
                       help="离线检查 Agent 定义/轨迹（支持 --format sarif）")
    p.add_argument("--agent", help="Agent 定义文件或目录")
    p.add_argument("--trace", help="轨迹文件")
    p.add_argument("--format", help="输出/输入格式：sarif|otel|langsmith")
    p.add_argument("--output", help="输出文件路径（配合 --format sarif）")
    p.add_argument("--config", help="配置 JSON")
    p.add_argument("--out", help="报告输出目录")
    p.add_argument("--list-static-rules", action="store_true",
                   help="列出静态规则后退出")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("mcp-proxy",
                       help="MCP stdio 安全代理（tools/call 强制门禁）")
    p.set_defaults(func=cmd_mcp_proxy)

    p = sub.add_parser("react", help="多轮 ReAct Agent（INT-01 主路径）")
    p.add_argument("goal", help="任务目标")
    p.add_argument("--agent-type", default="react")
    p.add_argument("--tool", action="append",
                   help='工具定义 name={"type":"object",...}，可多次')
    p.add_argument("--executor", default="none", choices=["none", "stub"],
                   help="none=不执行真实副作用；stub=只读桩")
    p.add_argument("--max-steps", type=int, default=4)
    p.add_argument("--no-memory", action="store_true",
                   help="禁用跨任务记忆注入")
    p.add_argument("--memory-db", default="", help="经验库路径")
    p.add_argument("--memory-task-type", default="", help="任务类型（检索用）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_react)

    p = sub.add_parser("synth", help="从轨迹合成候选 Skill（INT-01 主路径）")
    p.add_argument("trace", help="轨迹 JSON 文件（含 steps 数组）")
    p.add_argument("--task-type", default="newdomain")
    p.add_argument("--db", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_synth)

    p = sub.add_parser("intent", help="执行前意图门禁（需本地模型）")
    p.add_argument("agent")
    p.add_argument("tool")
    p.add_argument("args", help='JSON 对象，例如 {"order_id":"A1001"}')
    p.add_argument("goal", help="用户最初的目标")
    p.set_defaults(func=cmd_intent)

    p = sub.add_parser("eval", help="跑全部可复现实验 [checkout-only]")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("test", help="跑回归测试 [checkout-only]")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("serve", help="启动本地看板")
    p.add_argument("--port", type=int, default=8770)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("config", help="打印生效的安全配置")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("verify-receipt",
                       help="离线验证 Action Receipt（签名/未签名/篡改）")
    p.add_argument("file_or_token", help="回执 JSON 文件或 base64url token")
    p.add_argument("--key", default="", help="验签密钥（默认读环境变量）")
    p.set_defaults(func=cmd_verify_receipt)

    p = sub.add_parser("scorecard",
                       help="企业就绪度自评（打分 + 缺口清单）[checkout-only]")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--out", default="", help="写入文件（相对路径按仓库根解析）")
    p.add_argument("--min", type=float, default=None,
                   help="守门线：总分低于它则非零退出")
    p.set_defaults(func=cmd_scorecard)

    a = ap.parse_args(argv)
    if not getattr(a, "func", None):
        ap.print_help()
        return 0
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
