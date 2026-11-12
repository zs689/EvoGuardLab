# -*- coding: utf-8 -*-
"""TLS 终止与反向代理头信任。

两种部署都支持：
1. **反代终止 TLS**（推荐）：应用只说 HTTP，`resolve_proxy()` 在**信任的对端**
   上读取 `X-Forwarded-*`；
2. **应用内 TLS**：`build_context()` 返回可用的 `ssl.SSLContext`，交给
   `ThreadingHTTPServer` 的 socket 包装。

边界：证书签发、轮换、ACME 不在应用内做；`X-Forwarded-*` 只有在
`peer_ip` 属于信任列表时才采信，否则一律忽略（防伪造）。
"""
import ipaddress
import os
import ssl
from dataclasses import dataclass

# 默认只信任本机回环（典型反代同机部署）。跨机反代必须显式配置网段。
DEFAULT_TRUSTED = ("127.0.0.1/32", "::1/128")

_VERSION_MAP = {
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
    "TLSv1.3": ssl.TLSVersion.TLSv1_3,
}


@dataclass(frozen=True)
class TLSConfig:
    certfile: str
    keyfile: str
    min_version: str = "TLSv1.2"
    ciphers: str = ""
    alpn: tuple = ("http/1.1",)


def build_context(cfg):
    """构建服务端 SSLContext；证书缺失时给清晰错误，不静默降级到明文。"""
    if cfg.min_version not in _VERSION_MAP:
        raise ValueError("不支持的最低 TLS 版本：%s" % cfg.min_version)
    for p in (cfg.certfile, cfg.keyfile):
        if not p or not os.path.isfile(p):
            raise FileNotFoundError("TLS 证书/私钥不存在：%s" % p)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = _VERSION_MAP[cfg.min_version]
    if cfg.ciphers:
        ctx.set_ciphers(cfg.ciphers)
    if cfg.alpn:
        ctx.set_alpn_protocols(list(cfg.alpn))
    ctx.load_cert_chain(cfg.certfile, cfg.keyfile)
    return ctx


def is_trusted(ip, trusted=None):
    """IP 是否在信任列表；支持单 IP 与 CIDR。非法值一律不信任。"""
    trusted = DEFAULT_TRUSTED if trusted is None else trusted
    try:
        addr = ipaddress.ip_address(str(ip).strip("[]"))
    except ValueError:
        return False
    for item in trusted:
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class ProxyInfo:
    proto: str          # "http" / "https"
    host: str
    client_ip: str
    trusted: bool       # 对端是否在信任列表


def _first(value):
    return (value or "").split(",")[0].strip()


def resolve_proxy(headers, peer_ip, trusted=None):
    """解析代理头。对端不可信时**完全忽略** X-Forwarded-*，只用真实 peer。"""
    peer = str(peer_ip or "").strip("[]")
    ok = is_trusted(peer, trusted)
    if not ok:
        return ProxyInfo(proto="http", host=_first(headers.get("Host"))[:255],
                         client_ip=peer, trusted=False)
    proto = _first(headers.get("X-Forwarded-Proto")).lower() or "http"
    if proto not in ("http", "https"):
        proto = "http"
    host = _first(headers.get("X-Forwarded-Host")) or _first(headers.get("Host"))
    client = _first(headers.get("X-Forwarded-For")) or peer
    return ProxyInfo(proto=proto, host=host[:255], client_ip=client, trusted=True)


def is_https(headers, peer_ip, trusted=None):
    """给 security headers / cookie 判断用。"""
    return resolve_proxy(headers, peer_ip, trusted).proto == "https"
