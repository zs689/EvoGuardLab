# -*- coding: utf-8 -*-
"""双标注（人 vs 人）守门测试。

守的不是「kappa 好看」，是五条硬约束：
  1. 两人必须标完全同一批 case_id（抽样确定性复算，map 必须恰好覆盖）；
  2. 匿名编号 r2_XXX 必须经 map 还原，映射缺失/越界拒绝出数；
  3. 缺条不静默（写进 warnings，按交集算）；
  4. 这是「人 vs 人」的标注者间 kappa，不是人 vs 语料、不是准确率；
  5. 完全照抄（且两列都含 0/1）会得到 kappa 1.0，note 必须点明
     「数据无法判别独立性」；若两列全常量，pe=1 分母为 0，kappa 必须是 None。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import human_labels as H          # noqa: E402
import score_double_labels as S   # noqa: E402

EXISTS = os.path.exists(os.path.join(
    ROOT, "data", "external", "chinese", "cases.jsonl"))


@unittest.skipUnless(EXISTS, "中文语料未生成")
class TestDoubleLabels(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.map_path = os.path.join(self.td.name, "map.json")
        self.a_path = os.path.join(self.td.name, "a.json")
        self.b_path = os.path.join(self.td.name, "b.json")
        self.out = os.path.join(self.td.name, "out.json")

        batch = S.expected_batch()
        self.anon = {("r2_%03d" % i): cid
                     for i, cid in enumerate(sorted(batch), 1)}
        self._write_json(self.map_path, {"anon_to_case_id": self.anon})
        # A 用真实 case_id，一半 attack / 一半 benign。
        # 不能全标成同一类：两列都是常量时 pe=1，kappa 分母为 0、根本无定义。
        self.a_real = {cid: ("attack" if i % 2 == 0 else "benign")
                       for i, cid in enumerate(sorted(batch))}
        self._write_json(self.a_path, self.a_real)

    @staticmethod
    def _write_json(path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def _b_copy_of_a(self):
        """B 完全照抄 A（经匿名编号还原），用于测「两人完全一致」的情形。"""
        return {anon: self.a_real[cid] for anon, cid in self.anon.items()}

    def _write_b(self, value_by_anon):
        self._write_json(self.b_path, value_by_anon)

    def _score(self):
        return S.score(self.a_path, self.b_path, self.map_path, self.out)

    def test_anon_ids_map_back_to_same_batch(self):
        """匿名编号必须能还原成与 A 完全同一批 id。"""
        self._write_b({k: "benign" for k in self.anon})
        r = self._score()
        self.assertEqual(r["cases"], r["expected_cases"])
        self.assertEqual(r["cases"], 60)
        self.assertEqual(set(self.anon.values()), S.expected_batch())

    def test_map_must_cover_exactly_the_batch(self):
        """map 被改坏（少一条）时拒绝出数，不猜。"""
        bad = dict(self.anon)
        bad.pop(next(iter(bad)))
        self._write_json(self.map_path, {"anon_to_case_id": bad})
        self._write_b({k: "benign" for k in self.anon})
        with self.assertRaises(ValueError):
            self._score()

    def test_unknown_anon_id_rejected(self):
        """出现 map 里没有的匿名编号，直接报错。"""
        self._write_b({"r2_999": "attack"})
        with self.assertRaises(ValueError):
            self._score()

    def test_extra_id_outside_batch_rejected(self):
        """B 发回不属于这一批的 id，拒绝（不同批次算不出 kappa）。"""
        self._write_b({"zh_benign/NOT_IN_BATCH": "benign"})
        with self.assertRaises(ValueError):
            self._score()

    def test_missing_items_are_not_silent(self):
        """B 只标了一部分：按交集算，但必须写 warning 且 received_b < expected。"""
        part = {k: "benign" for k in list(self.anon)[:40]}
        self._write_b(part)
        r = self._score()
        self.assertEqual(r["cases"], 40)
        self.assertEqual(r["received_b"], 40)
        self.assertTrue(r["warnings"])

    def test_inter_annotator_not_corpus_not_accuracy(self):
        """口径必须写死：人 vs 人，不是人 vs 语料，不是准确率。"""
        self._write_b({k: "benign" for k in self.anon})
        r = self._score()
        self.assertIn("inter-annotator", r["metric"])
        self.assertIn("两位真人标注者之间", r["note"])
        self.assertIn("不是「人 vs 语料标签」", r["note"])
        self.assertIn("不是准确率", r["note"])
        # 不能出现 cohen_kappa_vs_corpus 这种混淆字段
        self.assertNotIn("cohen_kappa_vs_corpus", r)

    def test_perfect_copy_is_trivial_and_flagged(self):
        """两人完全一致 → kappa 1.0，但 note 必须说清数据无法判别抄没抄。"""
        self._write_b(self._b_copy_of_a())
        r = self._score()
        self.assertEqual(r["agreement_rate"], 1.0)
        self.assertEqual(r["cohen_kappa_inter_annotator"], 1.0)
        self.assertIn("也可能是", r["note"])
        self.assertIn("标注独立性", r["note"])

    def test_all_constant_labels_have_no_kappa(self):
        """整批全标同一类 → pe=1 分母为 0，kappa 必须是 None，不是 1.0 也不是 0。

        这是 cohen_kappa() 的既有约定（human_labels.py 注释：分母为 0 返回 None，
        不写 0）。若这里偷偷返回 1.0，就等于把「两人都只按了一个按钮」报成完美一致。
        """
        self._write_json(self.a_path,
                         {cid: "benign" for cid in S.expected_batch()})
        self._write_b({k: "benign" for k in self.anon})
        r = self._score()
        self.assertIsNone(r["cohen_kappa_inter_annotator"])
        self.assertEqual(r["agreement_rate"], 1.0)
        self.assertIn("分母为 0", r["note"])

    def test_by_category_partitions_all_cases(self):
        """分档 n 之和、分歧数之和必须等于全体，防止偷偷换分母。"""
        vals = {k: ("attack" if i < 7 else "benign")
                for i, k in enumerate(self.anon)}
        self._write_b(vals)
        r = self._score()
        self.assertEqual(sum(s["cases"] for s in r["by_category"].values()),
                         r["cases"])
        self.assertEqual(sum(s["disagreement_count"]
                             for s in r["by_category"].values()),
                         r["disagreement_count"])
        self.assertGreater(r["injection_scope"]["cases"], 0)

    def test_score_writes_result_file(self):
        self._write_b({k: "benign" for k in self.anon})
        self._score()
        self.assertTrue(os.path.exists(self.out))
        with open(self.out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["cases"], 60)

    def test_ext_batch_pairs_only_on_core_and_reports_unpaired(self):
        """加长批 200 条：A 只有首轮 60 条时，kappa 只按 60 条交集算。

        多出的 140 条必须显式记成 unpaired，不能偷偷混进分母充数。
        """
        ext = S.expected_batch(100)
        core = S.expected_batch()
        self.assertEqual(len(ext), 200)
        self.assertLessEqual(core, ext)   # 加长批必然包含首轮那 60 条
        anon_ext = {"r2_%03d" % i: cid
                    for i, cid in enumerate(sorted(ext), 1)}
        self._write_json(self.map_path, {"anon_to_case_id": anon_ext})
        self._write_json(self.a_path,
                         {cid: self.a_real[cid] for cid in core})
        self._write_b({k: "benign" for k in anon_ext})
        r = S.score(self.a_path, self.b_path, self.map_path, self.out,
                    n_each=100)
        self.assertEqual(r["expected_cases"], 200)
        self.assertEqual(r["cases"], 60)
        self.assertEqual(r["unpaired"]["only_b"], 140)
        self.assertEqual(r["unpaired"]["only_a"], 0)
        self.assertTrue(r["warnings"])

    def test_a_can_use_its_own_anon_map(self):
        """两位都用匿名页（各自独立编号）时，map_a 能还原并配成全批 200 条。"""
        ext = S.expected_batch(100)
        b_anon = {"r2_%03d" % i: cid
                  for i, cid in enumerate(sorted(ext), 1)}
        a_anon = {"r2_%03d" % i: cid
                  for i, cid in enumerate(sorted(ext, reverse=True), 1)}
        self.assertNotEqual(a_anon, b_anon)   # 编号互不对应，不泄题
        map_b = os.path.join(self.td.name, "map_b.json")
        map_a = os.path.join(self.td.name, "map_a.json")
        self._write_json(map_b, {"anon_to_case_id": b_anon})
        self._write_json(map_a, {"anon_to_case_id": a_anon})
        self._write_json(self.a_path, {k: "benign" for k in a_anon})
        self._write_b({k: "benign" for k in b_anon})
        r = S.score(self.a_path, self.b_path, map_b, self.out,
                    n_each=100, map_a_path=map_a)
        self.assertEqual(r["expected_cases"], 200)
        self.assertEqual(r["cases"], 200)
        self.assertEqual(r["unpaired"]["only_b"], 0)
        self.assertEqual(r["unpaired"]["only_a"], 0)

    def test_mismatched_map_a_rejected(self):
        """--map-a 与 --n 不配套时直接拒绝，不能拿旧 60 条表配 200 条批。"""
        ext = S.expected_batch(100)
        b_anon = {"r2_%03d" % i: cid
                  for i, cid in enumerate(sorted(ext), 1)}
        map_b = os.path.join(self.td.name, "map_b.json")
        self._write_json(map_b, {"anon_to_case_id": b_anon})
        self._write_b({k: "benign" for k in b_anon})
        with self.assertRaises(ValueError):
            S.score(self.a_path, self.b_path, map_b, self.out,
                    n_each=100, map_a_path=self.map_path)  # self.map_path 是 60 条表


if __name__ == "__main__":
    unittest.main()
