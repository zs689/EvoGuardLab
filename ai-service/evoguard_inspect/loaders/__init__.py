# -*- coding: utf-8 -*-
"""统一入口：自动识别格式，交给对应 loader。

识别靠特征字段，不靠文件后缀 —— 用户从各种平台导出，后缀常常是 .json 就完事。
"""
import json
import sqlite3

from . import evoguard as evoguard_loader
from . import langsmith, otel

FORMATS = ("otel", "langsmith", "evoguard")


def detect(doc):
    if isinstance(doc, dict):
        if "resourceSpans" in doc or "resource_spans" in doc:
            return "otel"
        if "runs" in doc and isinstance(doc["runs"], list):
            return "langsmith"
        if "spans" in doc and isinstance(doc["spans"], list):
            # 有些导出把 spans 摊平了，仍然按 OTel 处理
            return "otel"
    if isinstance(doc, list) and doc and isinstance(doc[0], dict):
        if "run_type" in doc[0]:
            return "langsmith"
    return None


def _is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def load_file(path, fmt=None):
    # 本项目自己的运行库：按文件头判断，不看后缀。
    # 「Agent 已经跑过了」最常见的情形就是手上有一个运行库，
    # 要求用户先导成 JSON 才能检查，是没必要的门槛。
    if fmt in (None, "evoguard") and _is_sqlite(path):
        store = sqlite3.connect(path)
        store.row_factory = sqlite3.Row
        try:
            return evoguard_loader.from_conn(store)
        finally:
            store.close()

    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if fmt is None:
        fmt = detect(doc)
    if fmt is None:
        raise ValueError("认不出格式：既不是 OTLP JSON，也不是 LangSmith runs 列表。\n"
                         "支持的格式：resourceSpans(OTLP) / runs(LangSmith)")
    if fmt == "otel":
        if isinstance(doc, dict) and "spans" in doc and "resourceSpans" not in doc:
            doc = {"resourceSpans": [{"scopeSpans": [{"spans": doc["spans"]}]}]}
        return otel.load(doc)
    return langsmith.load(doc)
