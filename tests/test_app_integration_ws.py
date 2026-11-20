# -*- coding: utf-8 -*-
"""INT-01-T3：主路径接线端到端测试（**真启 HTTP server**，不直接调底层模块）。

这条测试的存在意义：证明「模块通过单测」之外，服务入口真的调用了它们。
以前 EvoGuardLab 出过「模块 100% 通过但主路径一行没调」的问题。
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

# 隔离开发者本地 .env：本测试走 mock，不能被本机 EVOGUARD_PROVIDER 污染
# （否则会去调真实云端 API，测试进程无 key -> 500）。必须在 import app 前设。
os.environ["EVOGUARD_SKIP_DOTENV"] = "1"
os.environ["AGENTLAB_LLM"] = "mock"
os.environ.pop("EVOGUARD_PROVIDER", None)   # 防止被别的测试/早先的 .env 加载污染


def _serve():
    """起一个真实 HTTP server（随机端口），返回 (server, port, thread)。"""
    import app
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1], t


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["AGENTLAB_DB_DIR"] = cls.tmp.name
        # 清掉可能被其它测试污染的 provider 缓存（避免串用 deepseek）
        try:
            import gateway as _gw
            _gw.reset_cache()
        except Exception:
            pass
        cls.srv, cls.port, cls.th = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw.decode("utf-8"))
        except Exception:
            return r.status, raw.decode("utf-8", "replace")


class TestMainPathWiring(Base):
    def test_ready_reports_backends(self):
        """ready 必须暴露后端装配状态（memory/redis、auth 模式、errors）。"""
        code, body = self.req("GET", "/api/ready")
        self.assertIn(code, (200, 503))
        self.assertIn("backends", body)
        self.assertEqual(body["backends"]["limiter"], "memory")
        self.assertEqual(body["backends"]["auth_mode"], "off")

    def test_react_route_is_reachable_and_guards(self):
        """POST /api/react/run：越权工具必须 executed=False。"""
        code, body = self.req("POST", "/api/react/run", {
            "goal": "查一下订单",
            "agent_type": "react",
            "max_steps": 1,
            "tools": [{"name": "order.settle",
                       "schema": {"type": "object", "properties": {}}}],
            "executor": "stub"})
        self.assertEqual(code, 200)
        # 没有角色白名单时 settle 不在允许集 -> 门禁拒绝
        self.assertFalse(body["contract"]["executed"])
        for s in body["steps"]:
            self.assertFalse(s.get("executed"),
                             "越权工具被执行了：%s" % s)
    def test_react_reports_memory_audit(self):
        """主路径必须回报记忆注入审计（否则又是「模块绿但入口没调」）。"""
        code, body = self.req("POST", "/api/react/run", {
            "goal": "核对订单金额", "agent_type": "react",
            "max_steps": 1, "executor": "none"})
        self.assertEqual(code, 200)
        self.assertIn("memory", body)
        self.assertIn("injected", body["memory"])

    def test_react_can_disable_memory(self):
        code, body = self.req("POST", "/api/react/run", {
            "goal": "核对订单金额", "use_memory": False,
            "max_steps": 1, "executor": "none"})
        self.assertEqual(code, 200)
        self.assertEqual(body["memory"]["injected"], 0)

    def test_react_requires_goal(self):
        code, body = self.req("POST", "/api/react/run", {"goal": ""})
        self.assertEqual(code, 400)
    def test_semantic_layer_is_opt_in(self):
        """有害请求语义层默认关，不能拖慢普通请求。"""
        import importlib
        import app as A
        importlib.reload(A)
        self.assertFalse(A._HARMFUL_JUDGE_ON)
    def test_skill_monitor_requires_id(self):
        code, body = self.req("POST", "/api/skills/monitor", {})
        self.assertEqual(code, 400)

    def test_skill_monitor_unknown_skill(self):
        code, body = self.req("POST", "/api/skills/monitor",
                              {"skill_id": "sk_nonexistent"})
        self.assertEqual(code, 409)
        self.assertEqual(body.get("error"), "skill_not_found")

    def test_skill_synthesize_route(self):
        code, body = self.req("POST", "/api/skills/synthesize", {
            "steps": [{"seq": 1, "logical": 1, "name": "file.read",
                       "status": "error", "error_type": "timeout"}],
            "task_type": "newdomain"})
        self.assertEqual(code, 200)
        self.assertEqual(body["diagnosis"]["root_cause"], "tool_timeout")

    def test_skill_synthesize_empty_trace_is_reported(self):
        code, body = self.req("POST", "/api/skills/synthesize",
                              {"steps": [{"seq": 1, "logical": 1,
                                          "name": "x", "status": "ok",
                                          "output": {}}]})
        self.assertEqual(code, 200)
        self.assertFalse(body["accepted"]["accepted"])


class TestRateLimitStillWorks(Base):
    def test_429_after_limit(self):
        """限流仍走主路径（默认内存 limiter，行为不变）。"""
        import app
        limit = app.CONTROLS.config.rate_limit
        codes = []
        for _ in range(min(limit + 2, 70)):
            c, _b = self.req("GET", "/api/live")
            codes.append(c)
        self.assertIn(429, codes, "限流没有生效")


class TestGrepWiring(unittest.TestCase):
    """静态断言：新模块在主入口里确有真实调用（防回退成孤岛）。"""

    def test_app_imports_new_modules(self):
        # FINAL-04-T1：app.py 已拆分为 app_core/routes_agent/routes_enterprise，
        # 接线证明读取拆分后的全部模块源码（接口未变，只是源码位置移动）。
        src = ""
        for name in ("app.py", "app_core.py", "routes_agent.py",
                     "routes_enterprise.py"):
            with open(os.path.join(ROOT, "ai-service", "api", name),
                      encoding="utf-8") as f:
                src += f.read()
        for mod in ("autonomous_agent", "skill_synthesis", "redis_backend"):
            self.assertIn(mod, src, "拆分后的模块没有引用 %s" % mod)

    def test_rails_enable_evidence_grounding(self):
        import tool_rails as TR
        rules = TR.load_default()
        self.assertIn("kb.answer_grounded", rules)


if __name__ == "__main__":
    unittest.main()
