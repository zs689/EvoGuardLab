# -*- coding: utf-8 -*-
"""WS-06：Redis 可选后端 —— **纯标准库 RESP 协议**，不引入 redis-py。

核心库 `dependencies=[]`，Redis 是 optional。这里用一个很小的 RESP2
客户端（socket + 手搓协议），够覆盖限流/熔断/作业队列所需命令。
连接失败一律显式抛错，**不静默降级成内存后端**（那会让多副本配额失效）。

地址来自 `EVOGUARD_REDIS_URL`（默认 redis://127.0.0.1:6379/0）。
"""
import os
import socket
import time
import uuid

try:
    from . import distributed as _d
except ImportError:
    import distributed as _d                                # type: ignore


class RedisError(RuntimeError):
    pass


class RESP:
    """极简 RESP2 客户端：只支持 lockstep 请求/响应，够本项目用。

INFRA-CONC-01-T1：RESP 是**无请求 ID 的 lockstep 协议** —— 两个线程同时
sendall 会让回复错配到对方请求，是最危险的静默失效（限流判断串位）。
这里用 RLock 把整个 send+read 串成原子；断链（idle 超时）时重连一次，
避免 ThreadingHTTPServer 下永久失败。
"""

    def __init__(self, host="127.0.0.1", port=6379, db=0,
                 timeout=3.0, password=None):
        self.addr = (host, int(port))
        self.db = int(db)
        self.password = password
        self.timeout = timeout
        self.sock = None
        self._buf = b""
        # 可注入建连函数（测试用假 socket 构造断链/并发；生产走真 socket）
        import socket as _socket
        self._connect_fn = _socket.create_connection
        import threading as _threading
        self._lock = _threading.RLock()

    @classmethod
    def from_url(cls, url, timeout=3.0):
        # redis://[:pass@]host:port/db
        rest = str(url or "redis://127.0.0.1:6379/0").split("://", 1)[-1]
        password = None
        if "@" in rest:
            auth, rest = rest.rsplit("@", 1)
            password = auth.split(":", 1)[-1] or None
        host_port, _, db = rest.partition("/")
        host, _, port = host_port.partition(":")
        return cls(host or "127.0.0.1", int(port or 6379),
                   int(db or 0), timeout=timeout, password=password)

    def connect(self):
        if self.sock is not None:
            return
        try:
            self.sock = self._connect_fn(self.addr, self.timeout)
            self.sock.settimeout(self.timeout)
        except OSError as exc:
            raise RedisError("无法连接 Redis %s: %s" % (self.addr, exc))
        if self.password:
            self.cmd("AUTH", self.password)
        if self.db:
            self.cmd("SELECT", str(self.db))

    def _send(self, parts):
        data = b"*%d\r\n" % len(parts)
        for p in parts:
            b = str(p).encode("utf-8")
            data += b"$%d\r\n" % len(b) + b + b"\r\n"
        self.sock.sendall(data)

    def _readline(self):
        while b"\r\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RedisError("Redis 连接关闭")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _read(self):
        line = self._readline()
        t, body = line[:1], line[1:]
        if t == b"+":
            return body.decode("utf-8")
        if t == b"-":
            raise RedisError(body.decode("utf-8", "replace"))
        if t == b":":
            return int(body)
        if t == b"$":
            n = int(body)
            if n < 0:
                return None
            while len(self._buf) < n + 2:
                self._buf += self.sock.recv(65536)
            out, self._buf = self._buf[:n], self._buf[n + 2:]
            return out.decode("utf-8", "replace")
        if t == b"*":
            n = int(body)
            if n < 0:
                return None
            return [self._read() for _ in range(n)]
        raise RedisError("未知 RESP 类型 %r" % line[:20])

    def cmd(self, *parts):
        """单条命令：send+read 全程持锁；断链重连一次后重试。"""
        with self._lock:
            for attempt in (0, 1):
                try:
                    self.connect()
                    self._send(parts)
                    return self._read()
                except (RedisError, OSError) as exc:
                    # 断链/超时：第一次失败丢弃连接重来一次；
                    # 第二次直接抛，绝不静默返回一个「看起来正常」的值。
                    self._drop_socket()
                    if attempt == 1:
                        raise RedisError("redis 重试后仍失败：%s"
                                         % type(exc).__name__)
        raise RedisError("unreachable")

    def _drop_socket(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:                              # noqa: BLE001
                pass
            self.sock = None
            self._buf = b""

    def close(self):
        with self._lock:
            self._drop_socket()


# 原子滑动窗口：Lua 保证「清理 + 计数 + 入队」一步完成，多副本无竞态
_LUA_RATE = """
local key=KEYS[1]; local now=tonumber(ARGV[1]); local win=tonumber(ARGV[2])
local lim=tonumber(ARGV[3]); local id=ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now-win)
local n=redis.call('ZCARD', key)
if n>=lim then
  local o=redis.call('ZRANGE',key,0,0,'WITHSCORES')
  return {0, tonumber(o[2] or now)+win-now}
end
redis.call('ZADD', key, now, id)
redis.call('PEXPIRE', key, math.ceil(win*1000))
return {1, 0}
"""

_LUA_CLAIM = """
local q=KEYS[1]; local running=KEYS[2]
local now=tonumber(ARGV[1]); local lease=tonumber(ARGV[2])
local job=redis.call('LPOP', q)
if not job then return nil end
redis.call('ZADD', running, now+lease, job)
return job
"""


class RedisLimiter(_d.RateLimiter):
    """跨主机共享的原子滑动窗口（Lua 脚本）。"""

    def __init__(self, url=None, client=None, prefix="evoguard:rl:"):
        self.resp = client or RESP.from_url(
            url or os.environ.get("EVOGUARD_REDIS_URL")
            or "redis://127.0.0.1:6379/0")
        self.prefix = prefix

    def check(self, actor, limit, window, now=None):
        now = time.time() if now is None else now
        r = self.resp.cmd("EVAL", _LUA_RATE, 1, self.prefix + str(actor),
                          "%.6f" % now, "%.6f" % window, int(limit),
                          "%.6f-%s" % (now, uuid.uuid4().hex[:8]))
        return bool(int(r[0])), max(0.0, float(r[1]))


class RedisCircuitBreaker(_d.CircuitBreakerState):
    """跨副本共享的熔断计数（HASH + TTL）。"""

    def __init__(self, url=None, client=None, prefix="evoguard:cb:"):
        super().__init__(backend=None)
        self.resp = client or RESP.from_url(
            url or os.environ.get("EVOGUARD_REDIS_URL")
            or "redis://127.0.0.1:6379/0")
        self.prefix = prefix

    def snapshot(self, key):
        d = self.resp.cmd("HGETALL", self.prefix + str(key))
        m = {d[i]: d[i + 1] for i in range(0, len(d), 2)}
        return {"failures": int(m.get("failures", 0)),
                "open_until": float(m.get("open_until", 0.0))}

    def record(self, key, failure, now=None, threshold=5, cooldown=30.0):
        now = time.monotonic() if now is None else now
        s = self.snapshot(key)
        if failure:
            s["failures"] += 1
            if s["failures"] >= threshold:
                s["open_until"] = now + cooldown
        else:
            s = {"failures": 0, "open_until": 0.0}
        k = self.prefix + str(key)
        self.resp.cmd("HSET", k, "failures", s["failures"],
                      "open_until", "%.6f" % s["open_until"])
        self.resp.cmd("EXPIRE", k, int(cooldown * 2) + 5)
        return s

    def is_open(self, key, now=None):
        now = time.monotonic() if now is None else now
        return self.snapshot(key)["open_until"] > now


class RedisJobQueue:
    """多副本作业抢占：LPOP 领取 + ZSET 租约，超时自动回收（真并发）。"""

    def __init__(self, url=None, client=None,
                 queue="evoguard:jobs", running="evoguard:running"):
        self.resp = client or RESP.from_url(
            url or os.environ.get("EVOGUARD_REDIS_URL")
            or "redis://127.0.0.1:6379/0")
        self.q, self.running = queue, running

    def enqueue(self, job_id):
        return self.resp.cmd("LPUSH", self.q, str(job_id))

    def claim(self, *, now=None, lease=30.0):
        import json as _json
        now = time.time() if now is None else now
        self._reclaim(now)
        job = self.resp.cmd("EVAL", _LUA_CLAIM, 2, self.q, self.running,
                            "%.6f" % now, "%.6f" % lease)
        return _json.loads(job) if job else None

    def _reclaim(self, now):
        expired = self.resp.cmd("ZRANGEBYSCORE", self.running, 0, "%.6f" % now)
        for job in expired:
            self.resp.cmd("ZREM", self.running, job)
            self.resp.cmd("RPUSH", self.q, job)

    def ack(self, job):
        import json as _json
        self.resp.cmd("ZREM", self.running, _json.dumps(
            job, sort_keys=True, ensure_ascii=False))

    def pending(self):
        return self.resp.cmd("LLEN", self.q)


def health(url=None):
    """连接健康检查；失败抛 RedisError（不静默降级）。"""
    r = RESP.from_url(url or os.environ.get("EVOGUARD_REDIS_URL")
                      or "redis://127.0.0.1:6379/0")
    try:
        return r.cmd("PING") == "PONG"
    finally:
        r.close()
