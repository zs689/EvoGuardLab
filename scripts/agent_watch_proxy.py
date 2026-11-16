#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentWatch HTTP 防护代理：外部流量必须先过网关才能到被监测 agent。

    python scripts/agent_watch_proxy.py --port 18780 --upstream http://127.0.0.1:9000/invoke
    python scripts/agent_watch_proxy.py --port 18780              # 内置演示后端

端点：
  POST /invoke  —— 经过 GuardGateway（默认 fail-closed），deny 返回 403
  POST /bypass  —— **仅验证对照用**，默认关闭（--allow-bypass 才开），
                   模拟攻击者绕过网关直连后端；生产绝不能开
  GET  /health  —— 探针

请求体：{"prompt": str, "tool_outputs": [...], "context": {...}}
响应：六字段契约 detected/decision/attempted/executed/effect/reason
      （/bypass 返回 agent 原始结果，用于证明「不防护就会被打穿」）
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, HERE)

import agent_watch as AW                                    # noqa: E402
import verify_agent_watch as V                             # noqa: E402


class Proxy:
    def __init__(self, upstream=None, enforce=True, allow_bypass=False):
        self.enforce = enforce
        self.allow_bypass = allow_bypass
        self.gateway = AW.GuardGateway(enforce=enforce)
        if upstream:
            self.target = AW.AgentTarget.from_http(
                "upstream", upstream,
                V.SAFE_MANIFEST["tools"], V.SAFE_MANIFEST)
        else:
            self.target = AW.AgentTarget.from_callable(
                "demo_backend", V.vulnerable_agent,
                V.VULN_MANIFEST["tools"], V.VULN_MANIFEST)


class Handler(BaseHTTPRequestHandler):
    proxy = None

    def log_message(self, *_args):
        pass

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "enforce": self.proxy.enforce,
                             "target": self.proxy.target.name})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 65536:
            self._send(413, {"error": "bad_body_size"})
            return
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "bad_json"})
            return
        if not isinstance(req, dict):
            self._send(400, {"error": "body_must_be_object"})
            return

        if self.path == "/bypass":
            if not self.proxy.allow_bypass:
                self._send(404, {"error": "bypass_disabled"})
                return
            self._send(200, self.proxy.target.invoke(req))
            return

        if self.path != "/invoke":
            self._send(404, {"error": "not_found"})
            return

        decision = self.proxy.gateway.invoke(self.proxy.target, req)
        self._send(200 if decision["executed"] else 403, decision)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18780)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--upstream", default="")
    ap.add_argument("--audit", action="store_true",
                    help="关闭拦截（仅观测），用于对照")
    ap.add_argument("--allow-bypass", action="store_true",
                    help="仅验证用：开放 /bypass 直连后端")
    a = ap.parse_args()
    Handler.proxy = Proxy(a.upstream or None, enforce=not a.audit,
                          allow_bypass=a.allow_bypass)
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print("AgentWatch proxy -> http://%s:%d enforce=%s"
          % (a.bind, a.port, Handler.proxy.enforce), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
