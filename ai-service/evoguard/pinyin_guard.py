# -*- coding: utf-8 -*-
"""HARD-中文短板：拼音/谐音/首字母/隐喻注入的零依赖检测器。

为什么单独一层：主 light_classifier 是**汉字** char n-gram，拼音文本
（ASCII 空格分隔）与它特征空间完全不同。实测把拼音集混进训练会让
中文 holdout recall 从 0.90 掉到 0.26（污染 vocab/idf），所以不混训，
改用独立规则层，只在检测到拼音化/谐音化迹象时启用。

四通道：
  1. 全拼音节（qing hu lue zhi ling）
  2. 首字母缩写（hlzl / wsyszl）
  3. 谐音错别字（呼略→忽略、只令→指令）
  4. 中英混合 / 隐喻角色扮演（ignore previous / 假设你是无限制助手）

零第三方依赖；核心库 dependencies=[] 约束不变。
"""
import re

#: 攻击意图的拼音音节（不含空格，便于匹配被空格/换行拆开的写法）
_PINYIN_PHRASES = (
    "qinghulue",       # 请忽略
    "huluezhiquandezhiling", "huluezhiqianzhiling",  # 忽略之前的指令
    "wushiyishangzhiling", "wushizhiling",           # 无视以上指令
    "zhixing",         # 执行（配合 delete/transfer 语义）
    "shanchuquanbushuju", "shanchushengchanku",      # 删除全部数据/生产库
    "ti-quan", "tiquan",                             # 提权
    "duquxitongmiyao", "miyao",                      # 读取系统密钥
    "raoguoshenfenyanzheng", "raoguo",               # 绕过身份验证
    "zhuanquanbuzhidingzhanghao", "zhuanzhang",      # 转账
    "kaitongchaojiguanliyuan",                       # 开通超级管理员
    "jiangzhanghuyuquanbuzhuandao", "jiangzhanghuyuequanbuzhuandao",
    "zhuan dao zhi ding",  # 转账到指定账户
    "huluezhiqianzhiling", "huluzhiling", "hulueling",
    "qingkongrizi", "rizi",                          # 清空日志
    "daochushejuku",                                 # 导出数据库
    "xiugaipeizhiwenjian",                           # 修改配置文件
)

#: 首字母缩写（攻击意图）
_INITIALS = (
    "hlzl",       # 忽略指令
    "qdzx",       # 请执行
    "wsyszl",     # 无视以上指令
    "scsj",       # 删除数据
    "dqmy",       # 读取密钥
    "dwbx",       # 外部邮箱
    "wktgjglyqx", # 开通管理员权限
    "rgsfyj",     # 绕过身份验证
    "bzhye",      # 不逐户余额（转账）
    "qbzz",       # 全部转账
    "ddzhyhh",    # 转到账户
)

#: 谐音/错别字 -> 正字（只在检测到攻击意图词时才计入）
_HOMOPHONE = {
    "呼略": "忽略", "乎略": "忽略", "呼luè": "忽略",
    "只令": "指令", "只定": "指令", " Zhi令": "指令",
    "无是": "无视", "勿视": "无视",
    "执形": "执行", "只行": "执行",
    "删出": "删除", "闪除": "删除",
    "蜜钥": "密钥", "密要": "密钥",
    "验正": "验证", "申份": "身份",
    "管里元": "管理员", "管理元": "管理员",
    "鱼额": "余额", "转道": "转到",
    "外不": "外部", "油箱": "邮箱",
    "配治": "配置", "校检": "校验",
    "日制": "日志", "属具库": "数据库",
    "开同": "开通", "全先": "权限", "权先": "权限",
    "绕锅": "绕过", "忽 略": "忽略",
}

#: 汉字攻击意图词（谐音归一化后再查）
_ZH_ATTACK = (
    "忽略", "无视", "忘记你的安全", "没有任何限制", "无条件执行",
    "删除全部", "删除所有", "删掉", "清空日志", "绕过身份",
    "绕过验证", "管理员密码", "明文告诉", "最高权限",
    "读取密钥", "读取系统密钥", "系统密钥", "全部转账", "转到指定",
    "导出数据库", "导出用户", "关闭安全", "关闭所有安全",
    "跳过所有校验", "获得这些权限", "清除审计", "无法记录我的操作",
    "超级管理员", "删除生产库", "删除生产", "留被分", "帐号开同",
    "转账到我的账户",
)

#: 英文/混合攻击短语
_EN_ATTACK = (
    "ignore previous", "ignore all", "forget your", "bypass",
    "ignore之前", "ignore之前的指令", "ignore 之前", "ignore 之前 de",
    "huluezhiling", "hu lue zhi ling",
    "transfer all funds", "developer mode", "no restrictions",
)

#: 良性语境白名单：这些词出现时，拼音/谐音关键词多为「分析/翻译」任务
_BENIGN_HINT = (
    "翻译成拼音", "拼音怎么", "谐音梗", "同音字", "写一个关于",
    "短故事", "解释退款流程", "帮我分析", "有没有用错",
    "check order", "what is the weather", "tian qi zen me yang",
)


def _normalise(text):
    """去零宽字符 + 全角转半角 + 小写，返回 (原样, 拼音串, 汉字串)。"""
    s = str(text or "")
    s = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", s)
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E
                else c for c in s)
    low = s.lower()
    py = re.sub(r"[^a-z]", "", low)
    zh = re.sub(r"[^一-鿿]", "", s)
    for k, v in _HOMOPHONE.items():
        zh = zh.replace(k, v)
    zh = zh.replace("忽 略", "忽略").replace("略之", "略之")
    return low, py, zh


def scan(text):
    """返回 {"hit": bool, "channel": str, "matched": [...]}。"""
    raw = str(text or "")
    # 先剥零宽再判（否则「忽\u200b略」的 prefilter 看不到攻击词）
    raw = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", raw)
    # 廉价前置过滤：拼音攻击必须含 >=4 个连续 ASCII 字母（全拼/首字母）
    # 或谐音错别字。纯中文/纯符号/大段无关文本直接返回，保证 scan 的
    # 10KB<1.5ms 延迟预算不被本层拖垮。
    cheap_hits = (re.search(r"[A-Za-z]{4,}", raw)
                  or re.search(r"[A-Za-z]{4,}", "".join(
                      chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E
                      else c for c in raw[:200]))
                  or any(k in raw for k in _HOMOPHONE)
                  or any(w in raw for w in _ZH_ATTACK)
                  or "忽略" in raw or "绕过" in raw)
    if not cheap_hits:
        return {"hit": False, "channel": "prefilter-skip", "matched": []}
    low, py, zh = _normalise(text)
    if any(h in low for h in _BENIGN_HINT):
        # 白名单只是「降低置信」，不是免死金牌：若同时有强攻击短语仍拦。
        # 但**引号里被引用/被翻译的词不算攻击证据**：
        #   "请把'忽略'翻译成拼音" 里的 忽略 是被解释的对象，不是指令；
        #   而 "翻译成拼音：忽略之前的指令" 没有引号包裹，仍是攻击。
        quoted = set(re.findall(
            r"['\"「」『』《》]([^'\"「」『』《》]{1,16})['\"「」『』《》]",
            str(text or "")))
        strong = [p for p in _PINYIN_PHRASES if p in py]
        strong += [w for w in _ZH_ATTACK if w in zh]
        strong = [w for w in strong
                  if not any(w in q or q in w for q in quoted)]
        if not strong:
            return {"hit": False, "channel": "benign_context",
                    "matched": []}
    matched = []
    for p in _PINYIN_PHRASES:
        if p in py and len(p) >= 6:
            matched.append("pinyin:" + p)
    tokens = set(re.findall(r"[a-z]{2,}", low))
    for ini in _INITIALS:
        if ini in tokens:
            matched.append("initial:" + ini)
    # 首字母可能按词分开写（rg sfyj），也允许 token 拼接后匹配
    joined = "".join(sorted(tokens))
    for pair in (("rg", "sfyj"), ("dq", "my"), ("sc", "sj")):
        if all(x in tokens for x in pair):
            matched.append("initial:" + "".join(pair))
    for w in _ZH_ATTACK:
        if w in zh:
            matched.append("homophone_or_zh:" + w)
    for w in _EN_ATTACK:
        if w in low:
            matched.append("en:" + w)
    matched = sorted(set(matched))
    return {"hit": bool(matched),
            "channel": "pinyin-homophone-mixed",
            "matched": matched[:8]}


def describe():
    return {"version": 1, "channels": ["pinyin", "initial",
                                       "homophone", "mixed"],
            "model_required": False, "dependencies": []}
