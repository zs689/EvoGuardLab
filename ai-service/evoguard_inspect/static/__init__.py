# -*- coding: utf-8 -*-
"""静态检查：不执行 Agent，只看 prompt / 工具定义 / 配置。"""
from . import rules       # noqa: F401  通用静态规则（16 条）
from . import case_rules  # noqa: F401  公开事故提炼的静态规则（4 条）
