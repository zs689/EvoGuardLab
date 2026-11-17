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

#: 允许读写的扩展名（默认只读白名单；写入另有一套更严格的）
READ_EXT = frozenset({".txt", ".md", ".json", ".csv", ".log", ".yaml",
                      ".yml", ".py", ".toml", ".ini"})
WRITE_EXT = frozenset({".txt", ".md", ".json", ".csv", ".log"})
MAX_BYTES = 1 << 20                      # 单文件 1 MiB
_BAD_NAME = re.compile(r"[\x00<>:\"|?*]")


class SandboxError(PermissionError):
    """路径/权限被拒绝。调用方据此记 executed=False。"""


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


def describe():
    return {
        "level": "application-level path fence (NOT an OS sandbox)",
        "root_required": True,
        "default_allow_write": False,
        "read_ext": sorted(READ_EXT),
        "write_ext": sorted(WRITE_EXT),
        "max_bytes": MAX_BYTES,
        "protections": ["absolute path", ".. traversal", "symlink escape",
                        "extension allowlist", "size cap"],
        "not_protected": ["OS-level resource limits", "network egress",
                          "process isolation (需容器/低权限用户)"],
    }
