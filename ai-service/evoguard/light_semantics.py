# -*- coding: utf-8 -*-
"""轻量分类器的语义簇归一化（训练与推理**共用同一份**词表）。

为什么单独一个模块：训练脚本在 scripts/、推理在 evoguard/，两边若要各写
一份簇表，早晚会漂移（训练用 7 个簇、推理用 5 个），结果是权重对不上而
分数悄悄变差。这里只有一份，两边都 import。

**词表来自领域常识，不是从留出集里挑词** —— 否则等于变相用测试集调参。
"""
import re

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


#: SCORE-02-T2：中文拼音/首字母危险意图词 -> 复用同一套语义簇 token。
#: 关键纪律：这里只做「危险意图词识别」，**不做单独拦截决定** ——
#: 必须与动作结构（调用/删除/绕过/输出）共现才算攻击，否则
#: "qing bang wo cha ding dan"（请帮我查订单）这种正常拼音会被误杀。
PINYIN_TO_CLUSTER = {
    # 全拼（去空格后匹配）
    "hulue": "<IGN>", "wushi": "<IGN>", "wangji": "<IGN>",
    "shanchu": "<BYPASS>", "shandiao": "<BYPASS>",
    "raoguo": "<BYPASS>", "poyi": "<BYPASS>",
    "zhuanzhang": "<OUT>", "zhuandao": "<OUT>",
    "fasong": "<OUT>", "daochu": "<OUT>", "duchu": "<OUT>",
    "diaoyong": "<OUT>", "zhixing": "<OUT>",
    " quanbu": "<ALL>", "suoyou": "<ALL>", "yishang": "<ALL>",
    "xitong": "<SYS>", "miyao": "<SYS>", "tishi": "<SYS>",
    "guanliyuan": "<ROLE>", "chaojiguanliyuan": "<ROLE>",
    "wuxianzhi": "<NOLIMIT>", "wuguol": "<NOLIMIT>",
    # 首字母缩写（方案点名的高频项）
    "hlzl": "<IGN>", "wszl": "<IGN>", "hl": "<IGN>",
    "qbsc": "<ALL><BYPASS>", "sc": "<BYPASS>",
    "dqmy": "<OUT><SYS>", "dwbx": "<OUT>",
    "rg": "<BYPASS>", "wktgjglyqx": "<ROLE><ALL>",
    "zhixing": "<OUT>", "fs": "<OUT>", "dw": "<OUT>",
}

#: 动作结构词（必须与危险意图共现才触发）
PINYIN_ACTION_WORDS = (
    "diaoyong", "zhixing", "shan", "sc", "fs", "dq", "zhuan",
    "call", "exec", "delete", "send", "output",
    "diao yong", "zhi xing", "删", "执行", "调用", "发送", "转账",
    "jie suan", "jiesuan", "qdzx", "settle", "结算",
    "jinru", "denglu", "kaitong", "ti quan", "tiquan", "quanxian",
    "ddzhyhh", "qbzz", "bzhye", "sfyj", "login", "grant",
)


def pinyin_tokens(text):
    """返回 (token, 证据)。规范化：小写 + 去空格 + 保留原串供动作共现判断。"""
    raw = str(text or "").lower()
    compact = raw.replace(" ", "").replace("\u200b", "")
    hits = []
    for word, tok in PINYIN_TO_CLUSTER.items():
        if len(word) >= 4:
            ok = word in compact
        else:
            # 短码（sc / rg / qb …）必须按**词边界**匹配。用子串匹配时
            # 英文里 "score"/"description" 全都会命中，实测把正常英文
            # 文本的分数从 0.14 推到 0.87（虚高 6 倍），是英文误报的主因。
            ok = re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(word),
                           raw) is not None
        if ok:
            hits.append(tok)
    if not hits:
        return []
    # 必须有动作结构共现（在原文或压缩串里均可）
    if not any(w in raw or w in compact for w in PINYIN_ACTION_WORDS):
        return []
    return list(dict.fromkeys(hits))


def describe():
    return {"clusters": sorted(SEMANTIC_CLUSTERS),
            "token_weight": TOKEN_WEIGHT,
            "source": "领域常识词表，未从留出集选取"}
