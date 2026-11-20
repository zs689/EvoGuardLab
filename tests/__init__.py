# -*- coding: utf-8 -*-
"""测试套件隔离：不让开发者本地 .env 污染测试环境。

开发者 .env 可能配了云端 provider（如 EVOGUARD_PROVIDER=deepseek），
但测试进程通常没有对应 key。若不隔离，测试会走真实 API 而不是 mock，
导致 500。设 EVOGUARD_SKIP_DOTENV=1 使 gateway 不自动加载 .env。
"""
import os

os.environ.setdefault("EVOGUARD_SKIP_DOTENV", "1")
