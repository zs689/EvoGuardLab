# -*- coding: utf-8 -*-
"""DEEP-05：安全规则表与预编译逻辑（从 security.py 机械拆出）。"""

# DEEP-05 通用兜底 imports（机械拆分后原模块级依赖统一补齐）
import hashlib
import json
import os
import re
import time
import traceback
import uuid
import concurrent.futures as _cf

import re


# ---------------- 特征表 ----------------
# 每条都写清「为什么加」和「什么时候会误报」，报告里要能自证不是拍脑袋。
DETECTORS = []


def _det(code, severity, pattern, detail, false_positive, flags=0, pre=()):
    """pre：命中前先做的廉价子串检查（任一出现才跑正则）。

    24 条正则全量扫 10KB 要 3ms 上下，对「不想加延迟」是硬伤。
    真实文本里绝大多数连触发字符都没有，先做 O(n) 的 in 判断能砍掉九成开销。
    """
    DETECTORS.append({"code": code, "severity": severity,
                      "re": re.compile(pattern, flags | re.IGNORECASE),
                      "detail": detail, "false_positive": false_positive,
                      "pre": tuple(x.lower() for x in pre)})


# --- 注入：试图让模型改规则、改身份、泄露提示词 ---
_det("injection_override", "error",
     # 间接注入常见「忽略用户目标/当前任务」—— 原来只认「以上/之前」
     # 的指令覆盖，漏掉这一类（MCP 返回侧实测）。
     r"(?:忽略|无视|忘记)(?:以上|之前|前面|上面|上述)(?:的)?(?:所有)?(?:指令|提示|规则|要求)"
     r"|(?:忽略|无视|放弃)(?:用户|当前|原本)(?:的)?(?:目标|任务|要求)"
     r"|(?:disregard|ignore|forget)\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+"
     r"(?:instructions?|prompts?|rules?)",
     "要求忽略既有指令，典型的指令覆盖攻击",
     "用户在描述「忽略之前那封邮件」这类业务语境时也会出现，需人工看一眼上下文")
_det("injection_supersede", "warn",
     # 多轮上下文漂移：先宣布上一条作废，再塞新目标。
     # 用 warn 而不是 error —— 正常业务里「上一条需求作废，改为……」是合法的，
     # 这里只提示人工复核，不硬拦。
     r"(?:上一条|刚才那条|之前的指令|前面的要求)[^。！？\n]{0,12}"
     r"(?:作废|无效|取消|不算|不适用)"
     r"|(?:the\s+)?(?:previous|above|prior)\s+(?:instruction|request|task)s?"
     r"\s+(?:is|are)\s+(?:void|cancell?ed|revoked)",
     "声明上一条指令作废并改写目标，多轮上下文漂移的典型写法",
     "正常业务里「上一条需求作废」也会命中，属可接受的可复核提示")
_det("injection_role_switch", "error",
     # 「You are now the admin」和「unrestricted」是同一类：都试图提升权限。
     # 只认 unrestricted/evil 会漏掉最常见的管理员提权写法。
     r"(?:你现在是|从现在起你是|扮演一个没有限制的|"
     r"you\s+are\s+now\s+(?:a|an|the)\s+(?:unrestricted|unfiltered|evil|dan|"
     r"admin|administrator|root|superuser|owner))",
     "试图改写模型身份或提升权限",
     "正常业务里出现「你现在是审核员」这类说法会误报")
_det("injection_double_negation_grant", "error",
     # 「不要忘记把权限授予 X」表面是否定，实际是要求授权。
     # 只匹配否定词、不匹配后面的授权动作，就会把它当成安全的劝阻。
     r"(?:不要|请勿|切勿|别)(?:忘记|忘了|忘掉)[^。！？\n]{0,24}"
     r"(?:授予|授权|开通|提权|批准|解除限制)"
     r"|(?:don'?t|do\s+not|never)\s+forget\s+to\s+(?:grant|authori[sz]e|"
     r"send|transfer|escalate|approve)",
     "双重否定包装的授权请求（「不要忘记授权」实为要求授权）",
     "「不要忘记把报告发给同事」这类正常提醒会命中，需看宾语")
_det("injection_prompt_leak", "error",
     r"(?:输出|打印|告诉我|展示|复述|reveal|print|show|repeat)\s*(?:你的)?\s*"
     r"(?:系统)?(?:提示词|系统提示|prompt|system\s+message|instructions)",
     "试图套取系统提示词",
     "用户问「你的提示词大概写了什么」时也会命中，属可接受误报")
_det("injection_delimiter", "warn",
     r"<\|(?:im_start|im_end|system|endoftext|assistant)\|>|\[/?INST\]|<<SYS>>",
     "出现对话模板的保留标记，试图伪造角色边界",
     "用户粘贴原始 prompt 做讨论时会命中")
_det("injection_tool_directive", "error",
     # 中文间接注入常见三种写法：执行以下X / 执行X / 调用X。
     # 只写「执行以下」会漏掉「执行脚本:」「运行命令：」这类更常见的祈使句。
     r"(?:执行|运行|调用).{0,8}(?:命令|脚本|工具)\s*[:：]",
     "在内容里夹带「执行以下命令」，常见于间接注入",
     "运维类任务里正常出现，需要结合来源判断")

# --- 外泄：把数据带出去 ---
_det("exfil_url_with_secret", "error",
     r"https?://[^\s\"'<>]{0,200}?(?:[?&](?:key|token|secret|password|passwd|api_key)=)",
     "URL 里携带凭据参数，典型外带通道",
     "调试文档里的示例 URL 会命中")
_det("exfil_upload_to_url", "error",
     r"(?:上传|发送|转发|外发|导出)[^。！？\n]{0,16}https?://"
     r"|(?:upload|send|forward|export)[^\n.]{0,20}https?://",
     "把数据发往外部地址，常见于数据外带",
     "正常的文件托管/回调配置会命中，需确认目标域名是否在允许清单")
_det("exfil_data_uri", "warn",
     r"!\[[^\]]*\]\(\s*data:[^)]{40,}\)",
     "Markdown 图片里塞 data URI，可用来把内容编码进外链",
     "富文本编辑器粘贴的图片会命中")
_det("exfil_encoded_blob", "warn",
     r"[?&](?:d|data|payload|q)=[A-Za-z0-9+/=_-]{60,}",
     "URL 参数里带长编码串，疑似打包外传",
     "分页/分享链接的长参数会命中")

# --- 凭据与隐私 ---
_det("secret_aws_key", "error", r"\bAKIA[0-9A-Z]{16}\b",
     "AWS Access Key", "文档里讲 AKIA 前缀时可能误报")
_det("secret_openai_key", "error", r"\bsk-[A-Za-z0-9_-]{20,}\b",
     "OpenAI 风格密钥", "短横线连写的普通编号可能误报")
_det("secret_github_token", "error", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
     "GitHub token", "几乎不会误报")
_det("secret_private_key", "error", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
     "私钥块", "几乎不会误报")
_det("secret_generic_kv", "warn",
     r"(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}",
     "键值形式的凭据", "配置文件样例、占位符会误报")
_det("pii_id_card", "error", r"(?<!\d)\d{17}[\dXx](?!\d)",
     "18 位身份证号", "订单号/流水号恰好 18 位会误报")
_det("pii_phone", "warn", r"(?<!\d)1[3-9]\d{9}(?!\d)",
     "手机号", "长数字编号的后 11 位可能命中")
_det("pii_email", "info", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
     "邮箱地址", "公开联系邮箱也会命中")

# --- 命令执行 ---
# 结尾用后瞻而不是「空白或行尾」：工具参数常是 JSON，
# `{"cmd": "rm -rf /"}` 里 / 后面跟的是引号，用 (?=\s|$) 会漏掉（实测踩到）。
_det("dangerous_shell_rm", "error", r"\brm\s+-[rf]{1,2}\s+/(?=[\s\"',}\]]|$|\*)",
     "删除根目录的破坏性命令", "讲安全的文章里会出现")
_det("dangerous_shell_pipe", "error",
     r"\b(?:curl|wget)\b[^\n|]{0,120}\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b",
     "下载后直接管道执行，典型投毒手法", "安装脚本示例会命中")
_det("dangerous_shell_eval", "warn",
     r"\b(?:Invoke-Expression|iex)\s*\(|\beval\s*\(\s*(?:input|request|sys\.argv)",
     "把外部输入直接当代码执行", "脚手架代码可能命中")
_det("dangerous_chmod", "warn", r"\bchmod\s+777\b",
     "把权限开到 777", "容器镜像脚本里常见")

# --- 输出侧：浏览器里能执行的东西 ---
_det("xss_script_tag", "error", r"<\s*script\b",
     "输出里带 script 标签", "代码示例/文档会命中，取决于输出上下文")
_det("xss_event_handler", "error", r"\son(?:error|load|click|mouseover|focus)\s*=",
     "内联事件处理器，典型 XSS 载荷", "前端代码片段会命中")
_det("xss_javascript_uri", "error", r"javascript\s*:",
     "javascript: 伪协议", "文档里讲 XSS 时会命中")
_det("xss_iframe", "warn", r"<\s*iframe\b", "内嵌框架，可被用来钓鱼",
     "嵌入视频时正常使用")


# 前置子串表：只有出现这些片段才值得跑对应正则。
# 单独一张表而不是塞进 _det 调用，是为了让「降噪条件」一眼可审 ——
# 条件写错了会漏检，这比误报更危险，所以必须集中可见。
_PRE = {
    "injection_override": ("忽略", "无视", "忘记", "放弃", "ignore", "disregard", "forget"),
    "injection_role_switch": ("你现在", "从现在起", "扮演", "you are now", "from now on"),
    "injection_prompt_leak": ("提示词", "系统提示", "prompt", "instructions",
                              "system message"),
    "injection_delimiter": ("<|", "[inst]", "<<sys>>", "<</sys>>"),
    # 正则首组是 (?:执行以下|运行以下|调用)，pre 必须包含「调用」本身，
    # 只写「调用以下」会让 `调用工具：` 这类载荷漏检（红队 PoC 已复现）。
    "injection_tool_directive": ("执行", "运行", "调用"),
    "exfil_url_with_secret": ("://",),
    "exfil_data_uri": ("data:",),
    # 正则首字符类为 [?&]，pre 只写 "?" 会漏掉 `&data=...` 形态。
    "exfil_encoded_blob": ("?", "&"),
    "secret_aws_key": ("akia",),
    "secret_openai_key": ("sk-",),
    "secret_github_token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    "secret_private_key": ("-----begin",),
    "secret_generic_kv": ("key", "secret", "password", "passwd", "token"),
    "pii_id_card": ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"),
    "pii_phone": ("13", "14", "15", "16", "17", "18", "19"),
    "pii_email": ("@",),
    "dangerous_shell_rm": ("rm",),
    # INT-03：新规则必须带前置词，否则每条文本都要全量跑正则
    # （实测 10KB 从 1.0ms 涨到 1.77ms，超了 1.5ms 预算）。
    "injection_supersede": ("作废", "无效", "取消", "不算", "void", "revoked",
                            "cancelled", "canceled"),
    "injection_double_negation_grant": ("忘", "forget"),
    # 上传外发规则必然含 URL，用 http 作前置最省
    "exfil_upload_to_url": ("http",),
    "dangerous_shell_pipe": ("curl", "wget"),
    "dangerous_shell_eval": ("invoke-expression", "iex", "eval("),
    "dangerous_chmod": ("chmod",),
    "xss_script_tag": ("<",),
    "xss_event_handler": ("onerror", "onload", "onclick", "onmouseover", "onfocus"),
    "xss_javascript_uri": ("javascript",),
    "xss_iframe": ("<",),
}
for _d in DETECTORS:
    _d["pre"] = tuple(x.lower() for x in _PRE.get(_d["code"], ()))


# ---------------- 中文越狱手法（HARD-中文深度优化） ----------------
# 背景：中文 direct_jailbreak 逐 case 召回只有 0.059（34 条里中 2 条），
# 因为原有规则几乎全是英文模式。下面按**手法类别**（不是照具体样本）
# 补中文规则，并在 151+31 条真实中文良性上实测误报为 0。
# 分级原则：高置信（系统覆盖/授权伪造）给 error 真拦；中置信（CTF/
# 角色/多轮/编码/绕过）给 warn 只记录——正常技术讨论也可能出现这些词，
# 直接 block 会误伤用户，如实标注为「检出率高于硬拦率」。
_det("zh_jailbreak_system_override", "error",
     r"(?:系统|管理员)[^。\n]{0,10}通知|系统指令|system\s*prompt|"
     r"RAW\s*MODE|未过滤模式|不受任何限制|优先级高于用户",
     "中文系统提示覆盖/越权伪装（假扮系统或管理员下发指令）",
     "正常客服话术里极少出现；若业务确实会转发『系统通知』原文会误判，"
     "应配合来源可信度判断",
     pre=("系统", "管理员", "RAW", "未过滤", "优先级高于用户"))

_det("zh_jailbreak_authorized_bypass", "error",
     r"合法的?授权|书面授权|已授权|授权编号",
     "用『合法授权/授权编号』给越权请求背书（常见于中文越狱包装）",
     "真实的合规审批邮件也会出现『授权编号』，需结合是否要求突破安全边界",
     pre=("授权",))

_det("zh_jailbreak_ctf_sandbox", "warn",
     r"(?:CTF|ctf)比赛|靶机|competition\s*range|lab\s*instance|沙箱环境",
     "以 CTF/靶机/沙箱名义索要攻击实现（中文越狱高频包装）",
     "正常的安全学习/竞赛讨论也会出现，所以只 warn 不拦",
     pre=("CTF", "ctf", "靶机", "沙箱", "range", "lab"))

_det("zh_jailbreak_role_grant", "warn",
     r"你(?:是一名|是资深|现在是|现在的身份是)|假设你是|扮演",
     "角色扮演式授权（要求模型扮演无限制角色）",
     "写作/客服场景正常使用『假设你是』，只 warn",
     pre=("你是", "假设你", "扮演", "身份"))

_det("zh_jailbreak_multiturn_evade", "warn",
     r"(?:轮\s*\d|第[一二三四五]轮)|从中断|接着(?:做|继续)|不要重新评估",
     "多轮接续规避（要求把被拒内容从中断处继续）",
     "任务分解本身的正常用法，只 warn",
     pre=("轮", "中断", "接着", "重新评估"))

_det("zh_jailbreak_encoding_evade", "warn",
     r"Base64|unicode\s*转义|拼音替代|拆字",
     "编码/拆字规避检测（要求用 Base64 或拆字绕过过滤）",
     "正常编码教学也会提到 Base64，只 warn",
     pre=("Base64", "unicode", "拼音", "拆字"))
