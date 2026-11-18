# -*- coding: utf-8 -*-
"""红队 P1-3：get_store() 的按租户 Store 缓存必须有界（LRU）。

攻击面：不断换 tenant_id 请求，每个租户一个 sqlite 文件 + 一个 Store 实例，
_STORES 只增不删时内存句柄与磁盘文件数无界增长。这里验证：
1. 超过上限时淘汰最久未用条目；
2. 命中会把条目刷新为最近使用（不会被误淘汰）；
3. default 主库永不淘汰；
4. 上限设为 0 时保留旧的「不限制」行为。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import app as APP                                            # noqa: E402


class TestStoreCacheLRU(unittest.TestCase):
    def setUp(self):
        self._old_max = APP._STORE_CACHE_MAX
        self._old_root = APP.TENANT_DB_ROOT
        self.tmp = tempfile.mkdtemp(prefix="evo-store-lru-")
        APP.TENANT_DB_ROOT = self.tmp
        APP._STORES.clear()

    def tearDown(self):
        APP._STORES.clear()
        APP._STORE_CACHE_MAX = self._old_max
        APP.TENANT_DB_ROOT = self._old_root

    def test_lru_evicts_least_recently_used(self):
        APP._STORE_CACHE_MAX = 2
        APP.get_store("t1")
        APP.get_store("t2")
        # 命中 t1，把它刷成最近使用；此时 LRU 顺序 [t2, t1]
        APP.get_store("t1")
        APP.get_store("t3")             # 超上限 -> 应淘汰 t2
        def tenant_of(key):
            return os.path.basename(os.path.dirname(key))

        tenants = [tenant_of(k) for k in APP._STORES.keys()]
        self.assertNotIn("t2", tenants,
                         "t2 是最久未用，应被淘汰：%s" % tenants)
        self.assertIn("t1", tenants)
        self.assertIn("t3", tenants)
        self.assertLessEqual(len(APP._STORES), 2)

    def test_default_store_never_evicted(self):
        APP._STORE_CACHE_MAX = 1
        default_key = APP.tenant_db_path("default")
        APP.get_store("default")
        for i in range(5):
            APP.get_store("tenant%d" % i)
        self.assertIn(default_key, APP._STORES,
                      "default 主库不能被淘汰")
        self.assertLessEqual(len(APP._STORES), 2, "非 default 条目仍受上限约束")

    def test_zero_means_unbounded(self):
        APP._STORE_CACHE_MAX = 0
        for i in range(5):
            APP.get_store("tenant%d" % i)
        self.assertEqual(len(APP._STORES), 5)


if __name__ == "__main__":
    unittest.main()
