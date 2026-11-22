# -*- coding: utf-8 -*-
"""WS-04-T2：失败→诊断→合成→独立验证→晋升/回滚闭环（新领域）。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import conditional_skill as CS                              # noqa: E402
import core                                                 # noqa: E402
import skill_synthesis as SS                                # noqa: E402
import skills                                               # noqa: E402


def newdomain_cases(prefix, n=3, domain="file"):
    """全新领域任务：不在 evolution_bench 里硬编码。"""
    return [{"case_id": "%s-%s-%d" % (prefix, domain, i),
             "agent_type": "newdomain", "task_id": "%s-%s-%d" % (prefix, domain, i),
             "family_id": "%s-%d" % (domain, i),  # 每条是独立任务家族
             "fault_type": "tool_timeout", "seed": 7}
            for i in range(n)]


class TestClosedLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "s.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.steps = [
            {"seq": 1, "logical": 1, "name": "file.read", "status": "error",
             "error_type": "timeout", "output": None},
            {"seq": 2, "logical": 2, "name": "http.fetch", "status": "ok",
             "output": {"docs": ["ok"]}},
        ]

    def test_synthesis_rejects_write_tool_declaration(self):
        s = SS.synthesize(self.store, self.steps, task_type="newdomain",
                          policy={"allowed_tools": ["file.read"],
                                  "write_tools": []})
        # 候选默认声明 3 个只读工具，超出 policy 的 1 个 → 门禁拒绝
        self.assertFalse(s["accepted"]["accepted"])

    def test_full_loop_promotes_when_gain_and_no_regression(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")
        self.assertTrue(s["accepted"]["accepted"], s["accepted"])

        def runner(case, skill):
            # 目标组：baseline 失败、candidate 成功 → 增益。
            # guard 组：baseline 与 candidate 都成功 → 零负迁移可估计。
            if case["task_id"].startswith("grd"):
                return True
            return skill is not None

        out = SS.evaluate_and_promote(
            self.store, s["skill"], target, guard, runner,
            training_cases=training)
        self.assertTrue(out["promoted"])
        self.assertEqual(out["status"], "active")
        self.assertFalse(out["rolled_back"])

    def test_bad_skill_is_rejected_and_rolled_back(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")

        def runner(case, skill):
            # guard 组：baseline 成功、带 skill 反而失败 → 负迁移
            if case["task_id"].startswith("grd"):
                return skill is None
            return skill is not None

        out = SS.evaluate_and_promote(
            self.store, s["skill"], target, guard, runner,
            training_cases=training)
        self.assertFalse(out["promoted"])
        self.assertEqual(out["evaluation"]["verdict"], "REJECT")
        self.assertEqual(out["status"], "REJECT")

    def test_training_overlap_is_rejected(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")
        overlap = [training[0]] + target[1:]
        with self.assertRaises(ValueError):
            SS.evaluate_and_promote(
                self.store, s["skill"], overlap, guard,
                lambda c, sk: True, training_cases=training)

    def test_closed_loop_end_to_end(self):
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        # EVO-REBUILD-01-T1：线上闭环强制信息增量门禁，所以这里用带真实
        # 证据（KeyError 键名）的失败轨迹 —— 这正是线上会遇到的情形。
        steps = [
            {"seq": 1, "logical": 1, "name": "file.read", "status": "ok",
             "output": {"path": "m.py"}},
            {"seq": 2, "logical": 2, "name": "http.fetch", "status": "error",
             "error_type": "KeyError",
             "output": {"stderr": "KeyError: 'user_name'"}},
        ]
        out = SS.closed_loop(
            self.store, steps,
            target_cases=target, guard_cases=guard,
            training_cases=training,
            run_case=lambda c, sk: (True if c["task_id"].startswith("grd")
                                    else sk is not None),
            task_type="newdomain")
        self.assertEqual(out["stage"], "promotion")
        self.assertTrue(out["promotion"]["promoted"])

    def test_closed_loop_rejects_flat_skill(self):
        """无信息增量的空壳技能不得走完闭环晋升。"""
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")
        out = SS.closed_loop(
            self.store, self.steps,       # timeout，无任何私有环境证据
            target_cases=target, guard_cases=guard,
            training_cases=training,
            run_case=lambda c, sk: True,
            task_type="newdomain")
        self.assertEqual(out["stage"], "synthesis")
        self.assertFalse(out["synthesis"]["accepted"]["accepted"])

    def test_describe_lists_new_domain(self):
        d = SS.describe()
        self.assertIn("sql.query", d["tools"])
        self.assertIn("independent_eval", d["stages"])


if __name__ == "__main__":
    unittest.main()

class TestOnlineMonitorRollback(unittest.TestCase):
    """FIX-C1：线上退化必须基于**上线后真实运行指标**触发回滚，
    而不是复用晋升时那份评估结果（旧实现在此是死代码）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "m.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.steps = [
            {"seq": 1, "logical": 1, "name": "file.read", "status": "error",
             "error_type": "timeout", "output": None},
            {"seq": 2, "logical": 2, "name": "http.fetch", "status": "ok",
             "output": {"docs": ["ok"]}},
        ]
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")
        assert s["accepted"]["accepted"], s["accepted"]
        self.skill = s["skill"]
        training = newdomain_cases("train", 3, "file")
        target = newdomain_cases("tgt", 3, "http")
        guard = newdomain_cases("grd", 3, "sql")

        # guard 组两侧都成功（零负迁移可估计）；目标组只有 candidate 成功。
        # 前缀用 family 判定，避免第二批 guard 用例被漏判成「基线失败」。
        def runner(case, skill):
            if case["task_id"].split("-")[0].startswith("grd") \
                    or case["task_id"].startswith("g2"):
                return True
            return skill is not None

        out = SS.evaluate_and_promote(
            self.store, self.skill, target, guard, runner,
            training_cases=training)
        assert out["promoted"], out
        self.skill_id = self.skill["id"]
        # 再晋升一个版本：否则 rollback 会因「v1 之前没有更旧快照」失败，
        # 测不到真正的版本回退（这是 skills.rollback 的真实前置条件）。
        ev2 = skills.evaluate(
            self.store, self.skill, newdomain_cases("t2", 3, "http"),
            newdomain_cases("g2", 3, "sql"), runner,
            training_cases=newdomain_cases("tr2", 3, "file"))
        skills.promote(self.store, self.skill_id, evaluation_id=ev2["id"])

    def _online_runs(self, n=6, success=True):
        """往 runs 表写「上线后」的真实运行记录。"""
        exp = self.store.create_experiment("mon", "newdomain", "T-mon",
                                          "tool_timeout", 0, 7)
        for i in range(n):
            # create_run 返回的是记录 dict（内部 get_run），取 id 再 finish
            run = self.store.create_run(exp["id"], "C", 7)
            rid = run["id"] if isinstance(run, dict) else run
            self.store.finish_run(rid, 1 if success else 0,
                                  "x" if success else "bad", "", 10, 100)
            # 归属本技能：没有 skill_id 的 runs 只能按 task_type 近似汇总，
            # 那种数据不能用来判退化（会拿全量成功率误杀好技能）。
            with self.store.conn() as c:
                c.execute("UPDATE runs SET skill_id=? WHERE id=?",
                          (self.skill_id, rid))

    def test_promotion_alone_does_not_rollback(self):
        """旧死代码的等价场景：刚晋升、没有线上数据，绝不能自判退化。"""
        r = SS.monitor_and_rollback(self.store, self.skill_id)
        self.assertFalse(r["degraded"])
        self.assertFalse(r["rolled_back"])
        self.assertEqual(r["status"], "active")
        self.assertIn("样本不足", " ".join(r["reasons"]))

    def test_insufficient_samples_never_rolls_back(self):
        """2 条失败 < min_runs(5)：不因噪声回滚。"""
        self._online_runs(n=2, success=False)
        r = SS.monitor_and_rollback(self.store, self.skill_id)
        self.assertFalse(r["rolled_back"], r["reasons"])

    def test_healthy_online_runs_keep_active(self):
        self._online_runs(n=8, success=True)
        r = SS.monitor_and_rollback(self.store, self.skill_id)
        self.assertFalse(r["degraded"])
        self.assertEqual(r["status"], "active")

    def test_unauthorized_side_effect_triggers_real_rollback(self):
        """核心验收：上线后出现越权副作用 -> 真回滚 + 版本退回快照。"""
        self._online_runs(n=6, success=True)
        import security as SEC
        SEC.ensure_schema(self.store)
        with self.store.conn() as c:
            rid = c.execute("SELECT id FROM runs LIMIT 1").fetchone()["id"]
            c.execute(
                "INSERT INTO security_effects(id,ts,run_id,where_,tool,"
                "detected,decision,action,attempted,executed,effect,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("eff1", 1.0, rid, "tool_approval", "sql.update", 1, "deny",
                 "blocked", 1, 1, "state_changed", "越权写"))
        before = [x for x in skills.list_skills(self.store)
                  if x["id"] == self.skill_id][0]
        r = SS.monitor_and_rollback(self.store, self.skill_id)
        self.assertTrue(r["degraded"])
        # 止血有两条正当路径：
        #   a) 回滚到**正文不同**的旧版本（rolled_back，版本号下降）；
        #   b) 旧版本正文与当前一致时，回滚等于没换药，直接停用（disabled）。
        # 单次 promote 的 fixture 属于 (b)：v1/v2 正文相同，所以断言不能
        # 只认 (a)。两条路径都必须让技能脱离 active（或版本真的退回去）。
        self.assertTrue(r["rolled_back"] or r.get("disabled"), r.get("error"))
        after = [x for x in skills.list_skills(self.store)
                 if x["id"] == self.skill_id][0]
        if r["rolled_back"]:
            self.assertLess(r["version_after"], r["version_before"])
            self.assertLess(int(after["active_version"]),
                            int(before["active_version"]))
            self.assertIsNotNone(after.get("rolled_back_at"))
        else:
            self.assertEqual(after["status"], skills.DISABLED)

    def test_success_drop_triggers_rollback(self):
        """成功率相对基线掉超过阈值也要回滚（不只看越权）。"""
        self._online_runs(n=8, success=False)
        r = SS.monitor_and_rollback(self.store, self.skill_id)
        self.assertTrue(r["degraded"], r["reasons"])
        self.assertTrue(any("成功率下降" in x for x in r["reasons"]))

    def test_dry_run_does_not_mutate(self):
        self._online_runs(n=8, success=False)
        r = SS.monitor_and_rollback(self.store, self.skill_id, dry_run=True)
        self.assertTrue(r["degraded"])
        self.assertFalse(r["rolled_back"])
        row = [x for x in skills.list_skills(self.store)
               if x["id"] == self.skill_id][0]
        self.assertEqual(row["status"], "active")

    def test_rollback_is_audited(self):
        self._online_runs(n=8, success=False)
        SS.monitor_and_rollback(self.store, self.skill_id)
        with self.store.conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM skill_rollback_audit "
                          "WHERE skill_id=?", (self.skill_id,)).fetchone()["n"]
        self.assertGreaterEqual(n, 1)

    def test_rollback_failure_is_reported_honestly(self):
        """回滚本身失败时（如快照缺失）必须如实报错，不能假装成功。"""
        self._online_runs(n=8, success=False)
        orig = skills.rollback

        def boom(*a, **k):
            raise KeyError("没有可回滚的版本")

        skills.rollback = boom
        try:
            r = SS.monitor_and_rollback(self.store, self.skill_id)
        finally:
            skills.rollback = orig
        self.assertTrue(r["degraded"])
        self.assertFalse(r["rolled_back"])
        self.assertIn("rollback_unavailable", r.get("error", ""))

    def test_no_dead_code_path(self):
        """旧实现用同一个 ev 判退化，回滚分支恒不触发。

        现在 evaluate_and_promote 里**没有**任何基于 ev 的回滚判断：
        晋升成功就停在 active，退化只由 monitor_and_rollback 依据线上指标触发。
        """
        import inspect
        src = inspect.getsource(SS.evaluate_and_promote)
        self.assertNotIn("_detect_regression", src,
                         "evaluate_and_promote 仍在用评估结果判『线上』退化")
        # 晋升之后、没有任何线上数据时，状态必须停在 active
        row = [x for x in skills.list_skills(self.store)
               if x["id"] == self.skill_id][0]
        self.assertEqual(row["status"], "active")

class TestV1DemoteAndAttribution(unittest.TestCase):
    """EVO-LOOP-01：v1 退化要能止血；归因要精确。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.Store(os.path.join(self.tmp.name, "v1.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.steps = [
            {"seq": 1, "logical": 1, "name": "file.read", "status": "error",
             "error_type": "timeout", "output": None},
            {"seq": 2, "logical": 2, "name": "http.fetch", "status": "ok",
             "output": {"docs": ["ok"]}},
        ]
        s = SS.synthesize(self.store, self.steps, task_type="newdomain")
        assert s["accepted"]["accepted"], s["accepted"]
        self.skill = s["skill"]
        self.sid = self.skill["id"]
        # 制造越权副作用所需的扫描表
        import security as SEC
        SEC.ensure_schema(self.store)

    def _promote_once(self):
        training = newdomain_cases("tr", 3, "file")
        target = newdomain_cases("tg", 3, "http")
        guard = newdomain_cases("gd", 3, "sql")
        def runner(case, sk):
            return True if case["task_id"].startswith("gd") else sk is not None
        out = SS.evaluate_and_promote(
            self.store, self.skill, target, guard, runner,
            training_cases=training)
        assert out["promoted"], out
        return out

    def _runs(self, n, *, success=True, skill_id=None, task="newdomain"):
        exp = self.store.create_experiment("v1", task, "T-v1",
                                          "tool_timeout", 0, 7)
        out = []
        for _ in range(n):
            run = self.store.create_run(exp["id"], "C", 7)
            rid = run["id"] if isinstance(run, dict) else run
            if skill_id:
                with self.store.conn() as c:
                    c.execute("UPDATE runs SET skill_id=? WHERE id=?",
                              (skill_id, rid))
            self.store.finish_run(rid, 1 if success else 0, "x", "", 10, 100)
            out.append(rid)
        return out

    def _unauthorized(self, rid):
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO security_effects(id,ts,run_id,where_,tool,"
                "detected,decision,action,attempted,executed,effect,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("e1", 1.0, rid, "tool_approval", "sql.update", 1, "deny",
                 "blocked", 1, 1, "state_changed", "越权写"))

    def test_v1_degradation_disables_for_stop_bleed(self):
        """v1（只有一版）线上越权：无旧快照可退，但必须 disable 止血。"""
        self._promote_once()
        # 确认确实只有一版可退
        with self.store.conn() as c:
            versions = [r[0] for r in c.execute(
                "SELECT version FROM skill_versions WHERE skill_id=?",
                (self.sid,)).fetchall()]
        self.assertLessEqual(len(versions), 2, "本用例要构造 v1 场景")

        rids = self._runs(6, success=True, skill_id=self.sid)
        self._unauthorized(rids[0])
        # 强制 rollback 抛「无更旧快照」来精确构造 v1 场景
        orig = skills.rollback
        def no_old(*a, **k):
            raise ValueError("当前指针 v1 之前没有更旧的快照")
        skills.rollback = no_old
        try:
            r = SS.monitor_and_rollback(self.store, self.sid)
        finally:
            skills.rollback = orig
        self.assertTrue(r["degraded"])
        self.assertFalse(r["rolled_back"])
        self.assertTrue(r["disabled"], r)
        self.assertEqual(r["status"], "disabled")
        row = [x for x in skills.list_skills(self.store)
               if x["id"] == self.sid][0]
        self.assertEqual(row["status"], "disabled")

    def test_exact_attribution_ignores_unrelated_failures(self):
        """同 task_type 下混入未启用本 skill 的失败运行，不误伤。"""
        self._promote_once()
        # 6 条本 skill 的成功运行 + 15 条无关失败运行（无 skill_id）
        self._runs(6, success=True, skill_id=self.sid)
        self._runs(15, success=False, skill_id=None)
        sk = [x for x in skills.list_skills(self.store)
              if x["id"] == self.sid][0]
        stats = SS.collect_online_stats(self.store, sk)
        self.assertEqual(stats["n"], 6,
                         "精确归因应只统计带本 skill_id 的运行")
        self.assertIn("skill_id", stats["association"])
        r = SS.monitor_and_rollback(self.store, self.sid)
        self.assertFalse(r["degraded"], "无关失败不应触发本 skill 回滚")

    def test_legacy_fallback_still_works(self):
        """旧库（runs 无 skill_id 归属）仍回退时间窗近似。"""
        self._promote_once()
        self._runs(8, success=False, skill_id=None)
        sk = [x for x in skills.list_skills(self.store)
              if x["id"] == self.sid][0]
        stats = SS.collect_online_stats(self.store, sk)
        self.assertGreaterEqual(stats["n"], 8)
        self.assertIn("回退", stats["association"])


class TestIterativeRefinement(unittest.TestCase):
    """NEXT-01：3 层嵌套规约逐轮暴露，必须累积学全而非只学第一条。"""

    def setUp(self):
        self.store = core.Store(os.path.join(
            tempfile.mkdtemp(prefix="refine-"), "x.db"))
        steps = [{"seq": 1, "logical": 1, "name": "code.run",
                  "status": "error", "error_type": "KeyError",
                  "output": {"stderr": "KeyError: 'user_name'"}}]
        out = SS.synthesize(self.store, steps, task_type="t",
                            require_info_gain=True)
        self.sid = out["skill"]["id"]
        skills.save(self.store, out["skill"])

    def _trace(self, stderr, seq):
        return {"seq": seq, "logical": seq, "name": "code.run",
                "status": "error", "error_type": "AssertionError",
                "output": {"stderr": stderr}}

    def test_three_rounds_accumulate_all_invariants(self):
        # 第 2 层：分页从 1 开始
        r2 = SS.refine_skill(
            self.store, self.sid,
            [self._trace("page must be 1-based: got 0", 2)],
            require_info_gain=True)
        self.assertTrue(r2["accepted"]["accepted"], r2["accepted"])
        self.assertEqual(len(r2["added"]), 1)
        # 第 3 层：时间必须 UTC Z
        r3 = SS.refine_skill(
            self.store, self.sid,
            [self._trace("timestamp must be ISO8601 UTC ending with Z", 3)],
            require_info_gain=True)
        self.assertTrue(r3["accepted"]["accepted"], r3["accepted"])
        doms = {x["domain"] for x in r3["skill"]["env_invariants"]}
        self.assertEqual(len(doms), 3, "三轮后应学全 3 个域的规约：%s" % doms)
        self.assertEqual(r3["version"], 3)

    def test_immutable_snapshots_and_rollback_to_v1(self):
        r2 = SS.refine_skill(
            self.store, self.sid,
            [self._trace("page must be 1-based: got 0", 2)])
        self.assertTrue(r2["accepted"]["accepted"])
        v1 = SS.latest_snapshot(self.store, self.sid)
        self.assertEqual(len(v1["env_invariants"]), 2)
        sk = skills.rollback(self.store, self.sid, to_version=1)
        self.assertIsNotNone(sk)
        # 回滚后最新版本快照仍是 v2（旧快照不可变、可再次回滚）
        self.assertEqual(
            len(SS.latest_snapshot(self.store, self.sid)["env_invariants"]), 2)

    def test_duplicate_rule_is_not_added_twice(self):
        tr = [self._trace("KeyError: 'user_name'", 9)]
        r = SS.refine_skill(self.store, self.sid, tr)
        # 同规则即使再次出现也不能重复累积
        self.assertFalse(r["accepted"]["accepted"])
        self.assertEqual(r["added"], [])

    def test_no_evidence_is_rejected(self):
        r = SS.refine_skill(
            self.store, self.sid,
            [self._trace("something went wrong, please be careful", 4)])
        self.assertFalse(r["accepted"]["accepted"])
