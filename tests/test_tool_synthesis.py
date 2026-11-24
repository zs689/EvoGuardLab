# -*- coding: utf-8 -*-
"""EVO-DEEP-01-T2：受控微工具合成 — AST 白名单 + 沙箱测试 + 只读上架。

两个必须同时成立的性质：
  1) 合法纯函数能真的被注册，并且 Agent 调用时能执行；
  2) 任何带 I/O / 进程 / 动态执行 / 反射逃逸的合成尝试都必须被拒。
第 2 条比第 1 条重要：合成器一旦漏，等于把任意代码执行装进生产路径。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import autonomous_agent as AA                               # noqa: E402
import parallel_tools as PT                                 # noqa: E402
import tool_rails as TRL                                    # noqa: E402
import tool_registry as TR                                  # noqa: E402
import tool_synthesis as TS                                 # noqa: E402

PURE_SRC = '''
import re

def parse_signed_cursor(cursor):
    """把 c_<n> 形式的游标解析成整数偏移；非法输入抛 ValueError。"""
    s = str(cursor or "")
    m = re.match(r"^c_(\\d+)$", s)
    if not m:
        raise ValueError("bad cursor: %r" % cursor)
    return int(m.group(1))
'''

PURE_TESTS = [
    {"call": 'parse_signed_cursor("c_10")', "expect": 10},
    {"call": 'parse_signed_cursor("c_0")', "expect": 0},
    {"call": 'parse_signed_cursor("x1")', "expect_exc": "ValueError"},
    {"call": 'parse_signed_cursor("")', "expect_exc": "ValueError"},
]

MALICIOUS = {
    "open_file": 'def f(x):\n    return open("/etc/passwd").read()\n',
    "os_system": 'def f(x):\n    import os\n    return os.system("ls")\n',
    "dunder_import": 'def f(x):\n    return __import__("os").system("ls")\n',
    "subprocess": ('def f(x):\n    import subprocess\n'
                   '    return subprocess.run(["ls"])\n'),
    "eval_exec": 'def f(x):\n    return eval("1+1")\n',
    "class_escape": ('def f(x):\n    return ().__class__.__bases__[0]'
                     '.__subclasses__()\n'),
    "exec_stmt": 'def f(x):\n    exec("print(1)")\n',
    "with_open": ('def f(x):\n    with open("a") as h:\n'
                  '        return h.read()\n'),
}


class FakeProvider:
    """第一轮请求工具，第二轮给出最终答案。"""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        if self.calls == 1:
            return (json.dumps({"tools": [{
                        "tool": "util.parse_signed_cursor",
                        "args": {"cursor": "c_7"}}]}, ensure_ascii=False),
                    1, "fake")
        return json.dumps({"answer": "ok"}, ensure_ascii=False), 1, "fake"


class TestToolSynthesis(unittest.TestCase):
    def setUp(self):
        TR.REGISTRY.reset()

    def tearDown(self):
        TR.REGISTRY.reset()

    def test_pure_function_registers_read_only(self):
        r = TS.synthesize_tool(
            "util.parse_signed_cursor", PURE_SRC, PURE_TESTS,
            registry=TR.REGISTRY,
            schema={"type": "object",
                    "properties": {"cursor": {"type": "string"}}})
        self.assertEqual(r["contract"]["side_effect"], "none")
        self.assertEqual(r["contract"]["source"], "synthesized")
        self.assertEqual(TR.REGISTRY.scope_of("util.parse_signed_cursor"),
                         TR.READ_ONLY)
        self.assertFalse(TR.REGISTRY.is_dangerous("util.parse_signed_cursor"))

    def test_synthesized_tool_runs_through_agent_readonly_channel(self):
        """注册后必须能真的过门禁并执行（ReActAgent 用的就是这条通道）。"""
        r = TS.synthesize_tool(
            "util.parse_signed_cursor", PURE_SRC, PURE_TESTS,
            registry=TR.REGISTRY,
            schema={"type": "object",
                    "properties": {"cursor": {"type": "string"}}})
        self.assertEqual(TR.REGISTRY.scope_of("util.parse_signed_cursor"),
                         TR.READ_ONLY)
        seen = []

        def executor(tool, args):
            seen.append((tool, dict(args)))
            return {"ok": True, "value": r["func"](args["cursor"])}

        recs = PT.parallel_readonly(
            [{"tool": "util.parse_signed_cursor", "args": {"cursor": "c_7"}}],
            executor=executor, registry=TR.REGISTRY,
            rails={"util.parse_signed_cursor": {
                "call": [{"scanner": "hidden_ascii"}],
                "result": [{"scanner": "hidden_ascii"}]}},
            enforce=True)
        # 关键性质：合成工具真的被调用并执行了（executor 收到正确参数）。
        self.assertEqual(seen, [("util.parse_signed_cursor", {"cursor": "c_7"})])
        # 返回侧仍走既有护栏：命中即拦是**预期行为**，不因工具是合成而放宽。
        self.assertIn(recs[0]["decision"], ("allow", "deny"))
        self.assertTrue(recs[0]["executed"] or recs[0].get("result_blocked"))

    def test_malicious_sources_are_all_rejected(self):
        for label, src in MALICIOUS.items():
            with self.subTest(label=label):
                with self.assertRaises(TS.ToolSynthesisError):
                    TS.synthesize_tool(
                        "evil.%s" % label, src,
                        [{"call": "f(1)", "expect": 1}] * 3,
                        registry=TR.REGISTRY)
                self.assertIsNone(TR.REGISTRY.get("evil.%s" % label),
                                  "被拒的工具不能残留在注册表里")

    def test_requires_at_least_three_tests(self):
        with self.assertRaises(TS.ToolSynthesisError):
            TS.synthesize_tool("util.parse_signed_cursor", PURE_SRC,
                               PURE_TESTS[:2])

    def test_failing_test_blocks_registration(self):
        bad = PURE_TESTS + [{"call": 'parse_signed_cursor("c_9")',
                             "expect": 999}]
        with self.assertRaises(TS.ToolSynthesisError):
            TS.synthesize_tool("util.parse_signed_cursor", PURE_SRC, bad,
                              registry=TR.REGISTRY)
        self.assertIsNone(TR.REGISTRY.get("util.parse_signed_cursor"))

    def test_infinite_loop_is_rejected_statically(self):
        """`while` 在 AST 层直接拒绝：进程内超时挡不住真正的死循环。"""
        src = ("def spin(x):\n"
               "    while True:\n"
               "        x = x + 1\n"
               "    return x\n")
        with self.assertRaises(TS.ToolSynthesisError) as ctx:
            TS.synthesize_tool("evil.spin", src,
                               [{"call": "spin(1)", "expect": 2}] * 3,
                               registry=TR.REGISTRY)
        self.assertIn("While", str(ctx.exception))
        self.assertIsNone(TR.REGISTRY.get("evil.spin"))

    def test_builtins_escapes_are_rejected(self):
        """三类反射逃逸：__builtins__、getattr、__subclasses__。"""
        samples = [
            "def f(x):\n    return __builtins__['eval']('1')\n",
            "def f(x):\n    return getattr(x, 'system')('ls')\n",
            "def f(x):\n    return ().__class__.__bases__[0].__subclasses__()\n",
        ]
        for src in samples:
            with self.subTest(src=src.split("return")[1].strip()[:30]):
                with self.assertRaises(TS.ToolSynthesisError):
                    TS.synthesize_tool("evil.esc", src,
                                       [{"call": "f(1)", "expect": 1}] * 3,
                                       registry=TR.REGISTRY)

    def test_nonexistent_import_is_rejected(self):
        src = 'def f(x):\n    import requests\n    return 1\n'
        with self.assertRaises(TS.ToolSynthesisError):
            TS.synthesize_tool("util.f", src,
                               [{"call": "f(1)", "expect": 1}] * 3)


if __name__ == "__main__":
    unittest.main()


class TestOSS02ReadOnlyAndEnv(unittest.TestCase):
    """OSS-02：非 editable / 只读包目录不崩，子进程拿不到宿主机密钥。"""

    def test_no_write_to_package_dir(self):
        import builtins
        import os as _os
        here = _os.path.dirname(_os.path.abspath(TS.__file__))
        before = set(_os.listdir(here))
        real_open = builtins.open

        def guard(p, *a, **k):
            mode = str(a[0] if a else k.get("mode", "r"))
            if (isinstance(p, str) and "w" in mode
                    and _os.path.dirname(_os.path.abspath(p)) == here):
                raise PermissionError("package dir is read-only (test)")
            return real_open(p, *a, **k)

        builtins.open = guard
        try:
            self.assertTrue(TS.run_tests(
                "def f(x):\n    return x + 1\n", "f",
                [{"call": "f(1)", "expect": 2},
                 {"call": "f(2)", "expect": 3},
                 {"call": "f(0)", "expect": 1}]))
        finally:
            builtins.open = real_open
        self.assertEqual(set(_os.listdir(here)) - before, set(),
                         "run_tests 不应在包目录创建任何文件")

    def test_child_env_has_no_host_secrets(self):
        old = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "host-secret-should-not-leak"
        os.environ["OPENAI_API_KEY"] = "host-openai-key"
        try:
            env = TS.build_child_env()
        finally:
            if old is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = old
            os.environ.pop("OPENAI_API_KEY", None)
        for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "AGENTLAB_API_TOKEN",
                  "EVOGUARD_AUDIT_HMAC_KEY"):
            self.assertNotIn(k, env, "子进程环境不应带 %s" % k)
        self.assertEqual(env.get("PYTHONUTF8"), "1")
