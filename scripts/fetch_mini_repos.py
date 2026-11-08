# -*- coding: utf-8 -*-
"""从 jsdelivr 目录接口挑出框架的**官方示例文件**，构造迷你仓库供静态扫描。

为什么需要：GitHub archive 整包走代理时会被截断（crewAI 244MB / pydantic-ai 70MB），
而静态检查只需要「Agent 定义」——也就是示例里的 prompt / tools / config，
不需要整个库源码。所以只取少量小文件，既避开截断，也减少不相关代码带来的噪音。

安全边界（和整包下载一致）：
  - 只下载文本文件，绝不执行、不 import、不安装；
  - 路径做白名单校验，拒绝 .. 与绝对路径（防 zip/路径穿越）；
  - 单文件上限 200KB，单框架上限 12 个文件；
  - 所有产物落在 _sandbox/ 下，用完由清理脚本删除。

跑法：python scripts/fetch_mini_repos.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = os.path.join(ROOT, "_sandbox")

# 显式路径，不依赖大仓库的目录树接口（crewAI / pydantic-ai 的树太大，
# 走代理会超时返回一个空壳，第一版因此「挑到 0 个文件」）。
# 这些路径来自各仓库公开的 examples / samples 目录页。
REPOS = {
    "autogen": {
        "repo": "microsoft/autogen",
        "paths": [
            "README.md",
            "python/samples/agentchat_streamlit/agent.py",
            "python/samples/agentchat_streamlit/main.py",
            "python/samples/core_chainlit/app_agent.py",
            "python/samples/agentchat_graphrag/app.py",
            "python/samples/agentchat_websockets/app.py",
        ],
    },
    "pydanticai": {
        "repo": "pydantic/pydantic-ai",
        "paths": [
            "examples/README.md",
            "examples/pydantic_ai_examples/weather_agent.py",
            "examples/pydantic_ai_examples/bank_support.py",
            "examples/pydantic_ai_examples/flight_booking.py",
            "examples/pydantic_ai_examples/rag.py",
            "examples/pydantic_ai_examples/sql_gen.py",
            "examples/pydantic_ai_examples/data_analyst.py",
            "examples/pydantic_ai_examples/medical_agent_delegation.py",
        ],
    },
    "crewai": {
        "repo": "crewAIInc/crewAI",
        "paths": [
            "README.md",
            "docs/en/concepts/agents.mdx",
            "docs/en/concepts/tasks.mdx",
            "docs/en/concepts/tools.mdx",
            "lib/crewai/src/crewai/agent.py",
            "lib/crewai/src/crewai/tools/base_tool.py",
        ],
    },
}
MAX_FILES = 12
MAX_BYTES = 200 * 1024
# 优先示例目录，其次才是其它目录
PREFER = ("example", "sample", "demo", "cookbook", "quickstart", "notebook")


def curl(url, out=None, timeout=30):
    cmd = ["curl", "-sL", "--max-time", str(timeout)]
    if out:
        cmd += ["-o", out]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if out:
            return os.path.exists(out) and os.path.getsize(out) > 0
        return p.stdout
    except Exception:
        return b"" if not out else False


def walk(node, prefix, out):
    """把 jsdelivr 的目录树摊平成 (path, size) 列表。"""
    for f in node.get("files", []) or []:
        name = f.get("name", "")
        if not name or "/" in name or name.startswith("."):
            continue
        path = prefix + name
        if f.get("type") == "directory":
            walk(f, path + "/", out)
        else:
            out.append((path, f.get("size", 0) or 0))


def safe_path(rel):
    """拒绝路径穿越 —— 从外部拿到的文件名不能当成本地路径直接用。"""
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    if "\\" in rel or ":" in rel:
        return None
    return rel


def score(path):
    """挑文件：示例目录优先、.py/md 优先、路径短一点优先（通常是入门示例）。"""
    low = path.lower()
    if not low.endswith((".py", ".md", ".json", ".yaml", ".yml")):
        return -1
    if any(x in low for x in ("/test", "test_", "/tests/", ".ipynb")):
        return -1
    s = 0
    if any(k in low for k in PREFER):
        s += 100
    if low.endswith(".py"):
        s += 20
    if "agent" in low or "tool" in low or "prompt" in low:
        s += 10
    s -= len(path) / 100.0
    return s


def main():
    for short, spec in REPOS.items():
        repo = spec["repo"]
        dst = os.path.join(SANDBOX, short + "_mini")
        os.makedirs(dst, exist_ok=True)
        got, tried = 0, 0
        for rel in spec["paths"][:MAX_FILES]:
            rel = safe_path(rel)
            if not rel:
                continue
            tried += 1
            target = os.path.join(dst, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # cdn.jsdelivr.net 的 /gh/ 单文件在本机不通（实测 000），
            # 走已验证可用的 raw 代理通道。
            url = ("https://ghfast.top/https://raw.githubusercontent.com/"
                   "%s/main/%s" % (repo, rel))
            ok = curl(url, out=target, timeout=25)
            if ok and os.path.exists(target):
                head = open(target, "rb").read(64).decode("utf-8", "replace")
                # GitHub raw 的 404 是纯文本 "404: Not Found"，不能当源码收下
                if head.startswith("404") or "<html" in head.lower() \
                        or os.path.getsize(target) > MAX_BYTES:
                    ok = False
            if ok:
                got += 1
            elif os.path.exists(target):
                os.remove(target)
        print("[%s] %-12s 取回 %d/%d 个 → %s" %
              ("√" if got else "×", short, got, tried,
               os.path.relpath(dst, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
