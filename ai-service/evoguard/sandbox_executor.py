# -*- coding: utf-8 -*-
"""INT-04-T1：受控文件沙箱（ReAct 的真实执行器，防路径逃逸）。

为什么需要：`autonomous_agent` 之前只能接调用方注入的 executor，
真正跑起来要么全是桩、要么让模型直接摸文件系统 —— 后者对「自主 Agent」
等于把家钥匙给了它。这里给一个**边界明确**的执行器：

- 所有路径先 `realpath` 解析（含符号链接），再判断是否落在 root 内；
- 拒绝绝对路径、`..`、以及解析后越界的符号链接；
- 写操作单独开关（默认只读）；
- 单文件大小上限、扩展名白名单（防写出 `.exe`/`.dll` 之类）；
- **每个拒绝都返回结构化错误**，让 ReAct 能如实记 `executed=False`。

不假称是 OS 级沙箱（那不是 Python 层能保证的）；这是**应用层路径围栏**，
配合容器/低权限用户才是完整隔离。这一点写在 describe() 里。
"""
import os
import re
import shutil
import threading

#: 允许读写的扩展名（默认只读白名单；写入另有一套更严格的）
READ_EXT = frozenset({".txt", ".md", ".json", ".csv", ".log", ".yaml",
                      ".yml", ".py", ".toml", ".ini"})
# AGENT-E2E-01：编码工具需要写出 .py（修 Bug 基准的核心动作）。写仍受
# SandboxExecutor 的 realpath 围栏 + 审批约束，扩扩展名不等于放宽边界。
WRITE_EXT = frozenset({".txt", ".md", ".json", ".csv", ".log", ".py"})
MAX_BYTES = 1 << 20                      # 单文件 1 MiB
_BAD_NAME = re.compile(r"[\x00<>:\"|?*]")


class SandboxError(PermissionError):
    """路径/权限被拒绝。调用方据此记 executed=False。"""


class SandboxOverloaded(RuntimeError):
    """并发容器已达上限，快速拒绝，不无限拉起容器打爆宿主机内存。"""


#: SBX-SEC-01-T1：全局有界并发信号量，防并发容器 OOM 攻击。
#: 0 表示不限制（仅供受控压测，默认必须有限）。
try:
    _MAX_CONCURRENT = max(0, int(os.environ.get(
        "EVOGUARD_SANDBOX_MAX_CONCURRENT", "4")))
except ValueError:
    _MAX_CONCURRENT = 4
_SANDBOX_SEMAPHORE = (threading.BoundedSemaphore(_MAX_CONCURRENT)
                      if _MAX_CONCURRENT > 0 else None)


def _norm_rel(path):
    """把用户给的路径规范化成相对路径；拒绝绝对路径与上跳。"""
    if not isinstance(path, str) or not path.strip():
        raise SandboxError("path_empty")
    p = path.replace("\\", "/").strip()
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise SandboxError("absolute_path_rejected:%s" % path)   # 绝对路径
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise SandboxError("parent_escape_rejected:%s" % path)   # .. 上跳
    if any(_BAD_NAME.search(x) for x in parts):
        raise SandboxError("illegal_char_in_name:%s" % path)
    return "/".join(parts)


class SandboxExecutor:
    """受控工作目录上的文件读写。默认只读；写入需显式开启。"""

    def __init__(self, root, allow_write=False, max_bytes=MAX_BYTES):
        self.root = os.path.realpath(os.path.abspath(root))
        self.allow_write = bool(allow_write)
        self.max_bytes = int(max_bytes)
        os.makedirs(self.root, exist_ok=True)

    # ---------------- 路径围栏 ----------------
    def resolve(self, path, *, for_write=False):
        rel = _norm_rel(path)
        target = os.path.realpath(os.path.join(self.root, rel))
        # realpath 已解开符号链接；比较前缀必须带分隔符，否则
        # /root2 会被 /root 前缀匹配放过（经典前缀绕过）。
        if not (target == self.root or
                target.startswith(self.root + os.sep)):
            raise SandboxError("symlink_escape_rejected:%s" % path)
        if for_write and not self.allow_write:
            raise SandboxError("write_disabled")
        return rel, target

    @staticmethod
    def _check_ext(rel, allowed):
        ext = os.path.splitext(rel)[1].lower()
        if ext not in allowed:
            raise SandboxError("extension_not_allowed:%s" % (ext or "<none>"))
        return ext

    # ---------------- 操作 ----------------
    def read(self, path):
        rel, target = self.resolve(path)
        self._check_ext(rel, READ_EXT)
        if not os.path.isfile(target):
            raise SandboxError("not_a_file:%s" % path)
        size = os.path.getsize(target)
        if size > self.max_bytes:
            raise SandboxError("file_too_large:%d" % size)
        with open(target, encoding="utf-8", errors="replace") as f:
            return {"path": rel, "size": size, "content": f.read()}

    def write(self, path, content):
        rel, target = self.resolve(path, for_write=True)
        self._check_ext(rel, WRITE_EXT)
        data = str(content or "")
        raw = data.encode("utf-8")
        if len(raw) > self.max_bytes:
            raise SandboxError("content_too_large:%d" % len(raw))
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
        return {"path": rel, "bytes": len(raw)}

    def list(self, subdir="."):
        rel, target = self.resolve(subdir if subdir not in (".", "") else ".")
        if not os.path.isdir(target):
            raise SandboxError("not_a_directory:%s" % subdir)
        out = []
        for name in sorted(os.listdir(target))[:200]:
            full = os.path.join(target, name)
            out.append({"name": name,
                        "dir": os.path.isdir(full),
                        "size": os.path.getsize(full)
                        if os.path.isfile(full) else 0})
        return {"path": rel, "entries": out}

    def stat(self, path):
        rel, target = self.resolve(path)
        if not os.path.exists(target):
            raise SandboxError("not_found:%s" % path)
        return {"path": rel, "is_file": os.path.isfile(target),
                "size": os.path.getsize(target)
                if os.path.isfile(target) else 0}

    def reset(self):
        """清空工作目录（只删 root 内部；root 本身不动）。"""
        for name in os.listdir(self.root):
            p = os.path.join(self.root, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return {"root": self.root, "cleared": True}


def as_tool_executor(sandbox, *, tool_prefix="file."):
    """把沙箱包装成 ReAct 用的 executor(tool, args)。

    工具名限定 `file.read` / `file.write` / `file.list` / `file.stat`；
    其他名字直接拒绝（未声明的能力不因为「能猜到」就放行）。
    """
    def _exec(tool, args):
        args = args if isinstance(args, dict) else {}
        if tool == tool_prefix + "read":
            return sandbox.read(args.get("path"))
        if tool == tool_prefix + "write":
            return sandbox.write(args.get("path"), args.get("content"))
        if tool == tool_prefix + "list":
            return sandbox.list(args.get("path") or ".")
        if tool == tool_prefix + "stat":
            return sandbox.stat(args.get("path"))
        raise SandboxError("tool_not_in_sandbox:%s" % tool)
    return _exec


# ---- PROD-04-T1：受限只读命令 + 出站 HTTP 白名单 --------------------------
#: 命令白名单是**精确匹配**的 token 列表，绝不用 shell=True，绝不做 split(';') 后拼接
READONLY_COMMANDS = frozenset({"ls", "cat", "stat", "wc", "head", "tail",
                               "pwd", "date", "echo", "find"})
SHELL_METACHARS = frozenset(";&|`$<>\n\r\\")

#: SSRF 黑名单：环回 / 链路本地 / 私有网段 / 云元数据
def _is_private_host(host):
    import ipaddress
    h = str(host or "").strip().lower().rstrip(".")
    if h in ("localhost", "ip6-localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_loopback or ip.is_link_local or ip.is_private
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        # 域名：必须走白名单精确匹配，不解析（防 DNS rebinding 误判）
        return None


class NetworkSandbox:
    """HTTP Fetch：域名白名单 + SSRF 内网拦截，纯标准库。"""

    def __init__(self, allowed_domains=(), timeout=5, max_bytes=MAX_BYTES):
        self.allowed = frozenset(str(d).lower().rstrip(".")
                                 for d in (allowed_domains or []))
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)

    def check_url(self, url):
        import urllib.parse as _up
        u = _up.urlparse(str(url or ""))
        if u.scheme not in ("http", "https"):
            raise SandboxError("scheme_not_allowed:%s" % u.scheme)
        host = (u.hostname or "").lower().rstrip(".")
        if not host:
            raise SandboxError("missing_host")
        if not self.allowed or host not in self.allowed:
            raise SandboxError("domain_not_allowlisted:%s" % host)
        private = _is_private_host(host)
        if private is True:
            raise SandboxError("ssrf_private_host:%s" % host)
        return host, u

    def fetch(self, url, *, opener=None):
        host, u = self.check_url(url)
        import urllib.request as _ur
        fn = opener or _ur.urlopen
        try:
            with fn(str(url), timeout=self.timeout) as r:
                raw = r.read(self.max_bytes + 1)
        except Exception as exc:                            # noqa: BLE001
            raise SandboxError("fetch_failed:" + type(exc).__name__)
        if len(raw) > self.max_bytes:
            raise SandboxError("response_too_large")
        return {"url": str(url), "host": host,
                "bytes": len(raw), "body": raw.decode("utf-8", "replace")}


#: 纯 Python 内置只读动作：**不依赖任何外部二进制**，跨平台行为一致。
#: 这是 Windows 原生环境唯一稳定的只读执行路径（该系统下没有 cat/echo
#: 可执行文件，shell=False 调它们会 FileNotFoundError）。
BUILTIN_ACTIONS = frozenset({"read_text", "list_dir", "count_lines"})


def run_builtin_action(action, *, path=None, cwd=None, max_bytes=MAX_BYTES,
                       root=None):
    """内置只读动作。全部用标准库实现，无子进程、无 shell。

    root 给定时，path 必须落在 root 之内（带分隔符的前缀判定，
    与 SandboxExecutor 同一套防 /root2 绕过的规则）。
    """
    import os as _os
    if action not in BUILTIN_ACTIONS:
        raise SandboxError("builtin_not_allowlisted:%s" % action)
    base = root or cwd or _os.getcwd()
    target = path if path is not None else "."
    if _os.path.isabs(str(target)):
        full = _os.path.realpath(str(target))
    else:
        full = _os.path.realpath(_os.path.join(base, str(target)))
    real_root = _os.path.realpath(base)
    if not (full == real_root or full.startswith(real_root + _os.sep)):
        raise SandboxError("builtin_path_outside_root:%s" % str(target)[:40])
    if action == "list_dir":
        names = sorted(_os.listdir(full))
        return {"action": action, "path": full, "entries": names,
                "bytes": len(names)}
    if not _os.path.isfile(full):
        raise SandboxError("builtin_not_a_file:%s" % str(target)[:40])
    size = _os.path.getsize(full)
    if size > max_bytes:
        raise SandboxError("output_too_large:%d" % size)
    with open(full, "rb") as f:
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise SandboxError("output_too_large:%d" % len(raw))
    text = raw.decode("utf-8", "replace")
    if action == "count_lines":
        lines = text.count("\n") + (0 if not text or text.endswith("\n") else 1)
        return {"action": action, "path": full, "lines": lines,
                "bytes": len(raw)}
    return {"action": action, "path": full, "text": text, "bytes": len(raw)}


def run_readonly_command(argv, *, cwd=None, timeout=5,
                         max_bytes=MAX_BYTES, popen=None):
    """受控只读命令：白名单精确匹配 + 无 shell + 无元字符 + 超时/输出上限。

    返回 {cmd, stdout, bytes}；任何违规抛 SandboxError（-> executed=False）。
    """
    import shutil as _shutil
    import subprocess
    args = list(argv or [])
    if not args or str(args[0]) not in READONLY_COMMANDS:
        raise SandboxError("command_not_allowlisted:%s"
                           % (args[0] if args else "<empty>"))
    # 每个参数都禁 shell 元字符（含反斜杠与换行）
    for a in args:
        if any(ch in str(a) for ch in SHELL_METACHARS):
            raise SandboxError("shell_metachar_in_arg:%s" % str(a)[:30])
    # 白名单只保证「名字允许」，不保证「本机存在」：Windows 原生环境没有
    # cat/echo 可执行文件，直接 subprocess 会抛 FileNotFoundError 而不是
    # 给出可读原因。这里显式判存在性，并提示改用内置动作。
    if popen is None and _shutil.which(str(args[0])) is None:
        raise SandboxError(
            "command_not_available:%s（本机无此可执行文件；"
            "跨平台只读请用 run_builtin_action）" % args[0])
    fn = popen or subprocess.run
    try:
        r = fn([str(x) for x in args], cwd=cwd, timeout=timeout,
               shell=False, capture_output=True, check=False)
    except Exception as exc:                                # noqa: BLE001
        raise SandboxError("command_failed:" + type(exc).__name__)
    out = (r.stdout or b"")
    if isinstance(out, str):
        out = out.encode("utf-8")
    if len(out) > max_bytes:
        raise SandboxError("output_too_large:%d" % len(out))
    return {"cmd": str(args[0]), "stdout": out.decode("utf-8", "replace"),
            "bytes": len(out), "returncode": r.returncode}


class DockerSandboxError(RuntimeError):
    """Docker 沙箱不可用或执行失败。绝不降级到宿主机 exec。"""


class DockerCodeSandbox:
    """AGENT-PRO-01-T2：受控容器代码沙箱。

    在无网、只读根文件系统、限内存/CPU/进程数的临时容器里跑 Python 脚本，
    支撑「改代码 -> 跑测试 -> 自我修复」闭环。宿主机没有 Docker 时**明确报错**，
    绝不退回 `exec(open(...))` 之类的宿主机执行（那等于把家钥匙给模型）。

    隔离由容器内核保证：--network=none 断网、--read-only 只读根、
    --memory/--cpus 限额、--cap-drop=ALL + no-new-privileges 收权。
    脚本通过只读挂载的临时目录进入容器，宿主机不留可执行副作用。
    """

    DEFAULT_IMAGE = "python:3.11-alpine"

    def __init__(self, image=None, *, memory="256m", cpus="0.5",
                 timeout_s=20.0, max_output_bytes=1 << 20):
        self.image = image or os.environ.get("EVOGUARD_SANDBOX_IMAGE") \
            or self.DEFAULT_IMAGE
        self.memory = str(memory)
        self.cpus = str(cpus)
        self.timeout_s = float(timeout_s)
        self.max_output_bytes = int(max_output_bytes)
        self.docker = shutil.which("docker")
        if not self.docker:
            raise DockerSandboxError(
                "docker_not_available：本机无 docker，代码沙箱不可用；"
                "按设计不降级到宿主机执行")
        # 确认 daemon 真能连（只 which 到 client 不够）
        import subprocess as _sp
        try:
            r = _sp.run([self.docker, "info", "--format", "{{.ServerVersion}}"],
                        capture_output=True, timeout=10, check=False)
        except Exception as exc:                            # noqa: BLE001
            raise DockerSandboxError("docker_daemon_unreachable:%s"
                                     % type(exc).__name__) from exc
        if r.returncode != 0:
            raise DockerSandboxError(
                "docker_daemon_unreachable:%s"
                % r.stderr.decode("utf-8", "replace")[:120])

    def run(self, code, *, timeout_s=None):
        """在容器里执行 Python 源码，返回 {ok, stdout, stderr, returncode, ...}。"""
        if not isinstance(code, str) or not code.strip():
            raise DockerSandboxError("empty_code")
        # SBX-SEC-01-T1：并发上限，超限立即拒绝（不排队无限拉容器）
        got = True
        if _SANDBOX_SEMAPHORE is not None:
            got = _SANDBOX_SEMAPHORE.acquire(blocking=False)
            if not got:
                raise SandboxOverloaded(
                    "sandbox_overloaded：并发容器已达上限 %d" % _MAX_CONCURRENT)
        try:
            return self._run_inner(code, timeout_s=timeout_s)
        finally:
            if got and _SANDBOX_SEMAPHORE is not None:
                _SANDBOX_SEMAPHORE.release()

    def _run_inner(self, code, *, timeout_s=None):
        import subprocess as _sp
        import tempfile
        import uuid
        limit = float(timeout_s or self.timeout_s)
        name = "evo-sbx-" + uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="evo-sbx-") as tmp:
            script = os.path.join(tmp, "script.py")
            with open(script, "w", encoding="utf-8", newline="\n") as f:
                f.write(code)
            cmd = [
                self.docker, "run", "--rm", "--name", name,
                "--network=none", "--read-only",
                "--memory=" + self.memory, "--cpus=" + self.cpus,
                "--pids-limit=64", "--cap-drop=ALL",
                "--security-opt", "no-new-privileges",
                # SBX-SEC-01-T1：非 root（nobody）+ 受限 tmpfs
                "--user", "65534:65534",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "-v", script + ":/work/script.py:ro",
                "-w", "/work", self.image,
                "python", "/work/script.py",
            ]
            try:
                r = _sp.run(cmd, capture_output=True, timeout=limit, check=False)
                out = r.stdout or b""
                err = r.stderr or b""
                if isinstance(out, str):
                    out = out.encode("utf-8")
                if isinstance(err, str):
                    err = err.encode("utf-8")
                return {"ok": r.returncode == 0,
                        "stdout": out[:self.max_output_bytes].decode("utf-8", "replace"),
                        "stderr": err[:self.max_output_bytes].decode("utf-8", "replace"),
                        "returncode": r.returncode,
                        "truncated": len(out) > self.max_output_bytes or
                                     len(err) > self.max_output_bytes}
            except _sp.TimeoutExpired:
                # 超时：强杀容器，宿主机无残留进程
                _sp.run([self.docker, "rm", "-f", name],
                        capture_output=True, timeout=10, check=False)
                return {"ok": False, "stdout": "", "stderr": "timeout",
                        "returncode": -1, "timeout": True}
            except Exception as exc:                        # noqa: BLE001
                _sp.run([self.docker, "rm", "-f", name],
                        capture_output=True, timeout=10, check=False)
                raise DockerSandboxError("run_failed:" + type(exc).__name__) \
                    from exc


def register_coding_tools(registry, *, root, docker_sandbox=None):
    """AGENT-E2E-01-T1：把受控文件读写 + 容器代码执行注册为标准 Agent 工具。

    - fs.read：只读，限于 sandbox root（路径围栏由 SandboxExecutor 保证）；
    - fs.write：写工具，走审批 + 受控根目录，绝不越界；
    - code.run_in_sandbox：只读挂载进容器执行测试，宿主机无副作用。

    executor（传入 ReActAgent 的 callable）由调用方按 name 分派到对应对象。
    """
    fs = SandboxExecutor(root, allow_write=True)
    reg = registry
    # 与 tool_contract.DEFAULT_CONTRACTS / TOOL_SCHEMAS 对齐的显式契约：
    # controlled 模式的契约层与身份层各自独立检查，两处必须一致。
    reg.register("fs.read", {
        "identities": ["coder"], "allowed_domains": [],
        "allowed_resource_pattern": None, "writable_paths": [],
        "max_amount": None, "methods": ["read"], "side_effect": "none",
        "requires_approval": False,
    }, scope="read_only", source="coding_bench")
    reg.register("fs.write", {
        "identities": ["coder"], "allowed_domains": [],
        "allowed_resource_pattern": None, "writable_paths": ["*"],
        "max_amount": None, "methods": ["write"], "side_effect": "write",
        "requires_approval": True,
    }, scope="write", source="coding_bench")
    reg.register("code.run_in_sandbox", {
        "identities": ["coder"], "allowed_domains": [],
        "allowed_resource_pattern": None, "writable_paths": [],
        "max_amount": None, "methods": ["read"], "side_effect": "none",
        "requires_approval": False,
    }, scope="read_only", source="coding_bench")

    def executor(tool, args):
        if tool == "fs.read":
            return fs.read(str(args.get("path") or ""))   # 已是 {path,size,content}
        if tool == "fs.write":
            r = fs.write(str(args.get("path") or ""), str(args.get("content") or ""))
            return {"written": True, "path": r.get("path"), "bytes": r.get("bytes")}
        if tool == "code.run_in_sandbox":
            if docker_sandbox is None:
                return {"error": "docker_sandbox_unavailable"}
            r = docker_sandbox.run(str(args.get("code") or ""))
            return {"stdout": r.get("stdout", ""), "stderr": r.get("stderr", ""),
                    "returncode": r.get("returncode"), "ok": r.get("ok")}
        return {"error": "unknown_tool:" + tool}

    return executor


def describe():
    return {
        "level": "application-level path fence (NOT an OS sandbox)",
        "root_required": True,
        "default_allow_write": False,
        "read_ext": sorted(READ_EXT),
        "write_ext": sorted(WRITE_EXT),
        "max_bytes": MAX_BYTES,
        "readonly_commands": sorted(READONLY_COMMANDS),
        "builtin_actions": sorted(BUILTIN_ACTIONS),
        "network": "domain allowlist + SSRF private-host block",
        "protections": ["absolute path", ".. traversal", "symlink escape",
                        "extension allowlist", "size cap"],
        "not_protected": ["OS-level resource limits", "network egress",
                          "process isolation (需容器/低权限用户)"],
        "code_sandbox": "DockerCodeSandbox（可选，容器内核隔离；无 Docker 明确报错不降级）",
    }
