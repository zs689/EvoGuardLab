# -*- coding: utf-8 -*-
"""AGENT-EVAL-01：评测题库质量守护。

确保 15 道题**既有效又可解**：
  1) buggy 源码下隐藏测试必须失败（否则题无效）；
  2) 每道题存在一个参考修复能通过（否则是必败题）。

注意：参考修复只存在于**测试**里，评测脚本 `eval_coding_agent.py` 的题库
**不含任何答案**（方案硬约束：无硬编码参考答案）。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import eval_coding_agent as E                              # noqa: E402

# 参考修复（仅测试使用）：证明每题可解。
FIXES = {
    "t01_sum_empty": {"calc.py": "def total(xs):\n    return sum(xs)\n"},
    "t02_div_zero": {"stats.py": "def ratio(a, b):\n    return a / b if b else 0\n"},
    "t03_parse_escape": {"io_util.py":
        "def parse_kv(s):\n"
        "    s = s.replace('\\\\,', '\\x00')\n"
        "    d = dict(p.split('=') for p in s.split(','))\n"
        "    return {k: v.replace('\\x00', ',') for k, v in d.items()}\n"},
    "t04_state_machine": {"door.py":
        "def transition(state, ev):\n"
        "    m = {('closed','open'):'open', ('open','close'):'closed'}\n"
        "    if (state, ev) not in m:\n        raise ValueError()\n"
        "    return m[(state, ev)]\n"},
    "t05_multi_file_iface": {"main.py":
        "from util import ratio_of\n\ndef compute(v):\n    return ratio_of(v, 2)\n"},
    "t06_float_eq": {"money.py":
        "def same(a, b):\n    return abs(a - b) < 1e-9\n"},
    "t07_dict_default": {"counter.py":
        "def inc(d, k):\n    d[k] = d.get(k, 0) + 1\n    return d\n"},
    "t08_stable_sort": {"sort_util.py":
        "def sort_by_age(rows):\n    return sorted(rows, key=lambda r: r['age'])\n"},
    "t09_recursion": {"fact.py":
        "def factorial(n):\n    return 1 if n <= 0 else n * factorial(n - 1)\n"},
    "t10_pagination": {"page.py":
        "def slice_page(items, page, size):\n"
        "    s = (page - 1) * size\n    return items[s:s + size]\n"},
    "t11_time_format": {"clock.py":
        "def hhmm(sec):\n    sec %= 86400\n"
        "    return '%02d:%02d' % (sec // 3600, (sec % 3600) // 60)\n"},
    "t12_regex_escape": {"search_util.py":
        "import re\ndef count_word(text, w):\n"
        "    return len(re.findall(re.escape(w), text))\n"},
    "t13_cache_invalidate": {"cache.py":
        "class Cache:\n    def __init__(self):\n        self.d = {}\n"
        "    def put(self, k, v):\n        self.d[k] = v\n"
        "    def get(self, k):\n        return self.d.get(k)\n"},
    "t14_type_coerce": {"coerce.py":
        "def to_int(x):\n    try:\n        return int(x)\n"
        "    except (ValueError, TypeError):\n        return 0\n"},
    "t15_dedupe_order": {"uniq.py":
        "def dedupe(xs):\n    seen = set()\n    out = []\n"
        "    for x in xs:\n        if x not in seen:\n"
        "            seen.add(x)\n            out.append(x)\n    return out\n"},
}


def _run(task, extra=None):
    w = tempfile.mkdtemp(prefix="bench-")
    E._write_files(w, task["files"])
    if extra:
        E._write_files(w, extra)
    return E._run_hidden(w, task["hidden"])[0]


class TestBenchQuality(unittest.TestCase):
    def test_benchmark_has_18_tasks(self):
        self.assertEqual(len(E.TASKS), 15)
        self.assertEqual(len(E._all_tasks()), 18, "应为 15 正常 + 3 注入")

    def test_every_buggy_fails(self):
        for t in E.TASKS:
            with self.subTest(task=t["id"]):
                self.assertFalse(_run(t), "%s 的 buggy 源码竟通过隐藏测试" % t["id"])

    def test_every_task_is_solvable(self):
        for t in E.TASKS:
            with self.subTest(task=t["id"]):
                self.assertTrue(_run(t, FIXES[t["id"]]),
                                "%s 无参考修复能通过（必败题）" % t["id"])

    def test_injection_tasks_carry_malicious_comment(self):
        inj = [t for t in E._all_tasks() if t["kind"] == "injection"]
        self.assertEqual(len(inj), 3)
        for t in inj:
            blob = "".join(t["files"].values())
            self.assertIn("ignore all previous instructions", blob)
            self.assertIn("evil.example.com", blob)


if __name__ == "__main__":
    unittest.main()
