# -*- coding: utf-8 -*-
"""评审 R09 守门测试：基线指纹必须覆盖**防护逻辑本身**。

原来的 CODE_FILES 只有 11 个文件，security.py / approval.py / tool_contract.py /
semantic_guard.py / action_scope.py / scan_pipeline.py / code_guard.py 一个都不在。
后果很具体：把某个检测器改松，manifest 不变 —— 于是「已冻结基线」这句话是假的。

这里不只断言「清单里有 8 个名字」，而是**造出真实差异**：
  - 改一个文件的字节 → 指纹必须变；
  - 改一个安全开关 → config_hash 必须变；
  - 少一个安全模块 → 必须报错，不能静默跳过。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import baseline  # noqa: E402

SEC_MODULES = ("security.py", "approval.py", "tool_contract.py",
               "semantic_guard.py", "action_scope.py", "scan_pipeline.py",
               "code_guard.py", "execution.py", "deep_scan.py",
               "tool_rails.py")


def _fake_root():
    """复制一份最小可识别根目录：只含 CODE_FILES 与 CONFIG_FILES。"""
    root = tempfile.mkdtemp(prefix="bl_root_")
    for rel in baseline.CODE_FILES + baseline.CONFIG_FILES:
        src = os.path.join(ROOT, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    run_dir = os.path.join(root, "data", "runs", "r1")
    os.makedirs(run_dir, exist_ok=True)
    return root, run_dir


class TestCoverage(unittest.TestCase):
    def test_all_security_modules_are_in_the_manifest_list(self):
        listed = set(baseline.CODE_FILES)
        for m in SEC_MODULES:
            rel = "ai-service/evoguard/" + m
            self.assertIn(rel, listed, "%s 不在指纹清单里" % m)

    def test_discover_reports_every_listed_file(self):
        root, run_dir = _fake_root()
        files = baseline._discover(run_dir, root)
        code = {f["path"] for f in files if f["kind"] == "code"}
        for rel in baseline.CODE_FILES:
            if os.path.exists(os.path.join(ROOT, rel)):
                self.assertIn(rel, code)
        shutil.rmtree(root, ignore_errors=True)


class TestFingerprintRespondsToRisk(unittest.TestCase):
    def test_changing_one_security_file_changes_the_fingerprint(self):
        root, run_dir = _fake_root()
        a = baseline._fingerprint(baseline._discover(run_dir, root), {}, "cfg")
        target = os.path.join(root, "ai-service", "evoguard", "action_scope.py")
        with open(target, "a", encoding="utf-8") as f:
            f.write("\n# 把这条规则改松一点\n")
        b = baseline._fingerprint(baseline._discover(run_dir, root), {}, "cfg")
        self.assertNotEqual(a, b, "改了防护模块，指纹却没变 —— 混跑检查形同虚设")
        shutil.rmtree(root, ignore_errors=True)

    def test_changing_a_threshold_file_changes_the_fingerprint(self):
        root, run_dir = _fake_root()
        a = baseline._fingerprint(baseline._discover(run_dir, root), {}, "cfg")
        target = os.path.join(root, "config", "thresholds_v1.json")
        if not os.path.exists(target):
            self.skipTest("本地没有 thresholds_v1.json")
        with open(target, encoding="utf-8") as f:
            body = f.read()
        with open(target, "w", encoding="utf-8") as f:
            f.write(body.replace(":", ": ", 1))
        b = baseline._fingerprint(baseline._discover(run_dir, root), {}, "cfg")
        self.assertNotEqual(a, b, "改了阈值文件，指纹却没变")
        shutil.rmtree(root, ignore_errors=True)

    def test_changing_a_security_switch_changes_config_hash(self):
        clean = baseline.config_hash(baseline.default_config(ROOT))
        os.environ["AGENTLAB_SECURITY_INTENT"] = "1"
        try:
            dirty = baseline.config_hash(baseline.default_config(ROOT))
        finally:
            os.environ.pop("AGENTLAB_SECURITY_INTENT", None)
        self.assertNotEqual(clean, dirty,
                            "开了意图门禁却算同一个 config_hash：两种实验条件被混算")

    def test_prompt_change_is_visible(self):
        """提示词改了等于判定逻辑改了 —— 必须能进指纹。"""
        cfg = baseline.default_config(ROOT)
        fp = cfg.get("prompt_fingerprints") or {}
        self.assertIn("goal_alignment", fp)
        self.assertEqual(len(fp["goal_alignment"]), 64)


class TestMissingFileIsAnError(unittest.TestCase):
    def test_missing_module_refuses_to_freeze(self):
        root, run_dir = _fake_root()
        os.remove(os.path.join(root, "ai-service", "evoguard", "approval.py"))
        with self.assertRaises(baseline.BaselineMismatch) as cm:
            baseline._discover(run_dir, root)
        self.assertIn("approval.py", str(cm.exception))
        shutil.rmtree(root, ignore_errors=True)


class TestEffectiveConfigExport(unittest.TestCase):
    def test_export_lists_switches_with_unset_marker(self):
        env = baseline.effective_security_config({})
        self.assertEqual(env["AGENTLAB_SECURITY_MODE"], "<unset>")
        self.assertEqual(set(env), set(baseline.SECURITY_SWITCHES))

    def test_export_reflects_real_values(self):
        env = baseline.effective_security_config(
            {"AGENTLAB_SECURITY_MODE": "enforce", "AGENTLAB_EXEC_MODE": "controlled"})
        self.assertEqual(env["AGENTLAB_SECURITY_MODE"], "enforce")
        self.assertEqual(env["AGENTLAB_EXEC_MODE"], "controlled")


if __name__ == "__main__":
    unittest.main()
