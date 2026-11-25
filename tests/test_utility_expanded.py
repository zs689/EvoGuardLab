# -*- coding: utf-8 -*-
"""检测评测统计意义：utility 必须并列「同源严格」与「扩充统计」两份。

同源 InjecAgent user_instruction 只有 17 条唯一，FPR=0 在 17 条上
没有统计意义。扩充用 jailbreak_classification 的公开良性（英文），
但**不混算**成一个数字。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

_EXT_JB = os.path.join(ROOT, "data", "external", "jailbreak", "cases.jsonl")


@unittest.skipUnless(os.path.isfile(_EXT_JB), "缺少外部公开语料 data/external/jailbreak")
class TestUtilityExpanded(unittest.TestCase):
    def test_load_expanded_benign_unique_ge_100(self):
        import eval_public_benchmarks as E
        rows = E.load_expanded_benign()
        self.assertGreaterEqual(len(rows), 100,
                                "扩充良性样本应 >=100 条唯一文本")
        texts = [r["text"] for r in rows]
        self.assertEqual(len(texts), len(set(texts)), "必须按唯一文本去重")

    def test_expanded_is_english_not_mixed_with_zh(self):
        """扩充集必须同语种（英文）配英文攻击集。"""
        import eval_public_benchmarks as E
        rows = E.load_expanded_benign()
        zh = [r for r in rows
              if any("\u4e00" <= c <= "\u9fff" for c in r["text"])]
        self.assertEqual(len(zh), 0, "英文攻击集不能配中文良性：%s"
                         % (zh[:2],))


if __name__ == "__main__":
    unittest.main()
