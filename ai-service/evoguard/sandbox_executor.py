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
import contextlib
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


# ZERO-07：FS/网络围栏拆到 fs_sandbox.py；re-export 保持兼容。
from fs_sandbox import *  # noqa: E402,F403
from fs_sandbox import (SandboxError, SandboxOverloaded,  # noqa: E402
                        SandboxExecutor, NetworkSandbox,
                        as_tool_executor, run_builtin_action,
                        run_readonly_command, _norm_rel, _is_private_host,
                        _SANDBOX_SEMAPHORE, _MAX_CONCURRENT, MAX_BYTES,
                        WRITE_EXT, READ_EXT)

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
                 timeout_s=20.0, max_output_bytes=1 << 20,
                 probe_daemon=True):
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
        # 确认 daemon 真能连（只 which 到 client 不够）。
        # probe_daemon=False 供「只检查参数构造、不真跑容器」的离线单测使用；
        # 实际 run()/run_workspace() 仍会在 daemon 不在时直连失败，绝不降级。
        if probe_daemon:
            import subprocess as _sp
            try:
                r = _sp.run([self.docker, "info", "--format",
                             "{{.ServerVersion}}"],
                            capture_output=True, timeout=10, check=False)
            except Exception as exc:                        # noqa: BLE001
                raise DockerSandboxError("docker_daemon_unreachable:%s"
                                         % type(exc).__name__) from exc
            if r.returncode != 0:
                raise DockerSandboxError(
                    "docker_daemon_unreachable:%s"
                    % r.stderr.decode("utf-8", "replace")[:120])

    def _base_docker_args(self, name, *, label_ts=None):
        """ZERO-06：三条沙箱路径共享的**完全一致**隔离参数。

        任何一条路径单独加参数都会造成边界不一致（历史上
        run() 有 PYTHONDONTWRITEBYTECODE 而 run_workspace() 没有，
        导致只读工作区里 __pycache__ PermissionError 掩盖真实错误）。
        """
        import time as _t
        exp = int(label_ts or (_t.time() + 1800))
        return [
            self.docker, "run", "--name", name,
            "--network=none", "--read-only",
            "--memory=" + self.memory, "--cpus=" + self.cpus,
            "--pids-limit=64", "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONPATH=/work",
            "-e", "HOME=/tmp",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--label", "evoguard.sandbox=1",
            "--label", "evoguard.expires=" + str(exp),
        ]

    def run(self, code, *, timeout_s=None, work_dir=None):
        """在容器里执行 Python 源码，返回 {ok, stdout, stderr, returncode, ...}。

        `work_dir` 给定时，该目录以**只读**方式挂到 /work 并加入 PYTHONPATH，
        使容器内的 `import` 能看到 Agent 刚写入的文件（否则只能反复重写）。
        """
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
            return self._run_inner(code, timeout_s=timeout_s,
                                   work_dir=work_dir)
        finally:
            if got and _SANDBOX_SEMAPHORE is not None:
                _SANDBOX_SEMAPHORE.release()

    def _run_inner(self, code, *, timeout_s=None, work_dir=None):
        import subprocess as _sp
        import tempfile
        import uuid
        limit = float(timeout_s or self.timeout_s)
        name = "evo-sbx-" + uuid.uuid4().hex[:12]
        mounts = []
        if work_dir:
            # DOCKER-OPT-01：把 Agent 实际改动的受控工作目录**只读**挂进
            # /work，否则 Agent 改完文件后无法在容器里 import 验证 ——
            # 它只能反复重写同一段代码，最终被 loop_guard 熔断。
            # 隔离边界不变：仍然 --read-only / --network=none / 非 root，
            # 工作目录挂载为 :ro，容器不可能写穿宿主机。
            root = os.path.realpath(work_dir)
            if os.path.isdir(root):
                mounts = ["-v", root + ":/work:ro", "-w", "/work"]
        with tempfile.TemporaryDirectory(prefix="evo-sbx-") as tmp:
            script = os.path.join(tmp, "script.py")
            with open(script, "w", encoding="utf-8", newline="\n") as f:
                f.write(code)
            cmd = self._base_docker_args(name) + ["--rm"] + mounts + [
                "-v", script + ":/opt/script.py:ro",
                self.image,
                "python", "/opt/script.py",
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


    @contextlib.contextmanager
    def session(self, work_dir, *, timeout_s=None):
        """MOD-MCP-01-T2：复用同一容器跑多条命令，退出必清理。"""
        with docker_session(self, work_dir, timeout_s=timeout_s) as s:
            yield s

    def run_workspace(self, work_dir, cmd, *, timeout_s=None):
        """SCORE-03-T2：把受控工作目录**只读挂载**为 /work，容器内跑命令。

        与 run() 同样：无网、只读根、非 root、限额、tmpfs。额外保证
        宿主机工作区**不可能被容器写穿**（`-v work:/work:ro`），
        容器内的测试缓存写 /tmp。cmd 必须是 list[str]（无 shell）。
        """
        import subprocess as _sp
        import uuid
        if not isinstance(work_dir, str) or not os.path.isdir(work_dir):
            raise DockerSandboxError("invalid_work_dir")
        if not isinstance(cmd, (list, tuple)) or not cmd                 or not all(isinstance(x, str) for x in cmd):
            raise DockerSandboxError("cmd_must_be_argv_list")
        root = os.path.realpath(work_dir)
        limit = float(timeout_s or self.timeout_s)
        name = "evo-ws-" + uuid.uuid4().hex[:12]
        # 只允许 /work 内的相对路径命令；解释器用绝对路径无妨
        cmdv = [str(x) for x in cmd]
        argv = self._base_docker_args(name) + ["--rm",
            "-v", root + ":/work:ro", "-w", "/work", self.image,
        ] + cmdv
        try:
            r = _sp.run(argv, capture_output=True, timeout=limit, check=False)
            out, err = r.stdout or b"", r.stderr or b""
            if isinstance(out, str): out = out.encode("utf-8")
            if isinstance(err, str): err = err.encode("utf-8")
            return {"ok": r.returncode == 0,
                    "stdout": out[:self.max_output_bytes].decode("utf-8", "replace"),
                    "stderr": err[:self.max_output_bytes].decode("utf-8", "replace"),
                    "returncode": r.returncode,
                    "work_dir_read_only": True,
                    "truncated": len(out) > self.max_output_bytes or
                                 len(err) > self.max_output_bytes}
        except _sp.TimeoutExpired:
            _sp.run([self.docker, "rm", "-f", name],
                    capture_output=True, timeout=10, check=False)
            return {"ok": False, "stdout": "", "stderr": "timeout",
                    "returncode": -1, "timeout": True}
        except Exception as exc:                            # noqa: BLE001
            _sp.run([self.docker, "rm", "-f", name],
                    capture_output=True, timeout=10, check=False)
            raise DockerSandboxError("workspace_run_failed:"
                                     + type(exc).__name__) from exc


_SESSION_LABEL = "evoguard.sandbox=1"
_SESSION_NAMES = set()
_SESSION_LOCK = threading.Lock()
_SESSION_TTL_S = 1800


def _reap_orphan_containers(docker_bin):
    """回收带本工具 label 且已过 TTL 的残留容器（只回收我们自己的）。"""
    import subprocess as _sp
    import time as _t
    try:
        r = _sp.run([docker_bin, "ps", "-a", "--filter",
                     "label=" + _SESSION_LABEL, "--format",
                     "{{.Names}}|{{.Label .Labels \"evoguard.expires\"}}"],
                    capture_output=True, timeout=10, text=True, check=False)
    except Exception:                                      # noqa: BLE001
        return 0
    now = _t.time()
    n = 0
    for line in (r.stdout or "").splitlines():
        parts = line.split("|")
        if len(parts) != 2:
            continue
        try:
            exp = float(parts[1])
        except (ValueError, TypeError):
            continue
        if exp <= now:
            _sp.run([docker_bin, "rm", "-f", parts[0]],
                    capture_output=True, timeout=10, check=False)
            n += 1
    return n


def _atexit_reap():
    """进程退出时兜底清理本进程创建、还活着的会话容器。"""
    import subprocess as _sp
    with _SESSION_LOCK:
        names = list(_SESSION_NAMES)
    for name in names:
        # 不知道用哪个 docker bin：从已登记的沙箱取，取不到就跳过
        bin_ = getattr(_atexit_reap, "docker_bin", "docker")
        _sp.run([bin_, "rm", "-f", name],
                capture_output=True, timeout=5, check=False)


@contextlib.contextmanager
def docker_session(box, work_dir, *, timeout_s=None):
    """MOD-MCP-01-T2：受控会话复用（同任务内免重复冷启动）。

    隔离参数与 run_workspace 完全一致：--network=none、--read-only、
    非 root、cap-drop ALL、pids-limit、tmpfs，工作目录 /work:ro。
    退出时**必定** docker rm -f（finally），不留残留容器。
    """
    import contextlib as _cl
    import subprocess as _sp
    import uuid as _uuid
    if not isinstance(work_dir, str) or not os.path.isdir(work_dir):
        raise DockerSandboxError("invalid_work_dir")
    root = os.path.realpath(work_dir)
    name = "evo-sess-" + _uuid.uuid4().hex[:12]
    limit = float(timeout_s or box.timeout_s)
    import atexit as _atexit
    _atexit_reap.docker_bin = box.docker
    _atexit.register(_atexit_reap)
    _reap_orphan_containers(box.docker)   # 建新会话前先回收过期孤儿
    with _SESSION_LOCK:
        _SESSION_NAMES.add(name)
    argv = box._base_docker_args(name) + ["-d",
        "-v", root + ":/work:ro", "-w", "/work", box.image,
        "tail", "-f", "/dev/null",
    ]
    try:
        r = _sp.run(argv, capture_output=True, timeout=limit, check=False)
        if r.returncode != 0:
            raise DockerSandboxError(
                "session_start_failed:" +
                (r.stderr or b"").decode("utf-8", "replace")[:120])
    except Exception as exc:
        _sp.run([box.docker, "rm", "-f", name],
                capture_output=True, timeout=10, check=False)
        if isinstance(exc, DockerSandboxError):
            raise
        raise DockerSandboxError("session_start_failed:" +
                                 type(exc).__name__) from exc

    class _Session:
        def __init__(self, docker_bin, cname, timeout, max_bytes):
            self.docker, self.name = docker_bin, cname
            self.timeout, self.max_bytes = timeout, max_bytes
            self.closed = False

        def run(self, cmd, *, timeout_s=None):
            if self.closed:
                raise DockerSandboxError("session_closed")
            if not isinstance(cmd, (list, tuple)) or not cmd \
                    or not all(isinstance(x, str) for x in cmd):
                raise DockerSandboxError("cmd_must_be_argv_list")
            argv2 = [self.docker, "exec", self.name] + [str(x) for x in cmd]
            try:
                r = _sp.run(argv2, capture_output=True,
                            timeout=float(timeout_s or self.timeout),
                            check=False)
                out, err = r.stdout or b"", r.stderr or b""
                if isinstance(out, str): out = out.encode("utf-8")
                if isinstance(err, str): err = err.encode("utf-8")
                return {"ok": r.returncode == 0,
                        "stdout": out[:self.max_bytes].decode(
                            "utf-8", "replace"),
                        "stderr": err[:self.max_bytes].decode(
                            "utf-8", "replace"),
                        "returncode": r.returncode,
                        "reused_container": True}
            except _sp.TimeoutExpired:
                return {"ok": False, "stdout": "", "stderr": "timeout",
                        "returncode": -1, "timeout": True}
        def close(self):
            self.closed = True

    sess = _Session(box.docker, name, limit, box.max_output_bytes)
    try:
        yield sess
    finally:
        sess.close()
        with _SESSION_LOCK:
            _SESSION_NAMES.discard(name)
        _sp.run([box.docker, "rm", "-f", name],
                capture_output=True, timeout=10, check=False)
        # docker rm -f 可能异步返回：轮询确认真的消失，
        # 否则「退出必清理」的承诺会变成静默失效。
        import time as _t
        for _ in range(20):
            chk = _sp.run([box.docker, "inspect", name],
                          capture_output=True, timeout=5, check=False)
            if chk.returncode != 0:
                break
            _t.sleep(0.1)

class WorkspaceSnapshot:
    """AGENT-KERN-01-T1：工作区快照/回滚。

    「改代码 -> 跑测试」回合里，改动可能让原本通过的测试退化。自动保留
    改动前快照，退化时一键还原到干净状态，再换思路（Reflect 后的下一策略）。
    只快照受控 root 下的普通文件，不碰外部路径。
    """

    def __init__(self, root, *, keep=8):
        self.root = os.path.realpath(os.path.abspath(root))
        self.keep = int(keep)
        self._snaps = []            # list[(label, {rel: bytes})]

    def _collect(self):
        files = {}
        for dirpath, _dirs, names in os.walk(self.root):
            for n in names:
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, self.root).replace("\\", "/")
                if rel.split("/")[0].startswith("."):
                    continue        # 跳过隐藏目录（含 .git 等）
                try:
                    if os.path.getsize(full) > MAX_BYTES:
                        continue    # 超大文件不纳入快照
                    with open(full, "rb") as f:
                        files[rel] = f.read()
                except OSError:
                    continue
        return files

    def snapshot(self, label="before"):
        snap = self._collect()
        self._snaps.append((str(label), snap))
        if len(self._snaps) > self.keep:
            self._snaps.pop(0)
        return {"label": str(label), "files": len(snap)}

    def rollback(self, label=None):
        """还原到最近一次（或指定 label）快照：删除多余文件 + 恢复内容。"""
        if not self._snaps:
            raise SandboxError("no_snapshot")
        if label is None:
            _lbl, snap = self._snaps[-1]
        else:
            hit = [s for s in self._snaps if s[0] == label]
            if not hit:
                raise SandboxError("snapshot_label_not_found:%s" % label)
            snap = hit[-1][1]
        current = self._collect()
        for rel in set(current) - set(snap):
            try:
                os.unlink(os.path.join(self.root, rel.replace("/", os.sep)))
            except OSError:
                pass
        for rel, data in snap.items():
            full = os.path.join(self.root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
        return {"restored": len(snap),
                "removed": len(set(current) - set(snap))}


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
        "param_keys": ["path"], "required": ["path"],
    }, scope="read_only", source="coding_bench")
    reg.register("fs.write", {
        "identities": ["coder"], "allowed_domains": [],
        "allowed_resource_pattern": None, "writable_paths": ["*"],
        "max_amount": None, "methods": ["write"], "side_effect": "write",
        "requires_approval": True,
        "param_keys": ["path", "content", "approval_request_id"],
        "required": ["path", "content"],
    }, scope="write", source="coding_bench")
    reg.register("code.run_in_sandbox", {
        "identities": ["coder"], "allowed_domains": [],
        "allowed_resource_pattern": None, "writable_paths": [],
        "max_amount": None, "methods": ["read"], "side_effect": "none",
        "requires_approval": False,
        "param_keys": ["code"], "required": ["code"],
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
            # DOCKER-OPT-01：带上受控工作目录（只读挂载），让容器内的
            # import 能看到 Agent 刚写入的文件。
            r = docker_sandbox.run(str(args.get("code") or ""), work_dir=root)
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


def fs_patch(path, old_text, new_text, *, root=None, snapshot=None,
             max_bytes=MAX_BYTES):
    """SCORE-03-T2：精准局部编辑（search/replace 必须唯一匹配）。

    与整文件覆写相比：不会碰无关行，冲突（0 处或多处匹配）直接拒绝，
    不做任何「猜一个最接近的位置」的模糊替换。写前自动触发快照，
    退化时可回滚。root 限定路径围栏（realpath + 分隔符）。
    """
    import os as _os
    base = _os.path.realpath(_os.path.abspath(root or _os.getcwd()))
    target = _os.path.realpath(_os.path.abspath(path))
    if not (target == base or target.startswith(base + _os.sep)):
        raise SandboxError("path_outside_root:%s" % str(path)[:50])
    if not _os.path.isfile(target):
        raise SandboxError("not_a_file:%s" % str(path)[:50])
    if _os.path.getsize(target) > max_bytes:
        raise SandboxError("file_too_large")
    if not isinstance(old_text, str) or not old_text:
        raise SandboxError("old_text_empty")
    if not isinstance(new_text, str):
        raise SandboxError("new_text_must_be_str")
    with open(target, "r", encoding="utf-8") as f:
        src = f.read()
    n = src.count(old_text)
    if n == 0:
        raise SandboxError("old_text_not_found")
    if n > 1:
        raise SandboxError("old_text_ambiguous:%d" % n)
    if snapshot is not None:
        try:
            snapshot.capture()
        except Exception:                                  # noqa: BLE001
            raise SandboxError("snapshot_failed")
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(src.replace(old_text, new_text, 1))
    return {"ok": True, "path": target, "matches": 1,
            "bytes_before": len(src.encode("utf-8")),
            "bytes_after": len(src.replace(old_text, new_text, 1).encode("utf-8"))}


# ---- MOD-AGENT-01-T2：纯标准库 ast 只读符号检查（进 Docker 前 1ms 拦截）----
#: 内置名 + 常见 dunder（不算未定义）
_PY_BUILTINS = frozenset(dir(__builtins__)) if isinstance(__builtins__, object) else frozenset()


def check_symbols(root, *, files=None):
    """检查 Python 文件语法错与未定义名（零依赖、不执行任何代码）。

    `files` 限定要检查的相对路径；None 表示 root 下全部 .py。
    **跨文件符号**通过「同工作区其它 .py 的模块级符号」补全白名单：
    这样 solution.py 引用 registry.FIELDS 而没 import 时，FIELDS 若恰好
    在 registry.py 里定义，会给出精确的「缺少 import」提示（真实 coupling
    失败根因），而普通未定义名也照样报。

    返回 {ok, errors, checked}，失败不静默。
    """
    import ast as _ast
    import builtins as _bi
    errors = []
    checked = 0
    base = os.path.realpath(os.path.abspath(root))
    if not os.path.isdir(base):
        return {"ok": False, "errors": ["root_not_directory"],
                "checked": 0}

    def _collect_module_symbols():
        """扫描工作区所有 .py 模块级定义名（不执行）。"""
        names = set()
        for dirpath, dirs, names_ in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names_:
                if not n.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, n)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        tree = _ast.parse(f.read())
                except Exception:                          # noqa: BLE001
                    continue
                for node in tree.body:
                    if isinstance(node, (_ast.FunctionDef,
                                         _ast.AsyncFunctionDef,
                                         _ast.ClassDef)):
                        names.add(node.name)
                    elif isinstance(node, _ast.Assign):
                        for t in node.targets:
                            if isinstance(t, _ast.Name):
                                names.add(t.id)
                    elif isinstance(node, _ast.AnnAssign) and \
                            isinstance(node.target, _ast.Name):
                        names.add(node.target.id)
        return names

    workspace_symbols = _collect_module_symbols()
    builtin_names = set(dir(_bi))
    targets = []
    if files is None:
        for dirpath, dirs, names_ in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            targets += [os.path.join(dirpath, n) for n in names_
                        if n.endswith(".py")]
    else:
        for rel in files:
            full = os.path.realpath(os.path.join(base, rel))
            if not full.startswith(base):
                errors.append("path_outside_root:" + str(rel)[:60])
                continue
            targets.append(full)

    for fp in targets:
        checked += 1
        rel = os.path.relpath(fp, base).replace("\\", "/")
        try:
            with open(fp, "r", encoding="utf-8") as f:
                src = f.read()
        except Exception as exc:                            # noqa: BLE001
            errors.append("%s: read_failed:%s" % (rel, type(exc).__name__))
            continue
        try:
            tree = _ast.parse(src, filename=rel)
        except SyntaxError as exc:
            errors.append("%s: SyntaxError line %s: %s"
                          % (rel, exc.lineno, str(exc.msg)[:100]))
            continue
        # 本文件绑定名：import、赋值、函数/类、参数、局部赋值
        bound = set(builtin_names)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    bound.add((a.asname or a.name.split(".")[0]))
            elif isinstance(node, _ast.ImportFrom):
                for a in node.names:
                    bound.add(a.asname or a.name)
            elif isinstance(node, _ast.Name) and \
                    isinstance(node.ctx, (_ast.Store, _ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (_ast.FunctionDef,
                                   _ast.AsyncFunctionDef, _ast.ClassDef)):
                bound.add(node.name)
                for arg in list(node.args.args) + list(node.args.kwonlyargs):
                    bound.add(arg.arg)
            elif isinstance(node, _ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, _ast.comprehension):
                if isinstance(node.target, _ast.Name):
                    bound.add(node.target.id)
        reported = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load):
                n = node.id
                if n in bound or n in builtin_names:
                    continue
                if n in workspace_symbols:
                    reported.append((node.lineno, n,
                                     "跨文件符号 %s 未 import（可能是漏写 import）" % n))
                else:
                    reported.append((node.lineno, n, "未定义名 %s" % n))
        for line, name, msg in reported[:12]:
            errors.append("%s:%d: %s" % (rel, line, msg))
    return {"ok": not errors, "errors": errors, "checked": checked}
