# -*- coding: utf-8 -*-
"""规则注册表：让规则库可列举、可过滤、可扩展，而不是一堆散函数。

每个规则必须声明：分组、严重度、查什么、**什么时候会误报**。
「误报说明」不是文档装饰 —— 报告会把它印在每条问题下面，
用户看到警告时能立刻判断要不要理它。没有这条的规则不允许入库。
"""

# 分组：决定报告里的结构，也决定用户按什么思路排查
GROUPS = {
    "answer": "输出层：答案本身不对",
    "tool": "工具层：调用或结果的使用有问题",
    "flow": "流程层：步骤顺序 / 循环 / 过早收尾",
    "resource": "资源层：token 与耗时",
    "structure": "结构层：多 Agent / 并行 / 错误传播",
}

RULES = {}       # id -> meta（含 fn）
ALL_RULES = []   # 只放函数，保持旧接口


def rule(rule_id, group, severity, title, why, false_positive):
    """注册一条规则。false_positive 必填，不允许写「无」。"""
    if group not in GROUPS:
        raise ValueError("未知分组：%s" % group)
    if not false_positive:
        raise ValueError("规则 %s 缺少误报说明 —— 没有它用户会把警告当铁证" % rule_id)

    def deco(fn):
        meta = {"id": rule_id, "group": group, "severity": severity,
                "title": title, "why": why, "false_positive": false_positive,
                "fn": fn}
        RULES[rule_id] = meta
        ALL_RULES.append(fn)
        return fn
    return deco


def meta_of(rule_id):
    return RULES.get(rule_id, {})


def select(groups=None, only=None, skip=None):
    """按分组 / 白名单 / 黑名单挑规则，给 CLI 的 --group/--only/--skip 用。"""
    out = []
    for rid, m in RULES.items():
        if groups and m["group"] not in groups:
            continue
        if only and rid not in only:
            continue
        if skip and rid in skip:
            continue
        out.append(m["fn"])
    return out
