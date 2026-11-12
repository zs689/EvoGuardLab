# -*- coding: utf-8 -*-
"""可复现性：同一配置重跑，报告必须逐字节一致。

仓库对外最硬的主张是「换个时间、换台机器重跑，数字不变」。此前只有
fingerprint 的确定性测试，没人守整条链路。这里用子进程跑两次脚本，
比对产出的 json 字节 —— 任何时间戳、字典序、随机端口、set 迭代顺序
漏进产物都会被这条卡住。

注意：产物里不许出现输出目录的绝对路径，否则「同一实验写两个目录」
这种最基本的事都会破坏可比性。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")


def _run(script, out):
    env = dict(os.environ)
    env.pop("AGENTLAB_SECURITY_MODE", None)   # 环境不许影响默认档
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script),
                        "--out", out],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("%s 退出码 %s\nstdout:%s\nstderr:%s"
                             % (script, r.returncode, r.stdout[-2000:],
                                r.stderr[-2000:]))
    return r


def _files(out):
    return sorted(f for f in os.listdir(out)
                  if f.endswith((".json", ".md")))


def _read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


class TestReproducibleArtifacts(unittest.TestCase):
    def _twice(self, script, expect):
        """两个独立目录各跑一次，返回 {文件名: 字节}。"""
        outs = [tempfile.mkdtemp(prefix="repro_a_"),
                tempfile.mkdtemp(prefix="repro_b_")]
        for o in outs:
            _run(script, o)
        got = [dict((f, _read(os.path.join(o, f)))
                    for f in _files(o)) for o in outs]
        self.assertEqual(sorted(got[0]), sorted(expect),
                         "%s 产物清单不符合预期" % script)
        return got

    def test_defense_demo_is_byte_identical_across_runs(self):
        a, b = self._twice("defense_demo.py", ["demo.json", "demo.md"])
        for name in ("demo.json", "demo.md"):
            self.assertEqual(a[name], b[name],
                             "%s 两次运行内容不同：可复现性被破坏" % name)

    def test_freeze_mode_compare_is_byte_identical(self):
        a, b = self._twice("freeze_mode_compare.py",
                           ["compare.json", "compare.md", "manifest.json"])
        for name in ("compare.json", "compare.md", "manifest.json"):
            self.assertEqual(a[name], b[name],
                             "%s 两次运行内容不同" % name)

    def test_ablation_is_byte_identical(self):
        a, b = self._twice("ablation_v1.py",
                           ["manifest.json", "report.md", "results.json"])
        for name in ("manifest.json", "report.md", "results.json"):
            self.assertEqual(a[name], b[name],
                             "%s 两次运行内容不同" % name)

    def test_real_settlement_demo_is_byte_identical(self):
        """审批 id 是 uuid、回环端口是随机的 —— 必须脱敏，否则不可重跑。"""
        a, b = self._twice("real_settlement_demo.py",
                           ["demo.json", "report.md"])
        for name in ("demo.json", "report.md"):
            self.assertEqual(a[name], b[name],
                             "%s 两次运行不同：随机 id/端口漏进产物" % name)
        text = a["report.md"].decode("utf-8")
        self.assertIn("<masked>", text)
        self.assertIn("127.0.0.1:<port>", text, "回环端口未脱敏")

    def test_e02_e03_and_label_registry_are_byte_identical(self):
        """E02 家族划分、E03 检索反例、外部标注清单：都不许有随机成分。"""
        cases = [("conditional_skill_eval.py",
                  ["manifest.json", "report.md", "results.json"]),
                 ("experience_scope_demo.py", ["demo.json", "report.md"]),
                 ("build_external_label_registry.py",
                  ["protocol.md", "registry.json"]),
                 ("eval_labeled.py", ["report.md", "result.json"]),
                 # 这条曾经把 p50/p95 耗时写进产物，两次重跑必然不同；
                 # 现在耗时只在终端显示，产物必须逐字节一致。
                 ("eval_jailbreak_prompts.py", ["report.md", "result.json"]),
                 # 动作-职责不符：纯正则，产物里不许有任何随机成分
                 ("eval_action_scope.py", ["report.md", "result.json"]),
                 # 真实中文语料：只统计 + 手法代号，不许把正文写进产物
                 ("eval_chinese_real.py", ["report.md", "result.json"])]
        # clean clone 上 data/external/ 被 .gitignore 排除，这三个脚本无语料可跑。
        # 只跳过它们，其余四个产物的字节一致性仍然必须验证（不静默整条跳过）。
        if not os.path.isfile(os.path.join(ROOT, "data", "external",
                                           "injecagent", "cases.jsonl")):
            need_corpus = {"eval_jailbreak_prompts.py",
                           "eval_action_scope.py",
                           "eval_chinese_real.py"}
            cases = [c for c in cases if c[0] not in need_corpus]
        for script, expect in cases:
            outs = [tempfile.mkdtemp(prefix="repro_a_"),
                    tempfile.mkdtemp(prefix="repro_b_")]
            for o in outs:
                _run(script, o)
            first, second = (_files(o) for o in outs)
            self.assertEqual(first, second, "%s 两次产物清单不同" % script)
            self.assertTrue(first, "%s 没产出任何文件" % script)
            if expect:
                self.assertEqual(sorted(first), sorted(expect), script)
            for name in first:
                self.assertEqual(_read(os.path.join(outs[0], name)),
                                 _read(os.path.join(outs[1], name)),
                                 "%s/%s 两次内容不同" % (script, name))

    def test_no_absolute_out_dir_leaks_into_artifacts(self):
        """产物不能内嵌输出目录，否则换个目录就不可比。"""
        out = tempfile.mkdtemp(prefix="repro_leak_")
        _run("defense_demo.py", out)
        _run("ablation_v1.py", out)
        for f in _files(out):
            text = _read(os.path.join(out, f)).decode("utf-8")
            self.assertNotIn(out, text, "%s 里写进了输出目录绝对路径" % f)
            self.assertNotIn(out.replace("\\", "/"), text,
                             "%s 里写进了输出目录绝对路径" % f)

    def test_committed_artifacts_have_no_dev_machine_paths(self):
        """已提交的产物里不能出现开发机绝对路径。

        压测/演练把异常原文写进产物，异常信息带完整路径，一不留神就
        把 `E:\\agent\\...` 或 `C:/Users/...` 带进仓库。这条守门扫一遍
        `experiments/` 下所有 json/md。
        """
        import glob
        import re
        bad = re.compile(r"([A-Za-z]:\\\\?[^\"\s]{0,40}(?:EvoGuardLab|Users|agent)"
                         r"|/home/[^\"\s]+)")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        exp = os.path.join(root, "experiments")
        offenders = []
        for path in glob.glob(os.path.join(exp, "**", "*.json"), recursive=True):
            name = os.path.relpath(path, root).replace("\\", "/")
            try:
                text = _read(path).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            m = bad.search(text)
            if m:
                offenders.append("%s -> %s" % (name, m.group(0)[:60]))
        self.assertEqual(offenders, [], "产物里出现本机路径：%s" % offenders)


class TestArtifactSelfConsistency(unittest.TestCase):
    """汇总数字必须等于逐 case 之和，不能在报告层面自己骗自己。"""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="repro_chk_")

    def _json(self, name):
        return json.loads(_read(os.path.join(self.out, name)).decode("utf-8"))

    def test_demo_summary_matches_rows(self):
        _run("defense_demo.py", self.out)
        d = self._json("demo.json")
        rows = d["results"]
        self.assertEqual(len(rows), d["case_count"] * len(d["modes"]))
        for mode, s in d["summary"].items():
            sub = [r for r in rows if r["mode"] == mode]
            self.assertEqual(len(sub), s["planned"])
            self.assertEqual(s["sink_hits"], sum(r["sink_hits"] for r in sub))
            self.assertEqual(s["safe_completed"],
                             sum(1 for r in sub if r["safe_completed"]))
            self.assertEqual(s["unauthorized_calls_executed"],
                             sum(r["unauthorized_call_executed"]
                                 for r in sub))

    def test_ablation_config_count_and_unique_fingerprints(self):
        _run("ablation_v1.py", self.out)
        res = self._json("results.json")
        man = self._json("manifest.json")
        runs = res["runs"]
        self.assertEqual(len(runs), 17,
                         "§10 应为 6 组 13 配置 + 严格 2×2 四配置 = 17")
        seen = {}
        for key, run in runs.items():
            seen.setdefault(run["ablation"], []).append(
                run["config_fingerprint"])
        for group, fps in seen.items():
            self.assertEqual(len(fps), len(set(fps)),
                             "%s 组里有两个配置指纹一样，等于没消融" % group)
        cf = man["config_fingerprints"]
        self.assertEqual(sorted(cf.values() if isinstance(cf, dict) else cf),
                         sorted(f for v in seen.values() for f in v))
        # 每个配置只改一个字段，否则不是单变量消融
        for key, run in runs.items():
            self.assertEqual(len(run["changed_field"].split(","))
                             if isinstance(run["changed_field"], str) else 1, 1)


if __name__ == "__main__":
    unittest.main()
