# -*- coding: utf-8 -*-
"""安全解压：只提取文本文件，拒绝路径穿越，限制体积。

为什么单独写：第三方 zip 里可能有 `../../evil.py` 或几 GB 的压缩炸弹，
`zipfile.extractall` 会照单全收。这里逐条检查后再写盘，且**只写文本类文件**
——可执行文件、二进制一律跳过，反正静态分析也用不到。
"""
import os
import re
import sys
import zipfile

TEXT_EXT = {".py", ".yaml", ".yml", ".json", ".toml", ".md", ".txt",
            ".jinja", ".j2", ".cfg", ".ini"}
MAX_TOTAL = 400 * 1024 * 1024      # 解压总量上限
MAX_ONE = 2 * 1024 * 1024          # 单文件上限


_DRIVE = re.compile(r"^[A-Za-z]:")


def _suspicious(name):
    """路径可疑判定：跨平台按最坏情况来。

    统一把反斜杠当分隔符，`..\\evil.py` 才算穿越；盘符路径（`C:/...`）
    在 POSIX 上只是怪文件名，但同一个包可能在 Windows 上解，一律拒绝。
    """
    n = name.replace("\\", "/")
    if n.startswith("/"):
        return True
    if _DRIVE.match(n):
        return True
    return ".." in n.split("/")


def safe_extract(zip_path, dest):
    os.makedirs(dest, exist_ok=True)
    dest_abs = os.path.abspath(dest)
    total, kept, skipped = 0, 0, 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            # 1) 拒绝绝对路径与路径穿越（含盘符、反斜杠写法）
            if _suspicious(name):
                print("[skip] 可疑路径：%s" % name)
                skipped += 1
                continue
            # 2) 只留文本
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXT:
                skipped += 1
                continue
            # 3) 体积限制
            if info.file_size > MAX_ONE or total + info.file_size > MAX_TOTAL:
                skipped += 1
                continue
            target = os.path.abspath(os.path.join(dest_abs, name))
            if not target.startswith(dest_abs + os.sep):
                print("[skip] 越界：%s" % name)
                skipped += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            total += info.file_size
            kept += 1
    print("解压完成：保留 %d 个文本文件（%.1f MB），跳过 %d 个"
          % (kept, total / 1048576.0, skipped))
    return dest


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法：python safe_extract.py <zip> <dest>")
        sys.exit(2)
    safe_extract(sys.argv[1], sys.argv[2])
