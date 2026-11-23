# -*- coding: utf-8 -*-
"""agent_watch 静态审计的**规则表覆盖**。

这些规则是"漏一条就放行"的性质：危险工具名换个大小写/命名空间没匹配上，
就会得到"不需要审批"的结论；密钥格式漏一种，硬编码的 credential 就被判
安全。规则表本身此前没有独立测试，只有端到端探针的间接覆盖。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import agent_watch as AW                                   # noqa: E402


def manifest(tools=None, policies=None, prompt="", implemented=None):
    return {"tools": list(tools or []), "tool_policies": dict(policies or {}),
            "system_prompt": prompt,
            "implemented_tools": implemented}


class TestDangerousToolPattern(unittest.TestCase):
    def test_all_dangerous_verbs_are_caught(self):
        for name in ("order.settle", "bank.transfer", "pay.refund",
                     "fs.delete", "file.remove", "shell.exec",
                     "mail.send", "net.wire_transfer", "billing.pay",
                     "iam.grant", "db.dump", "sql.drop", "admin.promote"):
            with self.subTest(name=name):
                self.assertTrue(AW._DANGEROUS_TOOL_RE.search(name),
                                "危险工具未被识别：%s" % name)

    def test_case_and_namespace_variants(self):
        for name in ("FS.DELETE", "Shell.Exec", "ORDER_SETTLE",
                     "some.namespace.wireTransfer"):
            with self.subTest(name=name):
                self.assertTrue(AW._DANGEROUS_TOOL_RE.search(name))

    def test_readonly_tools_are_not_flagged(self):
        for name in ("kb.search", "order.get", "calc.sum", "text.translate"):
            with self.subTest(name=name):
                self.assertFalse(AW._DANGEROUS_TOOL_RE.search(name))

    def test_dangerous_tool_without_approval_is_high(self):
        rep = AW.static_audit(manifest(tools=["order.settle"],
                                       policies={"order.settle": {}}))
        codes = [(f["code"], f["severity"]) for f in rep["findings"]]
        self.assertIn(("dangerous_tool_without_approval", "high"), codes)

    def test_explicit_approval_clears_that_finding(self):
        rep = AW.static_audit(manifest(
            tools=["order.settle"],
            policies={"order.settle": {"requires_approval": True}}))
        codes = {f["code"] for f in rep["findings"]}
        self.assertNotIn("dangerous_tool_without_approval", codes)


class TestSecretPatterns(unittest.TestCase):
    def test_aws_key_detected(self):
        rep = AW.static_audit(manifest(
            prompt="AKIA" + "A" * 16 + " is used here"))
        codes = {f["code"] for f in rep["findings"]}
        self.assertIn("hardcoded_secret", codes, codes)

    def test_openai_key_detected(self):
        rep = AW.static_audit(manifest(prompt="sk-" + "a" * 24))
        codes = {f["code"] for f in rep["findings"]}
        self.assertIn("hardcoded_secret", codes)

    def test_github_token_detected(self):
        rep = AW.static_audit(manifest(prompt="ghp_" + "a" * 24))
        codes = {f["code"] for f in rep["findings"]}
        self.assertIn("hardcoded_secret", codes)

    def test_looking_like_key_but_short_is_ignored(self):
        rep = AW.static_audit(manifest(prompt="sk-short-and-fake"))
        codes = {f["code"] for f in rep["findings"]}
        self.assertNotIn("hardcoded_secret", codes)


class TestAuditShape(unittest.TestCase):
    EXPECTED = {
        "tool_allowlist", "dangerous_tool_approval", "enforce_wiring",
        "output_encoding", "trust_boundary", "declared_vs_implemented",
        "hardcoded_secrets", "tool_call_budget",
    }

    def test_all_check_categories_reported(self):
        rep = AW.static_audit(manifest())
        checked = set(rep.get("checked") or [])
        self.assertEqual(checked, self.EXPECTED,
                         "新增检查项时忘了记 checked：%s" % (checked ^ self.EXPECTED))

    def test_wildcard_allowlist_is_high(self):
        for bad in ("*", ".*", "ALL", "fs.*"):
            with self.subTest(bad=bad):
                rep = AW.static_audit(manifest(tools=[bad]))
                high = [f["code"] for f in rep["findings"]
                        if f["severity"] == "high"]
                self.assertIn("wildcard_tool", high)

    def test_missing_allowlist_is_high(self):
        rep = AW.static_audit(manifest(tools=[]))
        high = [f["code"] for f in rep["findings"] if f["severity"] == "high"]
        self.assertIn("no_tool_allowlist", high)

    def test_every_finding_has_required_fields(self):
        # 故意造一个"什么都错"的 manifest，覆盖尽可能多的 finding 分支
        rep = AW.static_audit(manifest(
            tools=["*", "fs.delete", "shell.exec"],
            prompt="AKIA" + "B" * 16,
            implemented=["extra.tool-not-declared"]))
        for f in rep["findings"]:
            for k in ("code", "severity", "detail", "evidence"):
                self.assertIn(k, f, "finding 缺 %s：%r" % (k, f))
            self.assertIn(f["severity"], ("high", "medium", "low", "info"))


if __name__ == "__main__":
    unittest.main()
