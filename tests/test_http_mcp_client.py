# -*- coding: utf-8 -*-
"""MOD-MCP-01-T1：纯标准库 HttpMCPClient（修前必红反例）。"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import mcp_client as MC                                     # noqa: E402


class _Stub(BaseHTTPRequestHandler):
    tools = [{"name": "echo.read", "description": "echo",
              "inputSchema": {"type": "object"}}]

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("Authorization") != "Bearer testtoken":
            self.send_response(401)
            self.end_headers()
            return
        m, mid = msg.get("method"), msg.get("id")
        if m == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}}}
        elif m == "tools/list":
            result = {"tools": self.tools}
        elif m == "tools/call":
            nm = (msg.get("params") or {}).get("name")
            result = {"content": [{"type": "text", "text": "echo:" + str(nm)}]}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": mid,
                           "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestHttpMCPClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Stub)
        cls.port = cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_http_handshake_and_tools(self):
        c = MC.HttpMCPClient(
            "http://127.0.0.1:%d/" % self.port,
            allowed_domains=["127.0.0.1"], token="testtoken",
            allow_loopback=True)
        try:
            tools = c.list_tools()
            self.assertEqual(tools[0]["name"], "echo.read")
            r = c.call_tool("echo.read", {"x": 1})
            self.assertIn("echo:echo.read", str(r))
        finally:
            c.close()

    def test_ssrf_blocked(self):
        with self.assertRaises(Exception):
            MC.HttpMCPClient("http://192.168.1.1/",
                             allowed_domains=["example.com"],
                             token="testtoken")

    def test_wrong_token_fails_loud(self):
        c = MC.HttpMCPClient(
            "http://127.0.0.1:%d/" % self.port,
            allowed_domains=["127.0.0.1"], token="wrong",
            allow_loopback=True)
        try:
            with self.assertRaises(Exception):
                c.list_tools()
        finally:
            c.close()

    def test_register_tools_same_interface(self):
        import tool_registry as TR
        c = MC.HttpMCPClient(
            "http://127.0.0.1:%d/" % self.port,
            allowed_domains=["127.0.0.1"], token="testtoken",
            allow_loopback=True)
        try:
            reg = TR.ToolRegistry()
            c.register_tools(reg)
            self.assertTrue(reg.declared())
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
