#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 --provider 的 preset 名正确解析成 ProviderSpec。

坑：`ProviderSpec(backend="deepseek")` **不会**自动套用 PROVIDER_PRESETS
里的 base_url/api_key_env，get_provider(force="deepseek") 会落到
LocalProvider 去连 127.0.0.1:1234 —— 调用失败后判官静默回退规则，
就会出现"报告写着 deepseek、实际一次模型都没调"的假实测。

本 helper 显式查表，返回 (spec, backend)，并校验 key 存在。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import gateway as GW                                        # noqa: E402


def resolve(provider, *, model=None, timeout=20, require_key=True):
    """返回 (ProviderSpec, backend_for_get_provider)。"""
    pre = GW.PROVIDER_PRESETS.get(str(provider or "").strip().lower())
    if pre:
        backend, base, keyenv, default_model, _tools = pre
        spec = GW.ProviderSpec(backend=backend, base_url=base,
                               model=model or default_model,
                               api_key_env=keyenv, timeout=timeout)
    else:
        # 未知 preset：当作 backend 名（openai_compat/ollama/...），
        # 这时调用方必须自己给 --base/--api-key-env
        spec = GW.ProviderSpec(backend=provider, model=model,
                               timeout=timeout)
        backend = provider
    if require_key and spec.api_key_env and not os.environ.get(
            spec.api_key_env):
        raise RuntimeError(
            "provider preset %s 需要环境变量 %s（未设置）"
            % (provider, spec.api_key_env))
    return spec, backend
