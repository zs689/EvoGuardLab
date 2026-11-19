# -*- coding: utf-8 -*-
"""环境变量前缀兼容：`EVOGUARD_*` 优先，向下兼容历史 `AGENTLAB_*`。

历史上所有配置都用 `AGENTLAB_` 前缀，而包名是 `evoguardlab`，对外割裂。
逐个改调用点风险大（散落在十几个模块、几十处），所以改成**启动时映射一次**：
设了 `EVOGUARD_X` 而没设 `AGENTLAB_X` 时，把值复制过去，其余代码零改动。

调用点：`ai-service/api/app.py`、`evoguard_cli.py` 启动入口。
"""
import os

NEW = "EVOGUARD_"
OLD = "AGENTLAB_"


def alias_env(environ=None):
    """把 EVOGUARD_* 映射成 AGENTLAB_*（不覆盖已显式设置的旧名）。

    返回被映射的旧变量名列表，便于启动日志如实打印。
    """
    env = environ if environ is not None else os.environ
    mapped = []
    for k in list(env.keys()):
        if not k.startswith(NEW):
            continue
        old = OLD + k[len(NEW):]
        if old not in env:
            env[old] = env[k]
            mapped.append(old)
    return sorted(mapped)


# SCORE-05-T2：一键安全预设档位。
# 生产部署最容易漏配十几个环境变量，导致「以为开了防护其实半裸奔」。
# strict 档位把这些硬门禁一次性置位；demo（默认）完全不动，
# 保证既有离线回归测试行为不变。显式设置优先于档位默认。
PROFILE_STRICT_DEFAULTS = {
    "AGENTLAB_SECURITY_MODE": "enforce",
    "AGENTLAB_EXEC_MODE": "controlled",
    "EVOGUARD_LIGHT_GUARD": "1",
    "EVOGUARD_UNKNOWN_TOOL": "deny",
    "EVOGUARD_OUTPUT_BLOCK": "1",
}


def apply_profile(environ=None):
    """根据 EVOGUARD_PROFILE 应用默认安全档位；返回实际设置的键名。"""
    env = environ if environ is not None else os.environ
    profile = str(env.get("EVOGUARD_PROFILE") or "demo").strip().lower()
    if profile not in ("demo", "strict"):
        raise ValueError("未知 EVOGUARD_PROFILE=%r（可选 demo/strict）" % profile)
    if profile == "demo":
        return []
    changed = []
    for key, value in PROFILE_STRICT_DEFAULTS.items():
        if key not in env:
            env[key] = value
            changed.append(key)
    return sorted(changed)


def profile_meta(environ=None):
    """给 /api/meta 用的 Effective 配置视图（不泄露密钥）。"""
    env = environ if environ is not None else os.environ
    profile = str(env.get("EVOGUARD_PROFILE") or "demo").strip().lower()
    return {
        "profile": profile if profile in ("demo", "strict") else "unknown",
        "hard_gates": {
            "security_enforce":
                str(env.get("AGENTLAB_SECURITY_MODE") or "").lower() == "enforce",
            "exec_controlled":
                str(env.get("AGENTLAB_EXEC_MODE") or "").lower() == "controlled",
            "light_guard": str(env.get("EVOGUARD_LIGHT_GUARD") or "") == "1",
            "unknown_tool_deny":
                str(env.get("EVOGUARD_UNKNOWN_TOOL") or "") == "deny",
            "output_block": str(env.get("EVOGUARD_OUTPUT_BLOCK") or "") == "1",
        },
    }
