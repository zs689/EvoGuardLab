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
    if profile not in PROFILES:
        raise ValueError(
            "未知 EVOGUARD_PROFILE=%r（可选 %s）"
            % (profile, "/".join(sorted(PROFILES))))
    if profile != "strict":
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
        "profile": profile if profile in PROFILES else "unknown",
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


# ---- ZERO-01：开箱默认硬拦截（Secure by Default）---------------------
# 库层默认保持 audit 不漂移（历史对照实验/1300+ 单测依赖它）；
# 但**对外服务与代理入口**裸启动时必须真拦截，不能"只记账不拦"。
# 原则：只在用户未显式设置时补默认；显式值（含 audit/dev）一律尊重。
ENTRY_SECURE_DEFAULTS = {
    "AGENTLAB_SECURITY_MODE": "enforce",
    "AGENTLAB_EXEC_MODE": "controlled",
    "EVOGUARD_LIGHT_GUARD": "1",
    "EVOGUARD_UNKNOWN_TOOL": "deny",
    "EVOGUARD_PROFILE": "secure-default",
}

# 显式指定这些档位 = 用户主动选择观察/兼容，不覆盖
_OPT_OUT_PROFILES = frozenset({"demo", "dev", "audit", "legacy", "strict"})
# apply_profile 接受的档位：只有 strict 施加硬门禁；其余都是显式 opt-out
PROFILES = _OPT_OUT_PROFILES | {"secure-default"}


def entry_defaults(environ=None):
    """返回**服务入口**的有效环境视图（不修改传入 environ）。

    仅用于 CLI/API/代理入口；库函数继续用原来的默认，
    这样历史 audit 对照脚本显式设值后行为完全不变。
    """
    env = environ if environ is not None else os.environ
    out = dict(env)
    explicit_profile = str(
        env.get("EVOGUARD_PROFILE") or env.get("AGENTLAB_SECURITY_MODE")
        or "").strip().lower()
    if explicit_profile in _OPT_OUT_PROFILES:
        return out                      # 显式选择，绝不覆盖
    for key, value in ENTRY_SECURE_DEFAULTS.items():
        if key not in out:
            out[key] = value
    return out


def apply_entry_defaults(environ=None):
    """把服务入口默认写进环境（调用点：serve / 代理入口启动前）。"""
    env = environ if environ is not None else os.environ
    changed = []
    for k, v in entry_defaults(env).items():
        if k not in env:
            env[k] = v
            changed.append(k)
    return sorted(changed)
