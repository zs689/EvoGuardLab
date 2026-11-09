# -*- coding: utf-8 -*-
"""S03 护栏：当前口径与冻结历史必须分得开。

推进计划 §4 S03：修改生成器并**新增当前版本报告**，保留冻结历史原件。
这里检查两件事：
  - 当前报告/生成器不再残留会改变理解的旧数字与旧结论；
  - 冻结历史原件仍是原样 —— 要改历史必须显式决定，不能悄悄覆盖。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestCurrentReports(unittest.TestCase):
    def test_labeled_report_uses_real_count(self):
        text = _read("experiments/labeled_eval_v2/report.md")
        with open(os.path.join(ROOT, "experiments", "labeled_eval_v2",
                               "result.json"), encoding="utf-8") as f:
            result = json.load(f)
        self.assertEqual(result["n_total"], 37)
        self.assertNotIn("49 条构造集", text)
        self.assertIn("在 %d 条构造集上" % result["n_total"], text)

    def test_labeled_report_states_permission_limit(self):
        text = _read("experiments/labeled_eval_v2/report.md")
        self.assertIn("不能用它判断越权", text.replace("**", ""))
        self.assertIn("权限专项", text)

    def test_assurance_report_no_legacy_claim(self):
        text = _read("experiments/assurance_v2/assurance.md")
        self.assertNotIn("只有声明", text)
        self.assertIn("真执行", text)
        self.assertIn("action", text)

    def test_eval_generator_has_no_hardcoded_49(self):
        src = _read("scripts/eval_labeled.py")
        self.assertNotIn("在 49 条构造集上", src)
        self.assertIn("% r[\"n_total\"]", src)
        self.assertIn("--out", src)

    def test_train_generator_uses_three_way_split(self):
        src = _read("ai-service/real_model_train.py")
        self.assertNotIn("dev task #1", src)
        self.assertNotIn("dev task #2", src)
        for name in ("dev_train_tasks", "dev_target_tasks", "dev_guard_tasks"):
            self.assertIn(name, src)

    def test_security_module_docstring_is_current(self):
        import security as S
        doc = S.__doc__ or ""
        self.assertNotIn("运行时并不强制执行", doc)
        self.assertIn("真实执行", doc)
        self.assertIn("S01", doc)

    def test_security_doc_no_longer_says_only_documentation(self):
        text = _read("docs/security.md")
        self.assertNotIn("前两项在本仓库里仍然只是文档与规则", text)
        self.assertIn("检测建议 ≠ 拦截成功", text)
        self.assertIn("security_effects", text)

    def test_assurance_doc_no_longer_says_no_labels(self):
        text = _read("docs/assurance.md")
        self.assertNotIn("人工标注集缺失", text)
        self.assertIn("37 条构造集", text)
        self.assertIn("Q02", text)


class TestFrozenHistoryUntouched(unittest.TestCase):
    """冻结原件保留旧描述，作为「当时就是这么写的」证据。"""

    def test_v1_labeled_report_still_49(self):
        self.assertIn("49 条", _read("experiments/labeled_eval/report.md"))

    def test_old_assurance_report_still_claims_declaration_only(self):
        self.assertIn("只有声明",
                      _read("experiments/p0_09_20261109/assurance/assurance.md"))

    def test_v1_demo_report_still_has_old_counts(self):
        with open(os.path.join(ROOT, "experiments", "defense_demo",
                               "demo.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["summary"]["enforce"]["enforced"], 4)
        self.assertEqual(d["summary"]["off"]["leaked"], 3)

    def test_old_train_report_still_uses_legacy_numbering(self):
        text = _read("experiments/real_model_20261109/train/train_report.md")
        self.assertIn("dev task #1", text)


if __name__ == "__main__":
    unittest.main()
