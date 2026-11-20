# -*- coding: utf-8 -*-
"""FINAL-02-T2：把结构化 Skill 从「提示词」升级为「可执行控制流钩子」。

真实 A/B 证据（experiments/skill_ab_real/report.md）：纯提示词 Skill 在
DeepSeek 上 ΔSuccess=-20pp（有害）。因此 Skill 不再塞进 system prompt，
而是挂载为运行时钩子，在工具调用前后改变控制流：

  - pre_write：写工具调用前，若 Skill 的 fix_pattern 指向精准编辑，
    返回 patch 建议（供 executor 优先采用），避免整文件覆写；
  - post_write：写成功后，按 verification_check 强制追加一次校验调用。

只有 error_signature / fix_pattern / verification_check 均非空且非 unknown
的 Skill 才挂载；空壳 Skill 不改变控制流（避免负增益）。
"""
import re

# 左词边界 + 允许下划线后缀（unknown_no_repair_trace 要能被识别为占位符），
# 右侧用 (?=$|[^a-z]) 排除 'denominator' 里的 'na'（后接 m 是字母，不匹配）。
PLACEHOLDER = re.compile(
    r"\b(?:unknown|none|n/a|na|待补)(?=$|[^a-z])", re.I)


def is_actionable(skill):
    """Skill 是否含真实可执行经验（不是空壳）。"""
    if not isinstance(skill, dict):
        return False
    for k in ("error_signature", "fix_pattern", "verification_check"):
        v = str(skill.get(k) or "").strip()
        if not v or PLACEHOLDER.search(v):
            return False
    return True


def signature_of(error_text):
    """从错误文本提取错误签名（如 ZeroDivisionError / KeyError）。"""
    m = re.search(r"\b([A-Za-z]+(?:Error|Exception))\b", str(error_text or ""))
    return m.group(1) if m else ""


def env_hint_for(skill, error_text):
    """命中 error_signature 时返回**单行**私有环境事实；不命中返回 None。

    EVO-REBUILD-01-T1：只注入 1 条 env_invariant，且在失败时才注入——
    绝不把整段规约塞进 system prompt（那样实测有害）。
    """
    if not isinstance(skill, dict):
        return None
    invs = skill.get("env_invariants") or []
    if not isinstance(invs, list) or not invs:
        return None
    sig = str(skill.get("error_signature") or "")
    got = signature_of(error_text)
    if sig and got and got.lower() not in sig.lower():
        return None                       # 签名不匹配：不注入（避免无关干扰）
    if sig and not got:
        return None
    for x in invs:
        if isinstance(x, dict) and str(x.get("rule") or "").strip():
            return str(x["rule"]).strip()[:200]
    return None


def hooks_from_skill(skill):
    """把结构化 Skill 转成控制流钩子 + 按需注入的 env_hint。"""
    if not is_actionable(skill):
        # 有 env_invariant 也可用（即使 fix_pattern 是 unknown）
        invs = (skill or {}).get("env_invariants") or []
        if not (isinstance(invs, list) and any(
                isinstance(x, dict) and str(x.get("rule") or "").strip()
                for x in invs)):
            return {"pre_write": None, "post_write": None,
                    "env_hint": None, "actionable": False}
        return {"pre_write": None, "post_write": None,
                "env_hint": (lambda err: env_hint_for(skill, err)),
                "actionable": True}

    check = str(skill.get("verification_check") or "").strip()
    fix = str(skill.get("fix_pattern") or "").strip()

    def pre_write(tool, args):
        # 只在写工具且 fix_pattern 暗示精准编辑时给建议，不强制改参数
        if tool == "fs.write" and re.search(r"patch|局部|精确|unique", fix, re.I):
            return {"prefer_tool": "fs.patch",
                    "reason": "skill.fix_pattern 建议精准编辑"}
        return None

    def post_write(tool, args, result):
        # 写成功后要求追加一次校验（由调用方决定是否真跑）
        if tool in ("fs.write", "fs.patch") and check:
            return {"verify": check[:200], "must_run": True}
        return None

    return {"pre_write": pre_write, "post_write": post_write,
            "env_hint": (lambda err: env_hint_for(skill, err)),
            "actionable": True}
