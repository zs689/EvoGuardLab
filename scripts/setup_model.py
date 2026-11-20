#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型接入向导：告诉你「支持哪些 key / 填在哪里 / 现在填了没」。

    python scripts/setup_model.py                # 列出全部预设 + 当前配置状态
    python scripts/setup_model.py deepseek       # 显示某个服务怎么配
    python scripts/setup_model.py --check        # 只检查当前能否连（不发真实请求）

EvoGuardLab 是模型无关的：任意 OpenAI 兼容 API / Anthropic / 本地 Ollama
都能接。**key 永远放在系统环境变量里，不写进代码或 .env 之外的文件。**
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402


def show_one(name, info):
    key = info["api_key_env"]
    if key:
        has = "已设置 ✅" if os.environ.get(key) else "未设置 ❌"
        keyline = "    key 环境变量: %s  [%s]" % (key, has)
    else:
        keyline = "    key: 不需要（本地服务无鉴权）"
    print("  %-14s %-16s %s" % (name, info["backend"], info["default_model"] or "(自动识别)"))
    print("    base_url: %s" % info["base_url"])
    print(keyline)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", nargs="?", default=None,
                    help="预设名，如 deepseek/qwen/ollama；不给则列出全部")
    ap.add_argument("--check", action="store_true", help="检查当前配置")
    a = ap.parse_args()

    presets = GW.list_providers()

    if a.check:
        cur = GW.ProviderSpec.from_env()
        print("=== 当前生效配置 ===")
        print("  provider :", os.environ.get("EVOGUARD_PROVIDER", "(未设)"))
        for k in ("backend", "base_url", "model", "api_key_env"):
            print("  %-9s: %s" % (k, getattr(cur, k)))
        if cur.api_key_env:
            print("  key 状态:", "已设置 ✅" if os.environ.get(cur.api_key_env)
                  else "未设置 ❌ 请设置环境变量 " + cur.api_key_env)
        else:
            print("  key 状态: 本地服务，无需 key")
        return 0

    if a.provider:
        name = a.provider.lower()
        if name not in presets:
            print("未知预设: %s" % name)
            print("可用:", ", ".join(sorted(presets)))
            return 1
        print("=== 接入 %s ===" % name)
        show_one(name, presets[name])
        info = presets[name]
        print("\n  配置三步（PowerShell）：")
        print("    1) [Environment]::SetEnvironmentVariable('EVOGUARD_PROVIDER','%s','User')" % name)
        if info["api_key_env"]:
            print("    2) [Environment]::SetEnvironmentVariable('%s','你的真实key','User')"
                  % info["api_key_env"])
            print("    3) 重开终端，运行: python scripts/setup_model.py --check")
        else:
            print("    2) 启动本地服务，确认监听 %s" % info["base_url"])
            print("    3) 运行: python scripts/setup_model.py --check")
        print("\n  或写进项目 .env（已 gitignore）：EVOGUARD_PROVIDER=%s" % name)
        return 0

    print("=== EvoGuardLab 支持的模型服务（模型无关，一行切换）===\n")
    print("【云端 API · OpenAI 兼容协议】")
    for n, i in presets.items():
        if i["backend"] == "openai_compat" and "local" not in n:
            show_one(n, i)
    print("\n【Anthropic 原生协议】")
    for n, i in presets.items():
        if i["backend"] == "anthropic_compat":
            show_one(n, i)
    print("\n【本地 AI（无需 key）】")
    for n, i in presets.items():
        if i["backend"] in ("ollama",) or "local" in n:
            show_one(n, i)
    print("\n" + "=" * 60)
    print("任何未列出的服务：只要兼容 OpenAI 协议，用 backend=openai_compat")
    print("并自填 EVOGUARD_MODEL_BASE + EVOGUARD_MODEL_MODEL 即可。")
    print("\nKEY 放哪里？→ 系统环境变量（见上方每个服务的 api_key_env）")
    print("查看当前状态: python scripts/setup_model.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
