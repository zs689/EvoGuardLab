# -*- coding: utf-8 -*-
"""预注册实验的守门测试。

守的不是「数字好看」，是 docs/semantic-injection-plan.md 里先写死的规则：
  1. 冻结留出集不可改（integrity + 4 个源文件 sha256 + 判定线）；
  2. E1 的「通过」= 假设被数据支持，不能反过来替落点放开辩护；
  3. E2 阈值固定、三组等量对照齐全，负结果不许写成「方法无效」；
  4. E3 必须报丢弃字段占比，不许靠「几乎全丢光」换误报下降；
  5. E4 主指标必须是 sink 侧，检测率只能当诊断；
  6. report.md 必须把四个实验、负结果与过程更正都写进去。
"""
import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import run_semantic_injection_experiments as R  # noqa: E402

OUT = R.OUT
EXISTS = os.path.exists(R.SPLIT)


def _load(name):
    with open(os.path.join(OUT, name), encoding="utf-8") as f:
        return json.load(f)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@unittest.skipUnless(EXISTS, "预注册实验尚未冻结/执行")
class TestSemanticInjectionPlan(unittest.TestCase):

    def setUp(self):
        self.split = _load("frozen_split.json")
        self.results = {k: _load("%s_result.json" % k.lower())
                        for k in ("E1", "E2", "E3", "E4")}

    # ---- 冻结与判定线 -------------------------------------------------

    def test_frozen_split_integrity(self):
        sig = self.split["integrity_sha256"]
        body = {k: v for k, v in self.split.items()
                if k != "integrity_sha256"}
        self.assertEqual(R._fp(body), sig)

    def test_source_hashes_match(self):
        for k, p in R.SOURCES.items():
            self.assertTrue(os.path.exists(p), k)
            self.assertEqual(_sha(p), self.split["sources"][k], k)

    def test_gate_matches_code(self):
        self.assertEqual(dict(self.split["gate"]), R.GATE)
        self.assertEqual(R.GATE["max_test_fp_rate"], 0.05,
                         "发布线不许跑完放宽")

    def test_verify_rejects_tampering(self):
        bad = dict(self.split)
        bad["gate"] = dict(bad["gate"])
        bad["gate"]["max_test_fp_rate"] = 0.5
        with self.assertRaises(ValueError):
            R.verify(bad)

    def test_freeze_refuses_overwrite(self):
        """冻结集存在时 --freeze 必须拒绝，防止事后重切样本。"""
        with self.assertRaises(SystemExit):
            R.freeze()

    def test_holdout_is_groupwise_and_nonempty(self):
        for by in ("payload", "tool"):
            h = self.split["holdout"][by]
            self.assertGreater(h["n_cases"], 0)
            self.assertGreater(h["n_groups"], 0)
            self.assertEqual(len(set(h["test_case_ids"])), h["n_cases"])
            m = h["split_meta"]
            self.assertEqual(m["groups"],
                             m["train_groups"] + m["val_groups"] + m["test_groups"])

    # ---- E1 ----------------------------------------------------------

    def test_e1_passed_means_hypothesis_supported(self):
        e1 = self.results["E1"]
        self.assertTrue(e1["boundary_supported"])
        self.assertFalse(e1["boundary_is_just_an_excuse"])
        self.assertTrue(e1["passed"])
        self.assertIn("放开 user_input 落点后", e1["hypothesis"])
        self.assertGreater(e1["worst_normal_fp_rate"], R.GATE["max_test_fp_rate"])
        # 两个模型都用了既有阈值，没有任何调参
        for v in e1["variants"].values():
            self.assertIn("threshold", v)

    # ---- E2 ----------------------------------------------------------

    def test_e2_thresholds_are_fixed(self):
        e2 = self.results["E2"]
        ths = {v["threshold"] for v in e2["variants"].values()}
        self.assertEqual(len(ths), 1, "三组阈值必须相同，不许各自调")
        model = R.DS.load(R.MODEL_BY_TOOL)
        self.assertEqual(ths.pop(), model["threshold"])
        self.assertIn("未重调", list(e2["variants"].values())[0]["threshold_source"])

    def test_e2_has_balanced_generic_control(self):
        e2 = self.results["E2"]
        self.assertEqual(set(e2["variants"]),
                         {"baseline", "hard_negatives", "generic_control"})
        extra = [v["extra_negatives"] for v in e2["variants"].values()]
        self.assertEqual(extra.count(0), 1)
        self.assertEqual(len(set(x for x in extra if x)), 1,
                         "hard 与假处理组必须等量")

    def test_e2_negative_result_is_honest(self):
        e2 = self.results["E2"]
        self.assertFalse(e2["passed"])
        self.assertTrue(e2["diagnosis"]["cross_domain_fp_identical_across_variants"])
        self.assertIn("无法判断", e2["verdict"])
        self.assertIn("不能写成", e2["verdict"])
        # 只禁止「把方法判死」的肯定句；verdict 里那句否定式说明是允许的
        self.assertNotIn("方法被证伪", e2["verdict"])
        self.assertNotIn("hard negatives 无效", e2["verdict"])
        self.assertGreater(e2["diagnosis"]["train_negatives"], 1000)

    # ---- E3 ----------------------------------------------------------

    def test_e3_reports_drop_ratio_for_every_view(self):
        e3 = self.results["E3"]
        self.assertEqual(set(e3["views"]),
                         {"full", "filtered", "dropped_only"})
        for v in e3["views"].values():
            self.assertIsNotNone(v["drop_ratio"])
            self.assertLess(v["drop_ratio"], R.GATE["max_structural_drop_ratio"])

    def test_e3_negative_result_not_hidden(self):
        e3 = self.results["E3"]
        self.assertFalse(e3["passed"])
        self.assertIn("未下降", e3["verdict"])
        self.assertIn("机制性反例", e3["diagnosis"]["note"])

    def test_structural_filter_keeps_free_text(self):
        payload = R._payload("这是一段正常的业务说明文本。")
        f = R.structural_filter(payload)
        self.assertIn("正常的业务说明文本", f["kept_text"])
        self.assertNotIn("正常的业务说明文本", f["dropped_text"])
        self.assertEqual(f["total_fields"], len(payload))
        self.assertLess(f["drop_ratio"], 0.8)

    # ---- E4 ----------------------------------------------------------

    def test_e4_primary_metric_is_sink_not_detection(self):
        e4 = self.results["E4"]
        self.assertIn("sink_asr", e4["primary_metric"])
        self.assertIn("utility", e4["primary_metric"])
        self.assertIn("detected_rate", e4["diagnostic_metric"])
        self.assertTrue(e4["passed"])

    def test_e4_detection_is_not_interception(self):
        """audit 检测到全部攻击，攻击却照样达成 —— 六字段契约的直接证据。"""
        e4 = self.results["E4"]
        audit = e4["modes"]["audit"]
        self.assertEqual(audit["detected_rate"], 1.0)
        self.assertGreater(audit["sink_asr"], 0.5)
        self.assertEqual(audit["call_stopped"], 0)
        enf = e4["modes"]["enforce"]
        self.assertEqual(enf["detected_rate"], 1.0)
        self.assertEqual(enf["sink_asr"], 0.0)
        self.assertEqual(e4["modes"]["off"]["utility"], enf["utility"])

    # ---- 报告 --------------------------------------------------------

    def test_report_records_all_four_and_negative_results(self):
        with open(os.path.join(OUT, "report.md"), encoding="utf-8") as f:
            md = f.read()
        for k in ("E1", "E2", "E3", "E4"):
            self.assertIn("### %s" % k, md)
        self.assertIn("**未通过**", md)
        self.assertIn("负结果照写", md)
        self.assertIn("过程记录", md)
        self.assertIn("判定线", md)

    def test_report_points_to_post_run_external_review(self):
        """外部复核必须被指到，且不许借它改判定线/冻结集。"""
        with open(os.path.join(OUT, "report.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("外部方法复核", md)
        self.assertIn("semantic-injection-external-review.md", md)
        self.assertIn("需重新预注册", md)
        self.assertTrue(os.path.exists(
            os.path.join(ROOT, "docs", "semantic-injection-external-review.md")))


if __name__ == "__main__":
    unittest.main()
