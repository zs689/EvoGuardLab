# -*- coding: utf-8 -*-
"""FINAL-03：零依赖标准 MCP 客户端（stdio JSON-RPC 2.0）。

协议：启动 MCP Server 子进程 → `initialize` 握手 → `tools/list` 发现工具
→ `tools/call` 调用。所有工具调用经 `as_executor()` 交给 ReActAgent 时，
仍走 execution.preflight + tool_rails + taint 污点追踪 + 返回侧护栏。

零第三方依赖：标准库 subprocess + json + threading + queue。
"""
import json
import os
import queue
import subprocess
import sys
import threading


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, argv, *, timeout=30, env=None, cwd=None):
        if not argv:
            raise ValueError("MCPClient 需要启动命令")
        self.argv = list(argv)
        self.timeout = int(timeout)
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()
        self._responses = {}
        self._cv = threading.Condition()
        self._reader = None
        self.tools = []
        self._start(env=env, cwd=cwd)

    def _start(self, *, env=None, cwd=None):
        e = dict(os.environ)
        if env:
            e.update(env)
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=e, cwd=cwd, text=True,
            bufsize=1, encoding="utf-8", errors="replace")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue
                with self._cv:
                    self._responses[mid] = msg
                    self._cv.notify_all()
        except Exception:                                   # noqa: BLE001
            pass

    def _request(self, method, params=None):
        with self._lock:
            self._id += 1
            mid = self._id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method,
               "params": params or {}}
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError("mcp_write_failed:%s" % type(exc).__name__)
        deadline = self.timeout
        with self._cv:
            if mid not in self._responses:
                self._cv.wait(timeout=deadline)
            resp = self._responses.pop(mid, None)
        if resp is None:
            raise MCPError("mcp_timeout:%s" % method)
        if "error" in resp:
            raise MCPError("mcp_error:%s:%s" % (
                method, str(resp["error"])[:120]))
        return resp.get("result")

    def initialize(self):
        """握手并发现工具，返回 tools 列表。"""
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "evoguard-mcp-client", "version": "1"}})
        # 通知 initialized（无 id，不期待响应）
        try:
            self.proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized",
                "params": {}}) + "\n")
            self.proc.stdin.flush()
        except Exception:                                   # noqa: BLE001
            pass
        return self.list_tools()

    def list_tools(self):
        result = self._request("tools/list", {})
        self.tools = list((result or {}).get("tools") or [])
        return self.tools

    def call(self, name, args=None):
        """直接调用远端工具（不经门禁；供 as_executor 内部使用）。"""
        return self._request("tools/call", {"name": name,
                                             "arguments": args or {}})

    def register_tools(self, registry, *, agent_type="mcp"):
        """把远端工具注册进 ToolRegistry（默认只读，写工具需显式声明）。"""
        import mcp_proxy as _proxy
        _proxy.register_tools(self.tools, registry=registry,
                              agent_type=agent_type)
        return registry

    def as_executor(self, *, registry=None, rails=None):
        """返回 ReActAgent 可直接用的 executor(tool, args)。

        注意：门禁由 ReActAgent._preflight 在执行前统一执行；这里只做
        返回侧护栏（污点/注入扫描），避免重复门禁造成误判。
        """
        def executor(tool, args):
            result = self.call(tool, args)
            try:
                import mcp_proxy as _proxy
                v = _proxy.guard_result(tool, result, rails=rails)
                if v.get("blocked"):
                    return {"error": "mcp_result_blocked",
                            "codes": v.get("codes", [])}
            except Exception:                               # noqa: BLE001
                pass
            return result
        return executor

    def close(self):
        """优雅关闭：先关 stdin 让 Server 退出，超时强杀。"""
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except Exception:                               # noqa: BLE001
                pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except Exception:                               # noqa: BLE001
                pass
        self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False
