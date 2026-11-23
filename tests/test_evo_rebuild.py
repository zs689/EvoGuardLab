# -*- coding: utf-8 -*-
"""EVO-REBUILD-01-T1：自进化产物必须是「高信息增量」而非泛泛提示。

第一性原理：自进化只有沉淀 1) 私有环境约定（env_invariant）或
2) 可执行确定性检查（pre_check）才有真实增益；泛泛流程提示实测有害。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import skill_synthesis as SS                                # noqa: E402
import conditional_skill as CS                              # noqa: E402
import core as CORE                                        # noqa: E402


def _store():
    d = tempfile.mkdtemp(prefix="evo-rebuild-")
    return CORE.Store(os.path.join(d, "x.db"))


class TestEnvInvariantExtraction(unittest.TestCase):
    def test_naming_invariant_from_keyerror(self):
        steps = [
            {"seq": 1, "logical": 1, "name": "file.read", "status": "ok",
             "output": {"path": "m.py"}},
            {"seq": 2, "logical": 2, "name": "http.fetch", "status": "error",
             "error_type": "KeyError",
             "output": {"stderr": "KeyError: 'user_name'"}},
        ]
        invs = SS.extract_env_invariants(steps)
        self.assertTrue(invs, "未从真实 KeyError 提取到命名约定")
        self.assertEqual(invs[0]["domain"], "naming")
        self.assertIn("user_name", invs[0]["rule"])
        self.assertIn("user_name", invs[0]["evidence"])

    def test_pagination_and_timezone(self):
        steps = [{"seq": 1, "name": "sql.query", "status": "error",
                  "error_type": "HTTPError",
                  "output": {"stderr": "422 offset must be >= 1 and invalid "
                                       "date format (not ISO 8601)"}}]
        invs = SS.extract_env_invariants(steps)
        domains = {x["domain"] for x in invs}
        self.assertIn("pagination", domains)
        self.assertIn("timezone", domains)

    def test_no_evidence_gives_empty(self):
        """没有真实错误证据时绝不编造规约。"""
        steps = [{"seq": 1, "name": "file.read", "status": "ok", "output": {}}]
        self.assertEqual(SS.extract_env_invariants(steps), [])

    def test_env_spec_requires_hit(self):
        """显式规约也必须在轨迹里真命中才算证据。"""
        steps = [{"seq": 1, "name": "file.read", "status": "error",
                  "error_type": "ValueError",
                  "output": {"stderr": "columns must be snake_case"}}]
        spec = [
            {"domain": "naming", "rule": "列名用 snake_case",
             "match": r"snake_case"},
            {"domain": "billing", "rule": "金额单位是分不是元",
             "match": r"unit_must_be_cents"},
        ]
        invs = SS.extract_env_invariants(steps, env_spec=spec)
        rules = " ".join(x["rule"] for x in invs)
        self.assertIn("snake_case", rules)
        self.assertNotIn("金额单位", rules, "没发生的规约不应被写入")


class TestPreCheckExtraction(unittest.TestCase):
    def test_verification_and_coupling(self):
        repair = [
            {"seq": 1, "name": "fs.patch", "status": "ok",
             "output": {"patched": True, "path": "a.py"}},
            {"seq": 2, "name": "fs.patch", "status": "ok",
             "output": {"patched": True, "path": "b.py"}},
            {"seq": 3, "name": "code.run_in_sandbox", "status": "ok",
             "output": {"returncode": 0}},
        ]
        checks = SS.extract_pre_checks([], repair)
        actions = {c["action"] for c in checks}
        self.assertIn("run_tests_after_write", actions)
        self.assertIn("check_coupled_files", actions)

    def test_no_repair_gives_empty(self):
        self.assertEqual(SS.extract_pre_checks([], []), [])


class TestInfoGainGate(unittest.TestCase):
    def test_flat_skill_rejected(self):
        """空泛套话技能必须被拒收。"""
        flat = {"env_invariants": [], "pre_checks": [],
                "fix_pattern": "timeout -> retry"}
        errs = CS.validate_info_gain(flat)
        self.assertTrue(errs, "空泛技能竟然通过了信息增量门禁")

    def test_skill_with_invariant_accepted(self):
        skill = {"env_invariants": [{"domain": "naming",
                                     "rule": "用 snake_case",
                                     "evidence": "KeyError: 'user_name'"}],
                 "pre_checks": []}
        self.assertEqual(CS.validate_info_gain(skill), [])

    def test_invariant_without_evidence_rejected(self):
        """没有证据的规约等于猜测，同样拒收。"""
        skill = {"env_invariants": [{"domain": "naming", "rule": "用 snake_case",
                                     "evidence": ""}],
                 "pre_checks": []}
        self.assertTrue(CS.validate_info_gain(skill))

    def test_accept_default_keeps_backward_compat(self):
        """既有声明式技能（无新字段）在默认路径仍可接受。"""
        sk = CS.example_skill()
        pol = {"allowed_tools": list(sk.get("allowed_tools") or []),
               "write_tools": []}
        out = CS.accept(sk, pol)          # 不传 require_info_gain
        self.assertTrue(out.get("accepted"), out.get("errors"))


class TestSynthesizeGating(unittest.TestCase):
    FAIL_STEPS = [
        {"seq": 1, "logical": 1, "name": "file.read", "status": "ok",
         "output": {"path": "m.py"}},
        {"seq": 2, "logical": 2, "name": "http.fetch", "status": "error",
         "error_type": "KeyError",
         "output": {"stderr": "KeyError: 'user_name'"}},
    ]
    REPAIR = [
        {"seq": 3, "name": "fs.patch", "status": "ok",
         "output": {"patched": True, "path": "m.py"}},
        {"seq": 4, "name": "code.run_in_sandbox", "status": "ok",
         "output": {"returncode": 0}},
    ]

    def test_paired_trace_with_evidence_accepted(self):
        st = _store()
        try:
            out = SS.synthesize(st, self.FAIL_STEPS,
                                repair_steps=self.REPAIR,
                                require_info_gain=True)
            self.assertTrue(out["accepted"]["accepted"],
                            out["accepted"].get("errors"))
            sk = out["skill"]
            self.assertTrue(sk["env_invariants"], "合成结果缺 env_invariants")
            self.assertTrue(sk["pre_checks"], "合成结果缺 pre_checks")
        finally:
            st.close()

    def test_failure_only_trace_rejected(self):
        """无信息量的失败轨迹 -> 提不出 env_invariant/pre_check -> 必须拒收。

        注意：如果失败消息里带真实证据（如 KeyError: 'user_name'），
        即使没有修复轨迹也**应该**接受（env_invariant 有证据）。
        这里用一条不含任何私有环境信息的超时错误，才是真正的空壳。
        """
        st = _store()
        try:
            flat_fail = [
                {"seq": 1, "logical": 1, "name": "file.read", "status": "ok",
                 "output": {"path": "m.py"}},
                {"seq": 2, "logical": 2, "name": "http.fetch",
                 "status": "error", "error_type": "timeout",
                 "output": {"stderr": "timed out"}},
            ]
            out = SS.synthesize(st, flat_fail, require_info_gain=True)
            self.assertFalse(out["accepted"]["accepted"],
                             "无信息增量的空壳技能被接受了")
            joined = " ".join(out["accepted"].get("errors") or [])
            self.assertIn("信息增量", joined)
        finally:
            st.close()

    def test_failure_with_evidence_accepted(self):
        """失败轨迹里带私有环境证据时，即使无修复轨迹也应接受。"""
        st = _store()
        try:
            out = SS.synthesize(st, self.FAIL_STEPS, require_info_gain=True)
            self.assertTrue(out["accepted"]["accepted"],
                            out["accepted"].get("errors"))
            self.assertTrue(out["skill"]["env_invariants"])
        finally:
            st.close()


class TestAutoMonitor(unittest.TestCase):
    """EVO-REBUILD-01-T2：退化监控必须自动触发，不能只靠人工调 API。"""

    SKILL = "sk_auto"

    def _setup(self, after_rate=0.95):
        import skills as SK
        st = _store()
        SK.ensure_schema(st)
        with st.conn() as c:
            c.execute(
                "INSERT INTO skills(id,name,task_type,fault_type,strategy,"
                "required_tools,safety,status,version,created_at,"
                "active_version,promoted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.SKILL, "auto", "newdomain", "timeout",
                 '["retry"]', "[]", "{}", SK.ACTIVE, 1, 1.0, 1, 1.0))
            c.execute(
                "INSERT INTO skill_evaluations(id,skill_id,target_n,"
                "before_rate,after_rate,gain_pp,guard_n,neg_transfer_rate,"
                "verdict,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("ev1", self.SKILL, 10, 0.5, after_rate, 40.0, 10, 0.0,
                 "PROMOTE", 1.0))
        return st

    def _add_run(self, st, rid, *, success=None, denied=0):
        with st.conn() as c:
            c.execute(
                "INSERT INTO runs(id,experiment_id,mode,status,seed,"
                "started_at,skill_id) VALUES(?,?,?,?,?,?,?)",
                (rid, "exp1", "C", "running", 1, 1.0, self.SKILL))
            for i in range(denied):
                c.execute(
                    "INSERT INTO steps(id,run_id,seq,logical,kind,name,status)"
                    " VALUES(?,?,?,?,?,?,?)",
                    ("%s-s%d" % (rid, i), rid, i, i, "denied", "shell.exec",
                     "blocked"))
            c.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
        st.set_state(rid, "running") if False else None
        return rid

    def test_unauthorized_in_production_step_format_triggers(self):
        """用 ctx.py **真实写入**的轨迹格式验证越权立即检查。

        此前测试只手工造 kind='denied'/status='blocked'，而生产路径写的是
        kind='security'/status='error'/error_type='approval_required'。
        统计函数只认前者 -> 生产上这条路径一次都没触发过。
        """
        import core as C
        st = self._setup(after_rate=0.5)
        try:
            C.AUTO_MONITOR_LAST.clear()
            rid = "r-prod-deny"
            self._add_run(st, rid)
            with st.conn() as c:
                c.execute(
                    "INSERT INTO steps(id,run_id,seq,logical,kind,name,status,"
                    "error_type) VALUES(?,?,?,?,?,?,?,?)",
                    (rid + "-s0", rid, 0, 0, "security", "shell.exec",
                     "error", "approval_required"))
            st.finish_run(rid, 1, "ok", "ok", 1, 1)
            last = dict(C.AUTO_MONITOR_LAST)
            self.assertTrue(last.get("auto"),
                            "生产的 security 轨迹没触发立即监控：%s" % last)
            self.assertGreaterEqual(int(last.get("unauthorized") or 0), 1)
        finally:
            st.close()

    def test_window_triggers_rollback_without_api_call(self):
        """连续 5 次失败运行后自动回滚，无需人工调 /api/skills/monitor。"""
        import core as C
        st = self._setup(after_rate=0.95)
        try:
            C.AUTO_MONITOR_LAST.clear()
            outs = []
            for i in range(C_ss_window()):
                rid = "r%d" % i
                self._add_run(st, rid, success=0)
                st.finish_run(rid, 0, "x", "y", 1, 1)
                outs.append(dict(C.AUTO_MONITOR_LAST))
            last = outs[-1]
            self.assertTrue(last.get("auto"),
                            "第 %d 次运行仍未自动触发监控：%s"
                            % (C_ss_window(), last))
            self.assertTrue(last.get("degraded"),
                            "成功率 0%% 远低于基线 95%%，未判退化")
            with st.conn() as c:
                r = c.execute("SELECT status FROM skills WHERE id=?",
                              (self.SKILL,)).fetchone()
            self.assertIn(r["status"], ("disabled", "candidate", "rejected"),
                          "退化后技能仍是 %s，未止血" % r["status"])
        finally:
            st.close()

    def test_unauthorized_triggers_immediately(self):
        """出现越权步骤时不等窗口，立即检查。"""
        import core as C
        st = self._setup(after_rate=0.5)
        try:
            C.AUTO_MONITOR_LAST.clear()
            rid = "rx"
            self._add_run(st, rid, success=1, denied=2)
            st.finish_run(rid, 1, "ok", "ok", 1, 1)
            last = dict(C.AUTO_MONITOR_LAST)
            self.assertTrue(last.get("auto"), "越权未立即触发监控：%s" % last)
            self.assertGreaterEqual(int(last.get("unauthorized") or 0), 1)
        finally:
            st.close()

    def test_no_skill_run_skips(self):
        """没有关联技能的运行不应触发监控。"""
        import core as C
        st = _store()
        try:
            C.AUTO_MONITOR_LAST.clear()
            with st.conn() as c:
                c.execute("INSERT INTO runs(id,experiment_id,mode,status,seed,"
                          "started_at) VALUES(?,?,?,?,?,?)",
                          ("rn", "e", "C", "running", 1, 1.0))
            st.finish_run("rn", 1, "a", "b", 1, 1)
            self.assertEqual(C.AUTO_MONITOR_LAST.get("skipped"),
                             "run_has_no_skill")
        finally:
            st.close()

    def test_auto_monitor_can_be_disabled(self):
        import core as C
        old = os.environ.get(C.AUTO_MONITOR_ENV)
        os.environ[C.AUTO_MONITOR_ENV] = "0"
        try:
            st = _store()
            try:
                C.AUTO_MONITOR_LAST.clear()
                with st.conn() as c:
                    c.execute("INSERT INTO runs(id,experiment_id,mode,status,"
                              "seed,started_at) VALUES(?,?,?,?,?,?)",
                              ("rd", "e", "C", "running", 1, 1.0))
                st.finish_run("rd", 1, "a", "b", 1, 1)
                self.assertEqual(C.AUTO_MONITOR_LAST.get("skipped"),
                                 "disabled_by_env")
            finally:
                st.close()
        finally:
            if old is None:
                os.environ.pop(C.AUTO_MONITOR_ENV, None)
            else:
                os.environ[C.AUTO_MONITOR_ENV] = old


def C_ss_window():
    import skill_synthesis as _s
    return _s.AUTO_MONITOR_WINDOW


class TestAutoAnchor(unittest.TestCase):
    """自动回滚必须写库外审计锚点（异地可取证）。"""

    def test_event_anchor_written(self):
        import external_anchor as EA
        import skill_synthesis as SS
        d = tempfile.mkdtemp(prefix="anchor-evo-")
        path = os.path.join(d, "anchor.jsonl")
        old = os.environ.get("EVOGUARD_AUDIT_ANCHOR_FILE")
        old_key = os.environ.get("EVOGUARD_ANCHOR_HMAC_KEY")
        os.environ["EVOGUARD_AUDIT_ANCHOR_FILE"] = path
        os.environ["EVOGUARD_ANCHOR_HMAC_KEY"] = "test-anchor-key"
        try:
            ok = SS._write_auto_anchor({"skill_id": "sk_auto",
                                        "degraded": True,
                                        "rolled_back": True})
            self.assertTrue(ok)
            evs = EA.read_events(path, kind="auto_rollback")
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["payload"]["skill_id"], "sk_auto")
            self.assertTrue(evs[0]["hmac"], "事件锚点没有 HMAC 签名")
        finally:
            if old is None:
                os.environ.pop("EVOGUARD_AUDIT_ANCHOR_FILE", None)
            else:
                os.environ["EVOGUARD_AUDIT_ANCHOR_FILE"] = old
            if old_key is None:
                os.environ.pop("EVOGUARD_ANCHOR_HMAC_KEY", None)
            else:
                os.environ["EVOGUARD_ANCHOR_HMAC_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
