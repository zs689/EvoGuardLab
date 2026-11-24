# -*- coding: utf-8 -*-
"""技能适用条件的环境维度：同一故障类型在不同环境（domain）下不该串用。

现状只有 agent + fault_type 两个维度。例子：`silent_corruption` 在
「金额单位=分」环境下的修复策略，挪到「时区格式」环境下是错的。
技能 body 里已有 env_invariants（带 domain），但 lookup 不校验它。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core                                                 # noqa: E402
import skills as SK                                         # noqa: E402


def _skill(env_domains):
    exp = {"id": "synth_env", "task_type": "react",
           "failure_type": "silent_corruption",
           "recovery_action": ["retry"], "fault_scope": "single"}
    sk = SK.propose(exp)
    sk["status"] = "active"
    sk["applies_when"] = {"agent": "react", "fault_type": "silent_corruption",
                          "env_domains": list(env_domains)}
    return sk


class TestEnvApplicability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="envapp-")
        self.store = core.Store(os.path.join(self.tmp.name, "e.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_matching_env_is_used(self):
        SK.save(self.store, _skill(["currency"]))
        r = SK.lookup(self.store, "react", "silent_corruption", "D",
                      active_domains={"currency"})
        self.assertIsNotNone(r, "环境匹配时应当命中")

    def test_mismatched_env_is_skipped(self):
        SK.save(self.store, _skill(["currency"]))
        r = SK.lookup(self.store, "react", "silent_corruption", "D",
                      active_domains={"timezone"})
        self.assertIsNone(r, "环境不匹配时不该串用")

    def test_no_env_declaration_still_matches(self):
        """没有环境声明的技能不受新维度影响（向后兼容）。"""
        SK.save(self.store, _skill([]))
        r = SK.lookup(self.store, "react", "silent_corruption", "D",
                      active_domains={"timezone"})
        self.assertIsNotNone(r, "无环境声明的技能应保持原行为")

    def test_no_active_domains_means_no_filter(self):
        """调用方没给当前环境信息时不额外过滤（避免误杀老调用点）。"""
        SK.save(self.store, _skill(["currency"]))
        r = SK.lookup(self.store, "react", "silent_corruption", "D")
        self.assertIsNotNone(r)


if __name__ == "__main__":
    unittest.main()


class TestSynthesizeWiresEnvDomains(unittest.TestCase):
    """端到端：从真实失败轨迹提炼的技能要自动带上环境域，并能按域过滤。"""

    FAIL = [{"seq": 1, "logical": 1, "kind": "tool", "name": "order.settle",
             "status": "error", "error_type": "ValueError",
             "output": "unit mismatch: expected cents got yuan"}]
    OK = [{"seq": 2, "logical": 1, "kind": "tool", "name": "order.settle",
           "status": "ok", "output": "ok"}]

    def test_synthesize_sets_env_domains(self):
        import skill_synthesis as SS
        r = SS.synthesize(None, list(self.FAIL), repair_steps=list(self.OK))
        sk = r["skill"]
        self.assertIn("unit", (sk.get("applies_when") or {}).get("env_domains", []),
                      sk.get("applies_when"))

    def test_synthesized_skill_not_used_in_other_env(self):
        import skill_synthesis as SS
        tmp = tempfile.TemporaryDirectory(prefix="syn-")
        self.addCleanup(tmp.cleanup)
        st = core.Store(os.path.join(tmp.name, "s.db"))
        self.addCleanup(st.close)
        r = SS.synthesize(None, list(self.FAIL), repair_steps=list(self.OK))
        sk = r["skill"]
        sk["id"] = "sk_env_e2e"
        sk["status"] = "active"
        SK.save(st, sk)
        self.assertIsNotNone(
            SK.lookup(st, sk["task_type"], sk["fault_type"], "D",
                      active_domains={"unit"}), "同环境应命中")
        self.assertIsNone(
            SK.lookup(st, sk["task_type"], sk["fault_type"], "D",
                      active_domains={"timezone"}), "跨环境不该串用")
