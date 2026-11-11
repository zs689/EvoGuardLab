# -*- coding: utf-8 -*-
"""人工标注分档报告的守门测试。

守的不是「kappa 好看」，是四条硬约束：
  1. 间接注入档必须**攻击 + 成对干净对照**一起算（少了对照，单档 kappa 恒为 0/None）；
  2. 顶层混合 kappa 不许被单独引用 —— note 里必须带警示；
  3. by_category 必须把「有害请求」与「间接注入」分开，且各档是对全体的一个划分；
  4. 单档里语料标签恒定 → kappa 无信息量，不能当标注质量结论。

背景：首轮把 60 条（含 12 条有害请求）混成一个二分类算 kappa 得 0.467，
被「页面问注入 vs 语料把有害请求也标 attack」的口径差压低。按入口分档后，
间接注入档一致率 0.878 / kappa 0.752，且不需要改动任何一条标签。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import human_labels as HL  # noqa: E402

EXISTS = os.path.exists(os.path.join(ROOT, "data", "external", "chinese", "cases.jsonl"))


@unittest.skipUnless(EXISTS, "中文语料未生成（跑 import_chinese_corpus.py 后才有）")
class TestByCategory(unittest.TestCase):

    def _labels(self, tmpdir, disturb=True):
        """造一份可复算的标签：默认与语料一致；disturb 时模拟真实口径差。"""
        _, attacks, benign = HL.load_cases()
        picked = HL.sample(attacks, HL.N_EACH) + HL.sample(benign, HL.N_EACH)
        lab = {r["case_id"]: ("attack" if r["label"] == "attack" else "benign")
               for r in picked}
        if disturb:
            # 模拟张硕按「注入/越狱」窄义理解：有害请求全标不是；
            # 4 条干净对照误报成攻击；1 条真间接注入漏判
            for cid in lab:
                if cid.startswith("zh_harm/"):
                    lab[cid] = "benign"
            clean = [c for c in lab if c.startswith("zh_ind_clean/")][:4]
            for c in clean:
                lab[c] = "attack"
            inj = [c for c in lab if c.startswith("zh_ind/") and
                   not c.startswith("zh_ind_clean/")][:1]
            for c in inj:
                lab[c] = "benign"
        p = os.path.join(tmpdir, "labels.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(lab, f, ensure_ascii=False)
        return p

    def _score(self, disturb=True):
        with tempfile.TemporaryDirectory() as td:
            return HL.score(self._labels(td, disturb), out=td)

    def test_injection_scope_is_paired(self):
        """少了干净对照，单档 kappa 恒等于 0 —— 必须成对算。"""
        self.assertEqual(HL.INJECTION_SCOPE, ("zh_ind", "zh_ind_clean"))
        r = self._score()
        self.assertGreater(r["injection_scope"]["cases"], 0)
        self.assertIsNotNone(r["injection_scope"]["cohen_kappa"])

    def test_by_category_partitions_all_cases(self):
        """各档 n 之和、分歧数之和必须等于全体，否则有人偷偷换了分母。"""
        r = self._score()
        self.assertEqual(sum(s["cases"] for s in r["by_category"].values()),
                         r["cases"])
        self.assertEqual(sum(s["disagreement_count"]
                             for s in r["by_category"].values()),
                         r["disagreement_count"])

    def test_harmful_request_and_injection_are_separate(self):
        """两类入口不能合并；分开报才能看出口径差压低的是哪一档。

        不绑具体数字（换语料就会变），只守关系：有害请求档几乎全分歧，
        而间接注入档明显更高 —— 这正是「不能混成一个 kappa」的实证。
        """
        r = self._score()
        cat = r["by_category"]
        self.assertIn("harmful_request", cat)
        self.assertIn("indirect_injection", cat)
        self.assertLess(cat["harmful_request"]["agreement_rate"], 0.2)
        self.assertGreater(r["injection_scope"]["agreement_rate"], 0.8)

    def test_top_level_kappa_carries_warning(self):
        """混合口径 kappa 不许被单独当成标注质量/准确率。"""
        r = self._score()
        self.assertIn("不能单独", r["note"])
        self.assertIn("injection_scope", r["note"])

    def test_single_category_kappa_is_uninformative(self):
        """单档语料标签恒定 → kappa 为 0/None，不能当结论。"""
        r = self._score()
        k = r["by_category"]["indirect_clean"]["cohen_kappa"]
        self.assertTrue(k is None or k == 0)

    def test_copying_corpus_gives_trivial_kappa_one(self):
        """照抄语料必然 kappa=1.0（恒等式），警示仍在，不许当「一致性提升」。"""
        r = self._score(disturb=False)
        self.assertEqual(r["cohen_kappa_vs_corpus"], 1.0)
        self.assertIn("不能单独", r["note"])


if __name__ == "__main__":
    unittest.main()
