# -*- coding: utf-8 -*-
"""真人双标注**真实产物**的守门测试。

`tests/test_double_labels.py` 守的是合并逻辑（纯 mock）；这里守的是
第二位标注者交回文件后落盘的三个结果，防止以后把它们写歪：
  1. 这是「人 vs 人」，不是「人 vs 语料」，更不是准确率；
  2. 140 条只有 B 标，不许混进 kappa 分母；
  3. 主口径是注入档（攻击 + 成对干净对照），不是顶层混合数；
  4. 同一人两次作答的稳定性是**另一个**指标，不许当双人 kappa；
  5. `harmful_request` 的口径差要在文档里如实说明。
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "experiments", "human_labels_v1")
EXISTS = os.path.exists(os.path.join(OUT, "double_result.json"))


def _load(name):
    with open(os.path.join(OUT, name), encoding="utf-8") as f:
        return json.load(f)


@unittest.skipUnless(EXISTS, "第二位标注者的结果尚未回收")
class TestDoubleAnnotationResults(unittest.TestCase):

    def setUp(self):
        self.main = _load("double_result.json")
        self.sheet60 = _load("double_result_60.json")
        self.intra = _load("intra_annotator_check.json")

    def test_metric_is_inter_annotator_not_accuracy(self):
        for r in (self.main, self.sheet60):
            self.assertIn("inter-annotator", r["metric"])
            self.assertIn("cohen_kappa_inter_annotator", r)
            self.assertNotIn("cohen_kappa_vs_corpus", r)
            self.assertIn("不是准确率", r["note"])
            self.assertIn("不是「人 vs 语料标签」", r["note"])

    def test_140_unpaired_never_enter_kappa_denominator(self):
        r = self.main
        self.assertEqual(r["expected_cases"], 200)
        self.assertEqual(r["cases"], 60)
        self.assertEqual(r["unpaired"]["only_b"], 140)
        self.assertEqual(r["unpaired"]["only_a"], 0)
        self.assertEqual(r["cases"] + r["unpaired"]["only_b"],
                         r["expected_cases"])
        self.assertTrue(r["warnings"])

    def test_injection_scope_is_primary(self):
        r = self.main
        s = r["injection_scope"]
        self.assertGreater(s["cases"], 0)
        self.assertLess(s["cases"], r["cases"])
        self.assertGreater(s["agreement_rate"], r["agreement_rate"])
        self.assertGreater(s["cohen_kappa"], r["cohen_kappa_inter_annotator"])
        # 两份页的注入档落在同一批 41 条上，结果应一致
        self.assertEqual(s["cases"], self.sheet60["injection_scope"]["cases"])
        self.assertEqual(s["cohen_kappa"],
                         self.sheet60["injection_scope"]["cohen_kappa"])

    def test_single_category_kappa_is_not_a_conclusion(self):
        """单档标签近乎常量，kappa 可为 0 / None / 负数，文档必须说明。"""
        cats = self.main["by_category"]
        self.assertLess(cats["harmful_request"]["agreement_rate"], 0.2)
        bad = [k for k, v in cats.items()
               if v["cohen_kappa"] is not None
               and not (0 <= v["cohen_kappa"] <= 1)]
        self.assertTrue(bad, "本轮应至少有一个无意义的单档 kappa")
        with open(os.path.join(ROOT, "docs", "double-annotation-results.md"),
                  encoding="utf-8") as f:
            md = f.read()
        self.assertIn("单档 kappa 一律无意义", md)

    def test_intra_annotator_is_a_different_metric(self):
        r = self.intra
        self.assertIn("intra-annotator", r["metric"])
        self.assertIn("cohen_kappa_same_annotator", r)
        self.assertIn("不是两位标注者之间", r["note"])
        self.assertNotIn("cohen_kappa_inter_annotator", r)
        self.assertEqual(r["cases"], 60)

    def test_harmful_request_scope_difference_is_documented(self):
        with open(os.path.join(ROOT, "docs", "double-annotation-results.md"),
                  encoding="utf-8") as f:
            md = f.read()
        self.assertIn("口径差", md)
        self.assertIn("140", md)
        self.assertIn("不能写", md)

    def test_docs_point_to_the_real_result(self):
        for rel in ("docs/capability-matrix.md", "README.zh.md", "README.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertIn("double-annotation-results.md", f.read(), rel)


if __name__ == "__main__":
    unittest.main()
