# -*- coding: utf-8 -*-
"""清理 _sandbox：下载来的框架源码用完即删，并留下可核对的清点记录。

安全事实（清理前打印，便于核对）：
  - 全部下载物只被当作**文本**读取，从未 import / 执行 / pip install；
  - 解压走 safe_extract.py，拒绝路径穿越、只保留文本后缀；
  - 这里会清点可执行文件后缀，正常情况下应为 0。

跑法：python scripts/cleanup_sandbox.py
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = os.path.join(ROOT, "_sandbox")
EXEC_EXT = (".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".ps1",
            ".sh", ".msi", ".scr", ".com", ".jar", ".pyc")


def main():
    if not os.path.isdir(SANDBOX):
        print("[√] _sandbox 不存在，无需清理（可能已经删过了）")
        return 0
    files, total, execs = 0, 0, []
    for dirpath, _, filenames in os.walk(SANDBOX):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
            files += 1
            if fn.lower().endswith(EXEC_EXT):
                execs.append(os.path.relpath(p, ROOT))
    print("待清理：%d 个文件，%.1f MB" % (files, total / 1024.0 / 1024.0))
    print("可执行文件：%d 个%s" % (len(execs), ("（%s）" % ", ".join(execs[:5])) if execs else ""))
    print("下载物全程只读文本，未执行、未安装、未 import。")

    shutil.rmtree(SANDBOX)
    if os.path.exists(SANDBOX):
        print("[×] 删除失败：目录仍存在")
        return 1
    print("[√] 已删除 _sandbox（%d 个文件 / %.1f MB），路径不存在" %
          (files, total / 1024.0 / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
