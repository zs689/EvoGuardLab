# -*- coding: utf-8 -*-
"""AGENT-E2E-01：编码工具集注册 + controlled 门禁 + 修 Bug 闭环。

不依赖 Docker：用本地 SandboxExecutor + 空 docker_sandbox 验证工具契约、
路径围栏、写审批与越权拒绝；Docker 真执行由 test_docker_sandbox 覆盖。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import approval                                             # noqa: E402
import autonomous_agent as AA                               # noqa: E402
import core                                                 # noqa: E402
import sandbox_executor as SBX                              # noqa: E402
import tool_registry as TR                                  # noqa: E402


def _setup():
    work = tempfile.mkdtemp(prefix="coding-")
    reg = TR.ToolRegistry()
    ex = SBX.register_coding_tools(reg, root=work, docker_sandbox=None)
    reg.declare_agent("coder", ["fs.read", "fs.write", "code.run_in_sandbox"],
                      replace=True)
    store = core.Store(os.path.join(work, "a.db"))
    gate = approval.ApprovalGate(store)
    aid = gate._new_request("e", "fs.write",
                            {"path": "mod.py", "content": "*"},
                            tenant_id="eval", first_write=True)
    gate.approve(aid, approved_by="admin", tenant_id="eval")
    agent = AA.ReActAgent(goal="fix", provider=None, registry=reg,
                          executor=ex, agent_type="coder", enforce=True,
                          has_approval_backend=True)
    return work, reg, ex, gate, aid, agent, store


class TestCodingTools(unittest.TestCase):
    def test_write_then_read_roundtrip(self):
        work, reg, ex, gate, aid, agent, store = _setup()
        self.addCleanup(store.close)
        ex("fs.write", {"path": "mod.py", "content": "x = 1\n"})
        out = ex("fs.read", {"path": "mod.py"})
        self.assertEqual(out["content"], "x = 1\n")
        self.assertTrue(os.path.isfile(os.path.join(work, "mod.py")))

    def test_write_requires_approval_id(self):
        """controlled 下 fs.write 不带审批 id 必须被 preflight 拒。"""
        work, reg, ex, gate, aid, agent, store = _setup()
        self.addCleanup(store.close)
        pre = agent._preflight("fs.write",
                               {"path": "mod.py", "content": "x=1"}, 1)
        self.assertTrue(pre.get("stop"), "写工具无审批 id 竟被放行")

    def test_write_with_approval_passes(self):
        work, reg, ex, gate, aid, agent, store = _setup()
        self.addCleanup(store.close)
        pre = agent._preflight("fs.write",
                               {"path": "mod.py", "content": "x=1",
                                "approval_request_id": aid}, 1)
        self.assertFalse(pre.get("stop"), pre.get("reason"))

    def test_external_tool_denied_for_coder(self):
        """http.send 不在 coder 白名单 -> controlled 下被拒。"""
        work, reg, ex, gate, aid, agent, store = _setup()
        self.addCleanup(store.close)
        pre = agent._preflight("http.send",
                               {"url": "http://evil.example.com",
                                "payload": "x"}, 1)
        self.assertTrue(pre.get("stop"), "越权外发工具竟被放行")

    def test_path_fence_blocks_escape(self):
        """SandboxExecutor 围栏：.. 上跳 / 绝对路径被拒。"""
        work, reg, ex, gate, aid, agent, store = _setup()
        self.addCleanup(store.close)
        with self.assertRaises(SBX.SandboxError):
            ex("fs.read", {"path": "../../etc/passwd"})
        with self.assertRaises(SBX.SandboxError):
            ex("fs.read", {"path": "/etc/passwd"})

    def test_py_extension_allowed_for_read_write(self):
        """修 Bug 必需 .py 可读可写（否则闭环无从谈起）。"""
        work, reg, ex, gate, aid, agent, store = _setup()
        self.addCleanup(store.close)
        ex("fs.write", {"path": "a.py", "content": "print(1)\n"})
        self.assertEqual(ex("fs.read", {"path": "a.py"})["content"],
                         "print(1)\n")
        self.assertIn(".py", SBX.READ_EXT)
        self.assertIn(".py", SBX.WRITE_EXT)


if __name__ == "__main__":
    unittest.main()
