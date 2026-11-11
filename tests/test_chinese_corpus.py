# -*- coding: utf-8 -*-
"""中文语料 + 分类别评测的守门测试。

守的不是「数字好看」，是四条硬约束：
  1. 来源与许可必须在清单里写清楚（不写 = 不能用）；
  2. 报告与产物**不许出现攻击正文** —— 这类语料不该在仓库里传播；
  3. 分类别统计：越狱手法 / 有害请求 / 间接注入不能合并成一个召回数字；
  4. 中文正常文本不能被判成注入。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data", "external", "chinese")
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, SCRIPTS)

EXISTS = os.path.exists(os.path.join(DATA, "cases.jsonl"))


def _cases():
    with open(os.path.join(DATA, "cases.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _manifest():
    with open(os.path.join(DATA, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


@unittest.skipUnless(EXISTS, "本地未导入中文语料（data/external 不入库）")
class TestCorpusShape(unittest.TestCase):
    def test_manifest_declares_license_for_every_source(self):
        m = _manifest()
        self.assertTrue(m["sources"], "没有任何来源记录")
        for key, s in m["sources"].items():
            self.assertTrue(s.get("license"), "%s 没写许可" % key)
            self.assertTrue(s.get("repo"), "%s 没写仓库" % key)
            self.assertEqual(len(s.get("raw_sha256", "")), 64,
                             "%s 缺原始文件指纹，无法复核" % key)

    def test_total_is_at_least_one_hundred_distinct_attacks(self):
        """用户要求的底线：不少于 100 种不同的中文攻击样本。"""
        m = _manifest()
        self.assertGreaterEqual(m["distinct_attack_texts"], 100,
                                "不同的攻击文本不足 100 条")
        self.assertGreaterEqual(m["attacks"], 100)

    def test_indirect_injection_alone_has_at_least_one_hundred(self):
        """「自然语言间接注入」这一类本身也要够 100 条，不能靠别的类凑。"""
        n = sum(1 for c in _cases() if c["class"] == "indirect_injection")
        self.assertGreaterEqual(n, 100, "中文间接注入只有 %d 条" % n)

    def test_case_ids_are_unique(self):
        ids = [c["case_id"] for c in _cases()]
        self.assertEqual(len(ids), len(set(ids)), "case_id 重复，统计会重复计数")

    def test_attack_texts_are_distinct(self):
        sha = [c["text_sha256"] for c in _cases() if c["label"] == "attack"]
        self.assertEqual(len(sha), len(set(sha)), "有重复的攻击文本（改写不算独立样本）")

    def test_classes_are_declared(self):
        valid = {"direct_jailbreak", "harmful_request", "indirect_injection",
                 "indirect_clean", "benign"}
        for c in _cases():
            self.assertIn(c["class"], valid, c["case_id"])

    def test_lang_is_declared_and_non_chinese_is_rare(self):
        """上游确实混进了少量英文条目 —— 不许藏，但必须显式标注。"""
        rows = _cases()
        self.assertTrue(all(c.get("lang") for c in rows), "缺 lang 标注")
        other = [c for c in rows if c["lang"] != "zh"]
        self.assertLess(len(other) / len(rows), 0.02,
                        "非中文条目占比 %.1f%%，标注可能出错了"
                        % (100.0 * len(other) / len(rows)))

    def test_templates_are_instantiated(self):
        """评审 R08：带槽位的模板没实例化，样本就没法评测。"""
        for c in _cases():
            self.assertNotIn("{decoded_query}", c["text"], c["case_id"])


@unittest.skipUnless(EXISTS, "本地未导入中文语料")
class TestNoAttackBodyInArtifacts(unittest.TestCase):
    """最硬的一条：报告里不许出现攻击正文。"""

    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="zh_art_")
        r = subprocess.run([sys.executable,
                            os.path.join(SCRIPTS, "eval_chinese_real.py"),
                            "--out", cls.out],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]

    def test_artifacts_do_not_contain_attack_text(self):
        blob = ""
        for name in ("report.md", "result.json"):
            with open(os.path.join(self.out, name), encoding="utf-8") as f:
                blob += f.read()
        for r in _cases():
            if r["label"] != "attack":
                continue
            probe = r["text"].strip()[:24]
            if len(probe) < 12:
                continue
            self.assertNotIn(probe, blob,
                             "%s 的正文漏进了产物" % r["case_id"])

    def test_metrics_are_reported_per_class(self):
        with open(os.path.join(self.out, "result.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(set(d["classes"]),
                         {"direct_jailbreak", "harmful_request",
                          "indirect_injection"},
                         "三个任务必须分开出数字")
        for cls, v in d["classes"].items():
            self.assertIn("recall_case", v, cls)
            self.assertIn("macro_by_technique", v, cls)

    def test_case_level_numbers_are_self_consistent(self):
        with open(os.path.join(self.out, "result.json"), encoding="utf-8") as f:
            d = json.load(f)
        rows = _cases()
        for cls, v in d["classes"].items():
            n = sum(1 for c in rows if c["class"] == cls)
            self.assertEqual(v["cases"]["tp"] + v["cases"]["fn"], n,
                             "%s 的分母与语料条数对不上" % cls)
            if n:
                self.assertAlmostEqual(v["recall_case"],
                                       round(v["cases"]["tp"] / n, 4))

    def test_misses_keep_case_ids(self):
        with open(os.path.join(self.out, "result.json"), encoding="utf-8") as f:
            d = json.load(f)
        for cls, v in d["classes"].items():
            self.assertEqual(len(v["misses"]), v["cases"]["fn"],
                             "%s 漏检没有逐条留代号" % cls)


@unittest.skipUnless(EXISTS, "本地未导入中文语料")
class TestChineseBenignNotFlagged(unittest.TestCase):
    def test_benign_controls_are_not_flagged_by_l1(self):
        import security as S
        for r in _cases():
            if r["class"] != "benign":
                continue
            codes = [f["code"] for f in S.scan(r["text"], "user_input")
                     if f["code"].startswith("injection_")]
            self.assertEqual(codes, [], "中文正常文本被误报：%s" % r["case_id"])

    def test_indirect_clean_controls_have_no_scope_hit(self):
        import action_scope as A
        for r in _cases():
            if r["class"] != "indirect_clean":
                continue
            f = A.check_output_scope(r["tool"], r["text"])
            self.assertFalse(f and f["code"] == "action_on_read_only_tool",
                             "正常文档被判越界：%s（%s）"
                             % (r["case_id"], r["carrier"]))


@unittest.skipUnless(EXISTS, "本地未导入中文语料")
class TestCorpusFingerprint(unittest.TestCase):
    def test_manifest_hash_covers_cases_file(self):
        import hashlib
        with open(os.path.join(DATA, "cases.jsonl"), "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(got, _manifest()["cases_sha256"],
                         "语料被改动但清单未更新，实验数字将无法复核")

    def test_indirect_generator_is_deterministic(self):
        import zh_indirect_cases as Z
        a, sa = Z.build()
        b, sb = Z.build()
        self.assertEqual(sa, sb)
        self.assertEqual([c["text_sha256"] for c in a],
                         [c["text_sha256"] for c in b],
                         "生成器不确定，语料指纹每次都不一样")


if __name__ == "__main__":
    unittest.main()
