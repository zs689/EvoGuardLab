# -*- coding: utf-8 -*-
"""evidence_gate：输出侧事实/证据核验护栏（WS-05）。

从同工作区的 evidence-gate 项目迁入的 6 个模块（clause/docx_reader/corpus/
bm25/gate/tokenize），只依赖标准库。`eg/llm.py` **不迁**：统一用
`evoguard.gateway` 的 Provider，避免两套模型网关代码。
"""
from . import gate, corpus, clause, bm25, docx_reader, tokenize  # noqa: F401

__all__ = ["gate", "corpus", "clause", "bm25", "docx_reader", "tokenize"]
