# -*- coding: utf-8 -*-
"""脱敏：实验产物落盘前移除开发机绝对路径（reproducibility 要求）。"""
import getpass
import os
import re
import tempfile


def redact(text):
    """把本机用户目录/temp 根目录替换成占位符。"""
    home = os.path.expanduser("~")
    user = getpass.getuser()
    tmp = tempfile.gettempdir()
    reps = []
    for p in (home, tmp):
        if p and len(p) > 3:
            reps.append(p)
    # Windows 反斜杠在 JSON 里是 \\，两种都要处理
    for p in list(reps):
        reps.append(p.replace("\\", "\\\\"))
    for p in reps:
        text = text.replace(p, "<HOME>" if p.startswith(home) else "<TMP>")
    # 用户目录名（如 <user-home>）单独兜底
    if user and len(user) > 2:
        text = re.sub(r"(Users[\\\\/])" + re.escape(user) + r"\b",
                      r"\1<USER>", text)
    return text
