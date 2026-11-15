# -*- coding: utf-8 -*-
"""EG-018：校验器要读懂嵌套 source/sources，不能把「字段放错层」当成缺失。"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import verify_corpus as VC                                  # noqa: E402


class TestCorpusUpstream(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="evo-corpus-up-")
        self.d = os.path.join(self.root, "data", "external", "foo")
        os.makedirs(self.d)

    def _manifest(self, obj):
        with io.open(os.path.join(self.d, "manifest.json"), "w",
                     encoding="utf-8") as f:
            json.dump(obj, f)

    def _upstream(self, obj):
        with io.open(os.path.join(self.root, "data", "corpus_upstream.json"),
                     "w", encoding="utf-8") as f:
            json.dump({"sources": {"foo": obj}}, f)

    def test_nested_source_is_read(self):
        self._manifest({
            "version": "v1",
            "source": {"repo": "org/repo", "license": "MIT",
                       "license_url": "https://example.invalid/LICENSE"},
            "blob_sha": "a" * 40,
        })
        e = VC.build_entry(self.root, "foo")
        self.assertEqual(e["license"], "MIT")
        self.assertEqual(e["repo"], "org/repo")
        self.assertEqual(e["pin_status"], "content_pinned")
        self.assertIn("blob_sha", e["content_hashes"])

    def test_sources_dict_and_list_are_merged(self):
        self._manifest({"sources": {
            "a": {"repo": "org/a", "license": "MIT"},
            "b": {"repo": "org/b", "license": "Apache-2.0"}}})
        e = VC.build_entry(self.root, "foo")
        self.assertEqual(e["license"], "Apache-2.0+MIT")
        self.assertIn("org/a", e["repo"])
        self.assertIn("org/b", e["repo"])
        self._manifest({"sources": [
            {"repo": "org/c", "license": "MIT",
             "license_url": "https://example.invalid/L"}]})
        e2 = VC.build_entry(self.root, "foo")
        self.assertEqual(e2["repo"], "org/c")

    def test_cache_placeholder_is_not_a_hash(self):
        self._manifest({"repo": "org/repo", "license": "MIT",
                        "git_sha": "cache"})
        e = VC.build_entry(self.root, "foo")
        self.assertEqual(e["pin_status"], "unpinned",
                         "cache 占位符不能被当成内容锁定")

    def test_verified_commit_suppresses_warning(self):
        self._manifest({"source": {"repo": "org/repo", "license": "MIT",
                                   "license_url": "https://e.invalid/L"},
                        "raw_sha256": "b" * 64})
        self._upstream({"commit": "c" * 40,
                        "commit_status": "verified_by_sha256_match",
                        "license_url": "https://e.invalid/L"})
        ok, rep = VC.verify(self.root, lock_path=os.path.join(
            self.root, "no-lock.json"))
        self.assertTrue(ok)
        self.assertFalse(any("commit 未核实" in w for w in rep["warnings"]))

    def test_unverified_commit_is_reported_honestly(self):
        self._manifest({"source": {"repo": "org/repo", "license": "MIT",
                                   "license_url": "https://e.invalid/L"},
                        "raw_sha256": "b" * 64})
        self._upstream({"commit": None, "commit_status": "unverified"})
        ok, rep = VC.verify(self.root, lock_path=os.path.join(
            self.root, "no-lock.json"))
        self.assertTrue(ok, "默认模式不应因 commit 未核实判失败")
        self.assertTrue(any("commit 未核实" in w for w in rep["warnings"]),
                        "但不能静默")

    def test_missing_license_url_is_backfilled_from_upstream(self):
        self._manifest({"source": {"repo": "org/repo", "license": "MIT"},
                        "blob_sha": "d" * 40})
        self._upstream({"license_url": "https://e.invalid/LICENSE",
                        "commit_status": "unverified"})
        e = VC.build_entry(self.root, "foo")
        self.assertEqual(e["license_url"], "https://e.invalid/LICENSE")
        self.assertIn("license_url", e["from_upstream"])


if __name__ == "__main__":
    unittest.main()
