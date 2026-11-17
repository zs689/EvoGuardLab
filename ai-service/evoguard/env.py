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
