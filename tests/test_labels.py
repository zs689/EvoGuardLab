# -*- coding: utf-8 -*-
"""标注集评测的测试：指标算法 + 三个真实误报修复的回归。

这里测的不是「规则能抓多少」，而是：
  1. 指标算错没有（分母为 0 时不能给 0）；
  2. 已知的三处误报有没有被重新打开（空壳答案 / 中文改写 / 恢复重放）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service"))

from evoguard_inspect import config, labels, loaders            # noqa: E402
from evoguard_inspect.loaders import evoguard as eg            # noqa: E402
from evoguard_inspect.loaders import otel                      # noqa: E402
from evoguard_inspect.model import Step, Trace                 # noqa: E402
from evoguard_inspect.rules import trace_rules as R            # noqa: E402


def rec(label, flagged_error=False, flagged_warn=False, applicable=3, rules=None):
    return {"key": "k", "label": label, "flagged_error": flagged_error,
            "flagged_warn": flagged_warn or flagged_error,
            "error_count": 1 if flagged_error else 0, "warn_count": 0,
            "rules": rules or [], "applicable_error_rules": applicable}


class TestMetricMath(unittest.TestCase):
    def test_ratio_is_none_when_denominator_zero(self):
        self.assertIsNone(labels._ratio(0, 0))
        self.assertEqual(labels._ratio(1, 4), 0.25)

    def test_confusion_and_precision(self):
        rows = [rec("failed", True), rec("failed", False),
                rec("clean", False), rec("recovered", True)]
        s = labels.summarize(rows)
        self.assertEqual(s["confusion"], {"tp": 1, "fp": 1, "fn": 1, "tn": 1})
        self.assertEqual(s["precision"], 0.5)
        self.assertEqual(s["recall"], 0.5)

    def test_no_positive_predictions_gives_none_precision(self):
        """一条都没报时，precision 不是 1.0，而是「没算」。"""
        s = labels.summarize([rec("failed", False), rec("clean", False)])
        self.assertIsNone(s["precision"])
        self.assertEqual(s["recall"], 0.0)

    def test_unjudgeable_is_excluded_from_denominators(self):
        rows = [rec("failed", True), rec("clean", False, applicable=0)]
        s = labels.summarize(rows)
        self.assertEqual(s["n_unjudgeable"], 1)
        self.assertEqual(s["n_judged"], 1)
        self.assertEqual(s["by_label"]["clean"]["n"], 0)

    def test_recovered_has_its_own_false_alarm_bucket(self):
        s = labels.summarize([rec("recovered", True), rec("clean", False)])
        self.assertEqual(s["by_label"]["recovered"]["false_alarm_rate"], 1.0)
        self.assertEqual(s["by_label"]["clean"]["false_alarm_rate"], 0.0)

    def test_is_flagged_respects_severity(self):
        issues = [{"severity": "warn"}]
        self.assertTrue(labels.is_flagged(issues, "warn"))
        self.assertFalse(labels.is_flagged(issues, "error"))


class TestEmptyAnswerNormalization(unittest.TestCase):
    """空壳答案曾导致 32/32 正常轨迹被判「没有答案」。"""

    def test_otel_unwrap_treats_shell_as_none(self):
        self.assertIsNone(otel.unwrap_answer({"parsed": None, "text": ""}))

    def test_otel_unwrap_keeps_real_answer(self):
        self.assertEqual(otel.unwrap_answer({"order_id": "A1001", "total": 504.0}),
                         {"order_id": "A1001", "total": 504.0})
        self.assertEqual(otel.unwrap_answer({"text": "退款 3 天"}), {"text": "退款 3 天"})

    def test_store_answer_shell_is_dropped(self):
        self.assertIsNone(eg._clean_answer('{"parsed": null, "text": ""}'))

    def test_store_answer_real_is_kept(self):
        self.assertEqual(eg._clean_answer('{"order_id": "A1001", "total": 504.0}'),
                         {"order_id": "A1001", "total": 504.0})


class TestFalsePositiveFixes(unittest.TestCase):
    def test_chinese_paraphrase_is_not_dependency_mismatch(self):
        """用户问「退货要几天内」，检索词是「退货时限」—— 这是改写，不是编造。"""
        self.assertTrue(R._related("退货时限", "退货要几天内"))
        self.assertFalse(R._related("A9999", "订单 A1001 明细"),
                         "纯 ASCII 的 ID 仍要按子串判，不能放过真错")

    def test_replay_steps_do_not_count_as_cycle(self):
        """恢复期有意重放，不是陷入循环。"""
        steps = [Step(seq=1, kind="tool", name="a", tool="a", args={"x": 1}, result="r"),
                 Step(seq=2, kind="tool", name="b", tool="b", args={"y": 1}, result="r"),
                 Step(seq=3, kind="replay", name="a", tool="a", args={"x": 1}, result="r"),
                 Step(seq=4, kind="replay", name="b", tool="b", args={"y": 1}, result="r")]
        tr = Trace(trace_id="t", agent="x", task="t", steps=steps, answer={"text": "ok"})
        ids = {i["rule_id"] for i in R.check_all([tr], config.load(None))}
        self.assertNotIn("cycle_detected", ids)

    def test_real_cycle_still_detected(self):
        steps = [Step(seq=1, kind="tool", name="a", tool="a", args={"x": 1}, result="r"),
                 Step(seq=2, kind="tool", name="b", tool="b", args={"y": 1}, result="r"),
                 Step(seq=3, kind="tool", name="a", tool="a", args={"x": 1}, result="r"),
                 Step(seq=4, kind="tool", name="b", tool="b", args={"y": 1}, result="r")]
        tr = Trace(trace_id="t", agent="x", task="t", steps=steps, answer={"text": "ok"})
        ids = {i["rule_id"] for i in R.check_all([tr], config.load(None))}
        self.assertIn("cycle_detected", ids)


class TestLabeledSetIntegrity(unittest.TestCase):
    def test_labels_file_matches_protocol(self):
        import json
        path = os.path.join(HERE, "fixtures", "labeled", "labels.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["protocol"], "trace-label-v2")
        self.assertGreaterEqual(len(doc["entries"]), 30)
        keys = [e["key"] for e in doc["entries"]]
        self.assertEqual(len(keys), len(set(keys)), "key 不能重复")
        for e in doc["entries"]:
            self.assertIn(e["label"], ("clean", "recovered", "failed", "unjudgeable"))
            self.assertTrue(os.path.exists(os.path.join(ROOT, e["file"])),
                            "轨迹文件不存在：%s" % e["file"])

    def test_every_trace_file_parses(self):
        import json
        path = os.path.join(HERE, "fixtures", "labeled", "labels.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        for e in doc["entries"]:
            traces = loaders.load_file(os.path.join(ROOT, e["file"]))
            self.assertTrue(traces, "%s 解析出 0 条轨迹" % e["key"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
