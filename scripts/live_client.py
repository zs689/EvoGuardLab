# -*- coding: utf-8 -*-
"""起一个真服务、发真请求的小工具。压测与演练脚本共用。

为什么不用 requests / pytest-httpserver：本仓库的 `dependencies = []` 是
可复现主张的一部分 —— 换台机器装不上 torch 也得能跑出同一批数字。
http.client + subprocess 够用了。
"""
import json
import os
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ai-service", "api", "app.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(port, method, path, headers=None, body=None, timeout=10.0):
    """返回 (status, headers, body_bytes)。连不上返回 (0, {}, b"")。

    注意：这里函数名就叫 `http`，所以模块必须写成 `from http.client import ...`
    —— 用 `import http.client` 的话，函数定义会把模块名盖掉。
    """
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        raw = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        return r.status, hdrs, raw
    except (OSError, HTTPException):
        return 0, {}, b""
    finally:
        try:
            conn.close()
        except OSError:
            pass


class Service:
    """上下文管理器：起进程、等就绪、退出时确保收尸。

    **stderr 不要接 PIPE**。这个服务每个请求都往 stderr 写一行访问日志；
    PIPE 缓冲区（Windows 上几 KB）写满后子进程就阻塞在 write 上 —— 表现为
    「请求发出去没响应、10s 超时」，看起来像性能问题，其实是管道死锁。
    所以这里写临时文件：能事后读、又不会堵。
    """

    def __init__(self, env=None, port=None, capture_log=True):
        self.port = port or free_port()
        self.env = dict(os.environ)
        self.env.setdefault("AGENTLAB_LOG_QUIET", "1")
        if env:
            self.env.update(env)
        self.proc = None
        self._log = None
        self._log_path = ""
        if capture_log:
            import tempfile
            fd, self._log_path = tempfile.mkstemp(prefix="evoguard-svc-",
                                                  suffix=".log")
            os.close(fd)
            self._log = open(self._log_path, "w", encoding="utf-8",
                             errors="replace")

    def tail(self, n=2000):
        """子进程日志尾部（起来失败时用）。"""
        if not self._log_path:
            return ""
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as f:
                return f.read()[-n:]
        except OSError:
            return ""

    def __enter__(self):
        out = self._log if self._log else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [sys.executable, APP, "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=out, stderr=out)
        deadline = time.time() + 30
        while time.time() < deadline:
            st, _, _ = http(self.port, "GET", "/api/health", timeout=1.0)
            if st:
                return self
            if self.proc.poll() is not None:
                raise RuntimeError("服务启动失败：%s"
                                   % (self.tail().strip()[:500] or "无输出"))
            time.sleep(0.1)
        raise RuntimeError("30s 内没起来（端口 %d）" % self.port)

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            self._log.close()
        return False


def percentile(values, p):
    """最近秩百分位（无插值）。空列表返回 None，不返回 0 —— 0 会被当成「很快」。"""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[k]


def write_json(path, obj):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return path
