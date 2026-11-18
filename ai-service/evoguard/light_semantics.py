# -*- coding: utf-8 -*-
"""轻量分类器的语义簇归一化（训练与推理**共用同一份**词表）。

为什么单独一个模块：训练脚本在 scripts/、推理在 evoguard/，两边若要各写
一份簇表，早晚会漂移（训练用 7 个簇、推理用 5 个），结果是权重对不上而
分数悄悄变差。这里只有一份，两边都 import。

**词表来自领域常识，不是从留出集里挑词** —— 否则等于变相用测试集调参。
"""

SEMANTIC_CLUSTERS = {
    "<IGN>": ("忽略", "无视", "忘记", "别管", "不要理会", "disregard",
              "ignore", "forget", "override"),
    "<BYPASS>": ("绕过", "破解", "逆向", "规避", "突破", "解除", "跳过",
                 "bypass", "circumvent", "jailbreak", "unlock"),
    "<ALL>": ("所有", "全部", "一切", "任何", "之前的", "以上", "上面",
              "previous", "all", "everything"),
    "<SYS>": ("系统提示", "系统指令", "规则", "限制", "政策", "护栏", "审查",
              "system prompt", "rules", "restrictions", "policy", "guard"),
    "<ROLE>": ("你现在是", "扮演", "从现在起", "act as",
               "you are now", "from now on", "pretend"),
    "<NOLIMIT>": ("无限制", "无过滤", "不受限", "不走审核", "未审查",
                  "unfiltered", "unrestricted", "no limits", "without rules"),
    "<OUT>": ("输出", "给出", "提供", "告诉我", "教我", "代码", "脚本",
              "output", "provide", "give me", "code", "script"),
}

#: 命中时给的权重倍数（与训练侧 feats() 保持一致）
TOKEN_WEIGHT = 2


def tokens(text):
    """返回命中的语义簇 token 列表（可多命中）。"""
    t = str(text or "").lower()
    hit = []
    for tok, words in SEMANTIC_CLUSTERS.items():
        for w in words:
            if w in t:
                hit.append(tok)
                break
    return hit


def describe():
    return {"clusters": sorted(SEMANTIC_CLUSTERS),
            "token_weight": TOKEN_WEIGHT,
            "source": "领域常识词表，未从留出集选取"}
