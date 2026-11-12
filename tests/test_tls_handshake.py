# -*- coding: utf-8 -*-
"""真实 TLS 握手测试 —— 不是「配置对象参数对不对」。

配置对象能构造出来，只证明 `load_cert_chain` 没抛错；证明不了握手能成、
证明不了最低版本能挡住老协议、更证明不了「明文客户端打进来会被拒」。
这里起一个真的 TLS socket，用真的客户端连一次。

证书用 `openssl` 现场自签（Git Bash / Linux / macOS 自带）。没有 openssl
时**跳过而不是假装通过** —— 跳过是诚实的，静默通过不是。

跑通了会写 `experiments/tls_handshake_v1/result.json`：scorecard 读的是
这份产物，不是本文件是否存在。产物在 = 真的握过手。
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from enterprise import tls_proxy as P                    # noqa: E402

OUT = os.path.join(ROOT, "experiments", "tls_handshake_v1", "result.json")
OPENSSL = shutil.which("openssl")


def _gen_cert(dirpath):
    """自签一张 localhost 证书。返回 (cert, key) 或 (None, None)。"""
    cert = os.path.join(dirpath, "server.crt")
    key = os.path.join(dirpath, "server.key")
    cmd = [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", key, "-out", cert, "-days", "1",
           "-subj", "/CN=localhost",
           "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, None
    if r.returncode != 0 or not (os.path.isfile(cert) and os.path.isfile(key)):
        return None, None
    return cert, key


def _serve_once(sock, ctx):
    """接一个连接、按 TLS 服务端握手、回一行 HTTP。握手失败就放弃。"""
    try:
        conn, _ = sock.accept()
    except OSError:
        return
    tls = None
    try:
        tls = ctx.wrap_socket(conn, server_side=True)
        tls.recv(1024)
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    except (ssl.SSLError, OSError):
        pass
    finally:
        for s in (tls, conn):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass


class _Server:
    """一个只服务一个连接的 TLS 服务端。每个用例各起一个，
    避免「上一个用例把连接用掉了」这种互相干扰。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def __enter__(self):
        threading.Thread(target=_serve_once, args=(self.sock, self.ctx),
                         daemon=True).start()
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass
        return False


@unittest.skipUnless(OPENSSL, "没有 openssl，无法签测试证书（跳过而非假装通过）")
class TestTLSHandshake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evo-tls-")
        cls.cert, cls.key = _gen_cert(cls.tmp)
        if not cls.cert:
            raise unittest.SkipTest("openssl 自签失败")
        cls.ctx = P.build_context(P.TLSConfig(cls.cert, cls.key))

    def _client_ctx(self):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c

    def test_handshake_succeeds(self):
        """真的握手：能连上、能拿到响应、协商出版本与套件。"""
        with _Server(self.ctx) as srv:
            raw = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
            with self._client_ctx().wrap_socket(raw,
                                                server_hostname="localhost") as s:
                version = s.version()
                cipher = s.cipher()
                s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                data = s.recv(64)
        self.assertIn(b"200 OK", data)
        self.assertIn(version, ("TLSv1.2", "TLSv1.3"))
        self.assertTrue(cipher and cipher[0])
        _record({"handshake": True, "version": version,
                 "cipher": cipher[0] if cipher else None})

    def test_plaintext_client_is_rejected(self):
        """明文客户端打 TLS 端口必须失败 —— 否则等于给了一条绕过通道。"""
        with _Server(self.ctx) as srv:
            raw = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
            raw.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            failed = False
            try:
                data = raw.recv(64)
                # 服务端不会回 HTTP；收到空字节或非 HTTP 也算被拒
                if not data or not data.startswith(b"HTTP/"):
                    failed = True
            except OSError:
                failed = True
            finally:
                raw.close()
        self.assertTrue(failed, "明文请求竟然被当作正常请求处理了")

    def test_min_version_rejects_old_protocol(self):
        """最低版本设 1.3 时，只支持 1.2 的客户端必须被拒。"""
        ctx13 = P.build_context(P.TLSConfig(self.cert, self.key,
                                           min_version="TLSv1.3"))
        cctx = self._client_ctx()
        cctx.maximum_version = ssl.TLSVersion.TLSv1_2
        with _Server(ctx13) as srv:
            raw = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
            rejected = False
            try:
                with cctx.wrap_socket(raw, server_hostname="localhost"):
                    rejected = False
            except ssl.SSLError:
                rejected = True
            finally:
                try:
                    raw.close()
                except OSError:
                    pass
        self.assertTrue(rejected, "min_version=TLSv1.3 却没挡住 TLS1.2 客户端")
        self.assertIsNotNone(ctx13)


def _record(facts):
    """把「真的握过手」写进产物；同一进程多次调用取并集。"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    data = {}
    if os.path.isfile(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    data.update(facts)
    data["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 只记工具名，不记本机绝对路径 —— 仓库里的产物不该带开发机目录。
    data["openssl"] = os.path.basename(OPENSSL) if OPENSSL else ""
    data["note"] = ("本文件由 tests/test_tls_handshake.py 在真实握手成功后写出。"
                    "scorecard 读的是它，不是测试文件是否存在。")
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
