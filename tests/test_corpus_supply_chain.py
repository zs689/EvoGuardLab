# -*- coding: utf-8 -*-
"""EG-018：语料锁清单能发现篡改，且不把「未导入」当成失败。"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import verify_corpus as VC                                  # noqa: E402


def make_source(root, name, *, license="MIT", commit="a" * 40,
                lines=3):
    d = os.path.join(root, "data", "external", name)
    os.makedirs(d, exist_ok=True)
    manifest = {
        "repo": "example/repo",
        "license": license,
        "license_url": "https://example.invalid/LICENSE",
        "files": [{"path": "a.py", "sha256": "0" * 64, "bytes": 10}],
    }
    if commit is not None:
        manifest["commit"] = commit
    with io.open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    with io.open(os.path.join(d, "cases.jsonl"), "w", encoding="utf-8") as f:
        for i in range(lines):
            f.write(json.dumps({"i": i}) + "\n")
    return d


class TestCorpusSupplyChain(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="evo-corpus-")

    def test_missing_corpus_is_not_a_failure(self):
        ok, rep = VC.verify(self.root)
        self.assertTrue(ok, "没有语料不能判失败")
        self.assertTrue(rep["not_imported"])

    def test_pinned_and_licensed_passes_strictly(self):
        make_source(self.root, "good")
        VC.write_lock(self.root)
        ok, rep = VC.verify(self.root)
        self.assertTrue(ok, rep["problems"] + rep["warnings"])
        self.assertEqual(rep["warnings"], [])
        self.assertEqual(rep["entries"][0]["pin_status"], "pinned")

    def test_tampered_cases_are_detected(self):
        d = make_source(self.root, "x")
        VC.write_lock(self.root)
        with io.open(os.path.join(d, "cases.jsonl"), "a", encoding="utf-8") as f:
            f.write('{"i": 99}\n')
        ok, rep = VC.verify(self.root)
        self.assertFalse(ok, "改了语料必须被发现")
        self.assertTrue(any("哈希" in p or "行数" in p for p in rep["problems"]))

    def test_missing_license_is_a_warning_not_a_tamper(self):
        make_source(self.root, "nolicense", license=None)
        VC.write_lock(self.root)
        ok, rep = VC.verify(self.root)
        self.assertTrue(ok, "缺许可不代表被篡改")
        self.assertTrue(any("license" in w for w in rep["warnings"]))

    def test_unpinned_commit_is_a_warning(self):
        make_source(self.root, "nopin", commit=None)
        VC.write_lock(self.root)
        ok, rep = VC.verify(self.root)
        self.assertTrue(ok)
        self.assertTrue(any("未锁定" in w for w in rep["warnings"]))

    def test_corrupt_manifest_is_a_problem(self):
        d = make_source(self.root, "bad")
        with io.open(os.path.join(d, "manifest.json"), "w",
                     encoding="utf-8") as f:
            f.write("{oops")
        ok, rep = VC.verify(self.root)
        self.assertFalse(ok)
        self.assertTrue(any("解析失败" in p for p in rep["problems"]))

    def test_label_sheet_is_not_treated_as_corpus(self):
        d = os.path.join(self.root, "data", "external", "label_sheet")
        os.makedirs(d)
        with io.open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>")
        self.assertEqual(VC.list_sources(self.root), [])


if __name__ == "__main__":
    unittest.main()
