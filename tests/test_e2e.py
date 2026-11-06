# -*- coding: utf-8 -*-
"""AgentLab 自动化测试（unittest，标准库，零依赖）。

跑法（在 agentlab 目录下）：
    python -m unittest discover -s tests -v

覆盖：
  1. 状态机：非法迁移必须抛 E_INVALID_STATE
  2. 确定性：同 seed 两次运行的轨迹完全一致
  3. 故障矩阵：三类 Agent × 适用故障，诊断根因全部判对
  4. 防泄漏：诊断函数拿不到故障真值（签名 + 源码双重检查）
  5. 恢复对比：C 必须显著优于 B
  6. 端到端：起真实 HTTP 服务 → 建实验 → 跑 4 模式 → 取轨迹
"""
import inspect
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)

import agents      # noqa: E402
import core        # noqa: E402
import evaluate    # noqa: E402
import runtime     # noqa: E402
import sim         # noqa: E402


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return core.Store(path)


class TestStateMachine(unittest.TestCase):
    def test_illegal_transition(self):
        s = fresh_store()
        exp = s.create_experiment("t", "order", "A1001", "none", 0, 7, "C")
        run = s.create_run(exp["id"], "baseline", 7)
        s.set_state(run["id"], core.READY)
        with self.assertRaises(core.ApiError) as cm:
            s.set_state(run["id"], core.SUCCEEDED)   # READY 不能直接到 SUCCEEDED
        self.assertEqual(cm.exception.code, "E_INVALID_STATE")

    def test_terminal_state_locked(self):
        s = fresh_store()
        exp = s.create_experiment("t", "order", "A1001", "none", 0, 7, "C")
        run = runtime.run_once(s, exp, "baseline")
        self.assertEqual(run["status"], core.SUCCEEDED)
        with self.assertRaises(core.ApiError):
            s.set_state(run["id"], core.RUNNING)


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_trace(self):
        s = fresh_store()
        exp = s.create_experiment("t", "order", "A1001", "silent_corruption", 0, 7, "C")
        r1 = runtime.run_once(s, exp, "A")
        r2 = runtime.run_once(s, exp, "A")
        f1 = [(x["logical"], x["kind"], x["name"], x["status"], x["error_type"])
              for x in s.list_steps(r1["id"])]
        f2 = [(x["logical"], x["kind"], x["name"], x["status"], x["error_type"])
              for x in s.list_steps(r2["id"])]
        self.assertEqual(f1, f2, "同 seed 两次运行的轨迹必须完全一致")


class TestFaultMatrix(unittest.TestCase):
    def test_all_faults_diagnosed(self):
        s = fresh_store()
        bad = []
        for at in sim.AGENT_TYPES:
            task = sim.TASKS[at][0]["task_id"]
            for ft in sim.CASE_FAULTS[at]:
                exp = s.create_experiment("t", at, task, ft, 0, 7, "C")
                run = runtime.run_once(s, exp, "C")
                d = s.get_diagnosis(run["id"])
                got = d["root_cause"] if d else None
                if got != ft:
                    bad.append((at, ft, got))
        self.assertEqual(bad, [], "以下组合诊断错误：%s" % bad)

    def test_fault_actually_breaks_task(self):
        """故障必须真的让 A 模式失败，否则实验是空的。"""
        s = fresh_store()
        broken = []
        for at in sim.AGENT_TYPES:
            task = sim.TASKS[at][0]["task_id"]
            for ft in sim.CASE_FAULTS[at]:
                exp = s.create_experiment("t", at, task, ft, 0, 7, "C")
                run = runtime.run_once(s, exp, "A")
                if run["success"] == 1:
                    broken.append((at, ft))
        self.assertEqual(broken, [], "这些故障没让任务失败（等于没注入）：%s" % broken)


class TestNoLeakage(unittest.TestCase):
    def test_diagnose_signature(self):
        params = list(inspect.signature(runtime.diagnose).parameters)
        self.assertEqual(params, ["steps"], "诊断模块只允许接收 steps")

    def test_diagnose_cannot_read_faults(self):
        src = inspect.getsource(runtime.diagnose) + inspect.getsource(runtime._diagnose_impl)
        self.assertNotIn("get_faults", src, "诊断源码里不允许出现读故障真值的调用")
        self.assertNotIn("fault_type", src, "诊断源码里不允许直接使用故障标签")

    def test_diagnosis_record_has_no_label(self):
        s = fresh_store()
        exp = s.create_experiment("t", "order", "A1001", "silent_corruption", 0, 7, "C")
        run = runtime.run_once(s, exp, "C")
        d = s.get_diagnosis(run["id"])
        self.assertIsNotNone(d)
        # 证据里可以出现"重算值"，但不能出现故障类型标签
        self.assertNotIn("silent_corruption", json.dumps(d["evidence"], ensure_ascii=False))


class TestRecoveryGap(unittest.TestCase):
    def test_c_beats_b(self):
        """C（诊断驱动）必须明显优于 B（盲目重试）。差距没了说明实验设计失效。"""
        s = fresh_store()
        cases = evaluate.build_cases()
        res = [evaluate.run_case(s, c) for c in cases]
        m = evaluate.aggregate(s, res)
        self.assertEqual(m["rate_baseline"], 100.0, "baseline 必须 100%，否则任务本身有问题")
        self.assertLess(m["rate_A"], 20.0, "A 应该基本全错，否则故障没注入")
        self.assertGreater(m["rate_C"] - m["rate_B"], 30.0,
                           "C 与 B 的差距必须显著（实测 %.1f vs %.1f）" % (m["rate_C"], m["rate_B"]))
        self.assertGreaterEqual(m["detect_f1"], 0.9, "检测 F1 不应低于 0.9")


class TestApiE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as appmod
        cls.appmod = appmod
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_full_flow(self):
        h = self._get("/api/health")
        self.assertTrue(h["ok"])
        meta = self._get("/api/meta")
        self.assertEqual(len(meta["agent_types"]), 3)
        self.assertEqual(len(meta["fault_types"]), 8)

        exp = self._post("/api/experiments", {
            "agent_type": "order", "task_id": "A1001",
            "fault_type": "silent_corruption", "seed": 7})
        self.assertIn("id", exp)

        results = {}
        for mode in ("baseline", "A", "B", "C"):
            run = self._post("/api/experiments/%s/run" % exp["id"], {"mode": mode})
            results[mode] = run
            self.assertGreater(len(run["steps"]), 0, "%s 模式没有产生轨迹" % mode)

        self.assertEqual(results["baseline"]["success"], 1, "无故障基线必须成功")
        self.assertEqual(results["A"]["success"], 0, "注入故障后 A 必须失败")
        self.assertEqual(results["C"]["success"], 1, "C 模式应该恢复成功")
        self.assertIsNotNone(results["C"]["diagnosis"], "C 模式必须留下诊断记录")
        self.assertEqual(results["C"]["diagnosis"]["root_cause"], "silent_corruption")

        got = self._get("/api/runs/" + results["C"]["id"])
        self.assertEqual(got["id"], results["C"]["id"])
        self.assertTrue(all("logical" in s for s in got["steps"]))

    def test_bad_request(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/api/experiments", {"agent_type": "order"})
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
