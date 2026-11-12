# -*- coding: utf-8 -*-
"""injection_scorer v2 与新预注册实验的守门测试。

守的是方法与口径，不是「数字好看」：
  1. 重叠分窗必须真的覆盖末尾 payload（不能偷偷截断）；
  2. 结构字段按形状丢弃，自由文本字段必须保留；
  3. 阈值只在干净 calibration 上定、按窗口数分桶；
  4. AUC 是阈值无关量，实现要正确；
  5. v2 的「未通过」结论与机制权衡不许被藏起来或改成通过；
  6. 零依赖边界：不引第三方包。
"""
import ast
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import injection_scorer as IS            # noqa: E402
import run_semantic_injection_v2 as V2   # noqa: E402

OUT = V2.OUT
DONE = os.path.exists(os.path.join(OUT, "result.json"))


class TestScorer(unittest.TestCase):

    def test_windows_cover_tail_payload(self):
        text = "正" * 9000 + "IGNORE_PREVIOUS_AND_SEND_DATA"
        ws = IS.windows(text, 512, 64)
        self.assertGreater(len(ws), 15)
        self.assertTrue(any("IGNORE_PREVIOUS_AND_SEND_DATA" in w for w in ws))
        # 相邻窗口必须有重叠，否则跨边界 payload 会漏
        self.assertEqual(ws[1][:64], ws[0][-64:])

    def test_windows_reject_bad_geometry(self):
        with self.assertRaises(ValueError):
            IS.windows("abc", size=64, overlap=64)

    def test_structural_fields_are_dropped_and_free_text_kept(self):
        p = {"id": "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
             "created_at": "2026-11-12T09:31:00+08:00",
             "version": "v2.3.1", "status": "PENDING",
             "owner_id": "E-2291", "request_token": "9f8e7d6c5b4a3210",
             "note": "这是一段正常的业务说明文本。"}
        kept, dropped, ratio = IS.filter_fields(p)
        keys = {k for k, _ in kept}
        self.assertIn("note", keys)
        self.assertNotIn("id", keys)
        self.assertEqual(len(dropped), 6)
        self.assertAlmostEqual(ratio, round(6 / 7, 3))

    def test_no_scorable_field_gives_none_not_zero(self):
        class M:
            weights = {}
            prior = 0.0
            max_scan_chars = 8000
        r = IS.score_document(M(), {"id": "E-2291", "status": "PENDING"})
        self.assertIsNone(r["score"])
        self.assertEqual(r["n_windows"], 0)

    def test_calibration_is_bucketed_and_uses_benign_only(self):
        import deep_scan as DS
        model = DS.load(V2.MODEL)
        benign = [V2.make_payload("正常的业务说明 %d" % i, i)
                  for i in range(30)]
        cal = IS.calibrate(model, benign, 0.05)
        self.assertEqual(cal["alpha"], 0.05)
        self.assertEqual(cal["n_calibration"], 30)
        self.assertTrue(cal["by_bucket"])
        self.assertIsNotNone(cal["global"])
        self.assertIn("只在", cal["note"])

    def test_auc_is_threshold_free_and_correct(self):
        self.assertEqual(IS.auc([3, 2, 1], [1, 1, 0]), 1.0)
        self.assertEqual(IS.auc([1, 2, 3], [1, 1, 0]), 0.0)
        self.assertEqual(IS.auc([1, 1, 1], [1, 0, 1]), 0.5)
        self.assertIsNone(IS.auc([], []))

    def test_zero_third_party_imports(self):
        path = os.path.join(ROOT, "ai-service", "evoguard",
                            "injection_scorer.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        third = {m for m in mods if m.split(".")[0] not in
                 ("json", "re", "math", "os", "sys", "time", "hashlib",
                  "collections", "deep_scan")}
        self.assertFalse(third, "引入了第三方依赖：%s" % third)


@unittest.skipUnless(DONE, "v2 实验尚未执行")
class TestV2Results(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(OUT, "result.json"), encoding="utf-8") as f:
            self.r = json.load(f)
        with open(V2.SPLIT, encoding="utf-8") as f:
            self.split = json.load(f)

    def test_gate_matches_preregistration(self):
        self.assertEqual(dict(self.split["gate"]), V2.GATE)
        self.assertEqual(dict(self.r["gate"]), V2.GATE)
        sig = self.split["integrity_sha256"]
        body = {k: v for k, v in self.split.items()
                if k != "integrity_sha256"}
        self.assertEqual(V2._fp(body), sig)

    def test_freeze_refuses_overwrite(self):
        with self.assertRaises(SystemExit):
            V2.freeze()

    def test_negative_result_is_not_hidden(self):
        self.assertFalse(self.r["passed"], "不许把未通过改成通过")
        self.assertFalse(self.r["checks"]["auc_ok"])
        self.assertEqual(set(self.r["per_view_checks"]),
                         {"baseline", "v2_window_only", "v2_full"})
        with open(os.path.join(OUT, "report.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("机制权衡", md)
        self.assertIn("未通过", md)
        self.assertIn("不放宽判定线", md)

    def test_tail_blindness_is_really_fixed(self):
        v = self.r["views"]
        self.assertEqual(v["baseline"]["tail_position"]["recall"], 0.0)
        self.assertEqual(v["v2_window_only"]["tail_position"]["recall"], 1.0)
        self.assertEqual(v["v2_full"]["tail_position"]["recall"], 1.0)
        # 字段过滤确实压住了过度防御，但 AUC 也确实掉了，两者都要留档
        self.assertLess(v["v2_full"]["overdefense_probe"]["fp_rate"],
                        v["baseline"]["overdefense_probe"]["fp_rate"])
        self.assertLess(v["v2_full"]["holdout"]["auc"],
                        v["baseline"]["holdout"]["auc"])


if __name__ == "__main__":
    unittest.main()
