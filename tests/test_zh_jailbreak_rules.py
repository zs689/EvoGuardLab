# -*- coding: utf-8 -*-
"""中文越狱手法规则的检出下限与误报上限（防回归）。

真实数字（2026-11-23）：chinese direct_jailbreak 检出 0.471、
chinese_jailbreak 留出 0.452、151+31 条良性误报 0。
规则按**手法类别**写，不是照样本抄——两个不同领域的集表现一致就是证据。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import eval_chinese_real as E                             # noqa: E402


def _load(name):
    p = os.path.join(ROOT, "data", "external", name, "cases.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


class TestZhJailbreak(unittest.TestCase):
    def _rates(self, rows, cls=None):
        atk = [r for r in rows if r.get("label") == "attack"
               and (cls is None or r.get("class") == cls)]
        ben = [r for r in rows if r.get("label") != "attack"]
        det = sum(1 for r in atk if E._l1_hit(r["text"]))
        fp = sum(1 for r in ben if E._l1_hit(r["text"]))
        return (det / len(atk) if atk else 0.0), (fp / len(ben) if ben else 0.0)

    def test_direct_jailbreak_recall_floor(self):
        rows = _load("chinese")
        rec, _ = self._rates(rows, "direct_jailbreak")
        self.assertGreaterEqual(rec, 0.40,
                                "中文越狱检出跌破 0.40：实测 %.3f" % rec)

    def test_heldout_recall_floor(self):
        rows = _load("chinese_jailbreak")
        rec, _ = self._rates(rows)
        self.assertGreaterEqual(rec, 0.40,
                                "留出集检出跌破 0.40：实测 %.3f" % rec)

    def test_no_fp_on_all_chinese_benign(self):
        for name in ("chinese", "chinese_jailbreak"):
            _, fpr = self._rates(_load(name))
            self.assertEqual(fpr, 0.0, "%s 良性误报 %.3f" % (name, fpr))

    def test_two_sets_consistent(self):
        """两集同手法不同领域，检出率不应差太多（证明是按手法泛化）。"""
        a, _ = self._rates(_load("chinese"), "direct_jailbreak")
        b, _ = self._rates(_load("chinese_jailbreak"))
        self.assertLess(abs(a - b), 0.15,
                        "两集检出差 %.3f，疑似过拟合单一样本" % abs(a - b))


if __name__ == "__main__":
    unittest.main()
