# -*- coding: utf-8 -*-
"""评测口径测试：哪些 run 该进成功率分母。

为什么单独测这个：把「模型答错」和「模型服务没给出答案」混在一个成功率里，
会让基础设施抖动看起来像 Agent 质量下降。这不是理论问题 ——
本项目实测踩过：Ollama 挂掉时，grammar 对照 6 次调用全是 E_LLM_UNREACHABLE，
报告却写成「0/6 严格 JSON」，等于宣称「模型格式合规率 0%」。

跑法（在 EvoGuardLab 目录下）：
    python -m unittest tests.test_judgeability -v
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import real_model_bench as B  # noqa: E402


def row(success=False, err=None, llm=(), bad=0):
    return {"success": success, "status": "FAILED" if not success else "SUCCEEDED",
            "error": err, "llm_errors": list(llm), "bad_model_output": bad,
            "latency_ms": 1, "tokens": 0}


class TestJudgeRun(unittest.TestCase):
    def test_success_is_passed(self):
        self.assertEqual(B.judge_run(row(success=True)), B.PASSED)

    def test_wrong_answer_is_failed(self):
        """模型给了错答案 → 必须计入分母。"""
        self.assertEqual(B.judge_run(row(success=False)), B.FAILED)

    def test_framework_exception_is_unjudgeable(self):
        """框架抛异常 = 评测没跑完，不能算 Agent 失败。"""
        self.assertEqual(B.judge_run(row(success=False, err="ValueError: x")),
                         B.UNJUDGEABLE)

    def test_llm_service_error_still_counts_as_failure(self):
        """模型报错但 run 跑完、有答案 → 仍算业务失败，不许剔出分母。

        这条断言是改过方向的。第一版写成「有 E_LLM_* 就是不可评」，
        结果实测 15 条失败被剔掉了分母 —— 而逐条查轨迹发现：
        那些 run 全都有答案，失败原因在注入的故障上（规则表无策略、计划本身错），
        模型那步报不报错结果都一样。用「服务抖了」洗白真实失败，比不修口径更糟。
        """
        self.assertEqual(B.judge_run(row(success=False, llm=["E_LLM_UNREACHABLE"])),
                         B.FAILED)

    def test_bad_model_output_still_counts(self):
        """模型吐出非 JSON 是模型质量问题，**不能**借「不可评」洗掉。"""
        self.assertEqual(B.judge_run(row(success=False, bad=1)), B.FAILED)

    def test_success_survives_transient_llm_error(self):
        """中途抖了一下但最后成功了 → 仍是 passed，不能反过来剔除。"""
        self.assertEqual(B.judge_run(row(success=True, llm=["E_LLM_HTTP"])), B.PASSED)


class TestAggregateRate(unittest.TestCase):
    """aggregate 的成功率分母必须只含可评样本，且保留老口径做对比。"""

    def _done(self, rows):
        cases, done = [], {}
        for i, r in enumerate(rows):
            c = {"suite": "single", "agent_type": "order", "task_id": "T%d" % i,
                 "fault_type": "tool_timeout", "seed": 7}
            cases.append(c)
            for m in B.MODES:
                if m == "D":
                    done[B.case_key(c, m)] = r
                else:
                    done[B.case_key(c, m)] = row(success=True)
        return done, cases

    def test_unjudgeable_removed_from_denominator(self):
        # 3 个成功 + 1 个业务失败 + 1 个框架异常（run 没跑完）
        rows = [row(success=True), row(success=True), row(success=True),
                row(success=False), row(success=False, err="ValueError: boom")]
        done, cases = self._done(rows)
        d = B.aggregate(done, cases, "single")["modes"]["D"]
        self.assertEqual(d["n"], 5)
        self.assertEqual(d["ok"], 3)
        self.assertEqual(d["failed"], 1)
        self.assertEqual(d["unjudgeable"], 1)
        self.assertEqual(d["rate"], 75.0, "可评口径 = 3/(3+1)")
        self.assertEqual(d["rate_raw"], 60.0, "老口径 = 3/5，保留做对比")
        self.assertEqual(d["judgeable_rate"], 80.0)

    def test_llm_error_not_removed_but_counted(self):
        """伴随模型报错的失败仍进分母，但要单独计数供审计。"""
        rows = [row(success=True), row(success=False, llm=["E_LLM_HTTP"])]
        done, cases = self._done(rows)
        d = B.aggregate(done, cases, "single")["modes"]["D"]
        self.assertEqual(d["failed"], 1)
        self.assertEqual(d["unjudgeable"], 0, "模型报错不算不可评")
        self.assertEqual(d["rate"], 50.0)
        self.assertEqual(d["failures_with_llm_error"], 1, "必须留下审计痕迹")

    def test_all_judgeable_when_no_service_errors(self):
        rows = [row(success=True), row(success=False)]
        done, cases = self._done(rows)
        d = B.aggregate(done, cases, "single")["modes"]["D"]
        self.assertEqual(d["judgeable_rate"], 100.0)
        self.assertEqual(d["rate"], d["rate_raw"], "没有不可评样本时两种口径必须一致")


if __name__ == "__main__":
    unittest.main()
