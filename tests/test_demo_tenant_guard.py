# -*- coding: utf-8 -*-
"""红队 P0-2：demo 模式不得让匿名客户端自报任意租户读数据/建库文件。

默认策略把租户头限制在白名单（只有 default）；本地多租户演示可显式配
白名单；EVOGUARD_DEMO_ALLOW_ANY_TENANT=1 才恢复旧的完全自报行为。
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import app as APP                                            # noqa: E402


class TestDemoTenantGuard(unittest.TestCase):
    def test_default_rejects_unlisted_self_reported_tenant(self):
        """默认：自报 victim 租户被拒，只有 default 可用。"""
        env = {"EVOGUARD_DEMO_ALLOW_ANY_TENANT": "",
               "EVOGUARD_DEMO_TENANT_WHITELIST": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            r = APP._demo_header_resolver()
        # 自报他人租户 -> None（上层会 _reject）
        self.assertIsNone(r.resolve({"X-Tenant-Id": "victim"}))
        # 无头 / 自报 default -> default
        self.assertEqual(r.resolve({}).tenant_id, "default")
        self.assertEqual(r.resolve({"X-Tenant-Id": "default"}).tenant_id,
                         "default")

    def test_explicit_whitelist_allows_listed_tenants(self):
        env = {"EVOGUARD_DEMO_ALLOW_ANY_TENANT": "",
               "EVOGUARD_DEMO_TENANT_WHITELIST": "alice,bob"}
        with mock.patch.dict(os.environ, env, clear=False):
            r = APP._demo_header_resolver()
        self.assertEqual(r.resolve({"X-Tenant-Id": "alice"}).tenant_id, "alice")
        self.assertIsNone(r.resolve({"X-Tenant-Id": "victim"}))

    def test_allow_any_opt_in_restores_legacy(self):
        env = {"EVOGUARD_DEMO_ALLOW_ANY_TENANT": "1",
               "EVOGUARD_DEMO_TENANT_WHITELIST": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            r = APP._demo_header_resolver()
        self.assertEqual(r.resolve({"X-Tenant-Id": "victim"}).tenant_id,
                         "victim", "显式逃生舱应恢复旧的自报行为")
        # 即便逃生舱，非法租户格式仍被 HeaderResolver 自身拒绝
        self.assertIsNone(r.resolve({"X-Tenant-Id": "../etc"}))


if __name__ == "__main__":
    unittest.main()
