# -*- coding: utf-8 -*-
"""合成工具测试的隔离子进程入口（静态模块，随包分发）。

OSS-02：此前是 run_tests() 在**运行期**把这个文件写进包目录，在非 editable
安装（pip install .）或只读 site-packages / Docker --read-only 下会直接
PermissionError。现在它是正式源码，只读执行，零运行期写包目录。

由 run_tests() 以 `python -I -c <bootstrap>` 启动：bootstrap 只做
sys.path 注入 + import 本模块；子进程 env 已被清洗（无宿主机密钥）。
stdin 收 JSON payload，stdout 末行输出 JSON 结果。
"""
import json as _json
import sys as _sys


def main():
    try:
        # -I 隔离下 stdin 编码跟 locale；显式按 UTF-8 读，避免中文 docstring
        # 在 Windows 非 UTF-8 locale 下 JSONDecodeError。
        raw = _sys.stdin.buffer.read()
        payload = _json.loads(raw.decode("utf-8"))
        import tool_synthesis as _ts
        _ts._run_tests_child(payload["source"], payload["func"],
                             payload["tests"])
        print(_json.dumps({"ok": True}))
    except BaseException as exc:                            # noqa: BLE001
        print(_json.dumps({"ok": False,
                           "error": type(exc).__name__ + ": " + str(exc)[:300]}))


if __name__ == "__main__":
    main()
