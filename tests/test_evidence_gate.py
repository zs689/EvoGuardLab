# -*- coding: utf-8 -*-
"""WS-05：evidence_gate 迁入后的回归 + 输出侧 evidence_grounding 扫描器。

原 evidence-gate 的 12 项基准以内联脚本形式存在（run_eval.py），
这里迁成可重复运行的 unittest，并钉住迁移前修过的四个真 bug：
多引用误报 / 子串绕过 / 章节号误报 / preamble 栈污染。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import evidence_gate.gate as G                              # noqa: E402
import evidence_gate.corpus as C                            # noqa: E402
import evidence_gate.clause as CL                           # noqa: E402
import evidence_gate.tokenize as TK                         # noqa: E402
import tool_rails as TR                                     # noqa: E402

SEC = {
    "差旅#3.1": "3.1 住宿费标准\n一线城市每人每天 450 元，凭发票报销。",
    "差旅#2.1": "2.1 交通费标准\n高铁二等座按实报销，单程上限 5500 元。",
}


class TestMigration(unittest.TestCase):
    def test_all_six_modules_import(self):
        import evidence_gate as E
        for n in E.__all__:
            self.assertTrue(hasattr(E, n), n)

    def test_docx_module_has_no_leaked_handle(self):
        src = open(os.path.join(ROOT, "ai-service", "evoguard",
                                "evidence_gate", "docx_reader.py"),
                   encoding="utf-8").read()
        self.assertIn("with zipfile.ZipFile", src)
        self.assertIn("outlineLvl", src)   # WS-05 迁入时补的大纲级别

    def test_no_private_prompt_in_module(self):
        p = os.path.join(ROOT, "ai-service", "evoguard", "evidence_gate")
        self.assertFalse(os.path.isfile(os.path.join(p, "llm.py")),
                         "llm.py 不迁：统一用 evoguard.gateway")


class TestFourFixedBugs(unittest.TestCase):
    def test_multi_citation_union(self):
        r = G.check("根据[差旅#3.1][差旅#2.1]，住宿 450 元、交通 5500 元。", SEC)
        self.assertNotEqual(r["label"], "NUMBER_MISMATCH")

    def test_substring_number_tamper_caught(self):
        r = G.check("交通费上限[差旅#2.1] 是 550 元。", SEC)
        self.assertEqual(r["label"], "NUMBER_MISMATCH")

    def test_section_number_in_evidence(self):
        r = G.check("根据[差旅#3.1]的 3.1 条规定，住宿 450 元。", SEC)
        self.assertNotEqual(r["label"], "NUMBER_MISMATCH")

    def test_unigram_bridge_for_oral_query(self):
        self.assertGreater(TK.tokenize_weighted("住酒店")["住"], 0)
        self.assertGreater(TK.tokenize_weighted("住宿")["住"], 0)

    def test_real_tamper_still_caught(self):
        r = G.check("住宿费[差旅#3.1] 是 900 元。", SEC)
        self.assertEqual(r["label"], "NUMBER_MISMATCH")


class TestGroundingScanner(unittest.TestCase):
    RULES = {"kb.search": {"call": [],
                           "result": [{"scanner": "evidence_grounding"}]}}

    def _v(self, text):
        return TR.check_result(self.RULES, "kb.search",
                               {"answer": text, "evidence_sections": SEC})

    def test_correct_number_passes(self):
        self.assertEqual(self._v("根据[差旅#3.1]，住宿费 450 元")["findings"], [])

    def test_wrong_number_is_error(self):
        f = self._v("根据[差旅#3.1]，住宿费 999 元")["findings"]
        self.assertTrue(any(x["severity"] == "error" for x in f))

    def test_no_citation_is_warn_not_error(self):
        f = self._v("住宿费 999 元")["findings"]
        self.assertTrue(any(x["severity"] == "warn" for x in f))

    def test_no_evidence_source_reports_info(self):
        v = TR.check_result(self.RULES, "kb.search", {"answer": "随便说"})
        self.assertTrue(any(x["code"] == "evidence_no_source"
                            for x in v["findings"]))


if __name__ == "__main__":
    unittest.main()
