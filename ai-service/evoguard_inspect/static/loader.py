# -*- coding: utf-8 -*-
"""加载 Agent 定义：单文件定义 或 代码仓库（只读文本，绝不执行）。

**安全边界（写死在代码里，不靠自觉）**：
  1. 只以文本方式打开文件，从不 import / exec / 调用子进程；
  2. 跳过 .git / node_modules / venv / 二进制 / 超过 1MB 的文件；
  3. 仓库模式置信度一律 low —— 从源码猜 prompt 边界必然不准，
     结论必须带着这个标记，否则用户会把猜测当事实。
"""
import json
import os

from .model import AgentDef, from_json, from_repo_files

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".idea", ".mypy_cache", "site-packages"}
SCAN_EXT = {".py", ".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".jinja", ".j2"}
MAX_BYTES = 1024 * 1024
MAX_FILES = 4000


def load_definition(path):
    """单个 JSON 定义文件。支持扁平格式与 OpenAI tools 格式。"""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return from_json(doc, source=os.path.abspath(path))


def _iter_texts(root):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if n >= MAX_FILES:
                return
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SCAN_EXT:
                continue
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > MAX_BYTES:
                    continue
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            n += 1
            yield os.path.relpath(p, root), text


def load_repo(root):
    """扫描仓库，启发式提取 Agent 定义。不做任何代码执行。"""
    if not os.path.isdir(root):
        raise ValueError("不是目录：%s" % root)
    files = list(_iter_texts(root))
    defn = from_repo_files(files, source=root)
    defn.notes.append("共扫描 %d 个文本文件" % len(files))
    return defn


def load_repo_split(root):
    """把目录下的**一级子目录**各当一个 Agent。

    为什么需要：一个仓库里通常有多个 Agent（examples/customer_service、
    examples/research_bot…）。全仓合并成一个的话，工具会堆到几十个，
    `tool_too_many` 必报 —— 那是抽样方式的问题，不是被检对象的问题。
    """
    if not os.path.isdir(root):
        raise ValueError("不是目录：%s" % root)
    groups = {}
    for rel, text in _iter_texts(root):
        parts = rel.split(os.sep)
        key = parts[0] if len(parts) > 1 else "__root__"
        groups.setdefault(key, []).append((rel, text))

    out = []
    for key, files in sorted(groups.items()):
        d = from_repo_files(files, source=root if key == "__root__" else os.path.join(root, key))
        d.name = os.path.basename(root) if key == "__root__" else key
        if d.system_prompt or d.tools:
            d.notes.append("来自子目录 %s（%d 个文本文件）" % (key, len(files)))
            out.append(d)
    return out


def load_any(path):
    """给路径自动判断：目录当仓库扫，文件当定义读。"""
    if os.path.isdir(path):
        return load_repo(path)
    return load_definition(path)
