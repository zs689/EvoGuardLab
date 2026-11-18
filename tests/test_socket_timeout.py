# -*- coding: utf-8 -*-
"""EG-005：每连接读写超时（防 slowloris 长期占住线程）。"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "api"))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import app as APP                                            # noqa: E402


class _FakeSock:
    def __init__(self):
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t


class TestSocketTimeout(unittest.TestCase):
    def test_default_timeout_is_reasonable(self):
        self.assertGreater(APP.SOCKET_TIMEOUT_S, 0)
        self.assertLessEqual(APP.SOCKET_TIMEOUT_S, 120)

    def test_setup_applies_timeout_to_connection(self):
        """setup() 在建好 rfile/wfile 后对 connection 设超时。"""
        h = APP.Handler.__new__(APP.Handler)
        fake = _FakeSock()
        h.connection = fake
        with mock.patch.object(APP.BaseHTTPRequestHandler, "setup") as sup:
            with mock.patch.object(APP, "SOCKET_TIMEOUT_S", 12.5):
                h.setup()
        sup.assert_called_once_with()
        self.assertEqual(fake.timeout, 12.5)

    def test_zero_timeout_leaves_system_default(self):
        h = APP.Handler.__new__(APP.Handler)
        fake = _FakeSock()
        h.connection = fake
        with mock.patch.object(APP.BaseHTTPRequestHandler, "setup"):
            with mock.patch.object(APP, "SOCKET_TIMEOUT_S", 0):
                h.setup()
        self.assertIsNone(fake.timeout, "0 表示不改动系统默认超时")


if __name__ == "__main__":
    unittest.main()
