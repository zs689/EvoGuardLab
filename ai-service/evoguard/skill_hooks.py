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
import os
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


def _hooks_from_skill_base(skill):
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


# ---- MOD-EVO-01-T1：声明式写后断言执行器（零任意代码执行）-------------
# 只接受 conditional_skill 白名单里的 4 种原语；实现全用标准库 ast/re，
# 不 eval/exec 任何来自技能的字符串。
_ALLOWED_KINDS = frozenset({
    "ast_symbol_sync", "regex_must_match", "regex_must_not_match",
    "pair_presence",
})


def _symbol_values(source, symbol):
    """用 AST 取模块级赋值符号的值集合（只取字面量，不执行代码）。"""
    import ast
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            val = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            val = node.value
        else:
            continue
        named = any(isinstance(t, ast.Name) and t.id == symbol
                    for t in targets)
        if not named or val is None:
            continue
        # 支持 (a, b) / [a, b] / {a, b} 字面量与单标量
        if isinstance(val, (ast.Tuple, ast.List, ast.Set)):
            for e in val.elts:
                if isinstance(e, ast.Constant):
                    out.append(str(e.value))
        elif isinstance(val, ast.Dict):
            # DEEP-02：REQUIRED 这类符号是 dict（{'id': True}），
            # 早期实现只支持 list -> 永远返回空 -> 永远判不同步。
            # 只把值为 True 的键算作「已同步」，与 is_valid 的语义一致。
            for k, v in zip(val.keys, val.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    if v.value is True:
                        out.append(str(k.value))
        elif isinstance(val, ast.Constant):
            out.append(str(val.value))
    return out


def run_post_write_assertions(assertions, *, root=".", files=None):
    """写后立刻校验；返回 {ok, failures, checked}。

    `files` 可选：{rel: text} 直接给内容（测试用）；否则从 root 读。
    任一断言失败/文件不存在 → ok=False，**不静默放过**。
    """
    import re as _re
    failures = []
    checked = 0
    for i, a in enumerate(assertions or []):
        if not isinstance(a, dict):
            failures.append("断言 #%d 不是对象" % i)
            continue
        kind = str(a.get("kind") or "")
        if kind not in _ALLOWED_KINDS:
            failures.append("断言 #%d 未知原语 %r（拒绝执行）" % (i, kind[:40]))
            continue
        rel = str(a.get("file") or "")
        # 路径围栏：绝对路径/上跳一律拒（与 schema 校验双保险）
        if not rel or rel.startswith("/") or \
                ".." in rel.replace("\\", "/").split("/"):
            failures.append("断言 #%d 路径越界 %r" % (i, rel[:60]))
            continue
        try:
            if isinstance(files, dict) and rel in files:
                text = str(files[rel])
            else:
                full = os.path.join(root, rel)
                if not os.path.realpath(full).startswith(os.path.realpath(root)):
                    failures.append("断言 #%d 路径越界" % i)
                    continue
                with open(full, "r", encoding="utf-8") as f:
                    text = f.read()
        except Exception as exc:                            # noqa: BLE001
            failures.append("断言 #%d 读文件失败：%s" % (i, type(exc).__name__))
            continue
        checked += 1
        msg = str(a.get("message") or "%s 未通过" % kind)[:200]
        try:
            if kind == "ast_symbol_sync":
                src = _symbol_values(text, str(a.get("source_symbol") or ""))
                mir = _symbol_values(text, str(a.get("mirror_symbol") or ""))
                if not src:
                    failures.append("%s：找不到源符号 %s"
                                    % (msg, a.get("source_symbol")))
                elif src != mir:
                    failures.append("%s：%s=%s 与 %s=%s 不同步"
                                    % (msg, a.get("source_symbol"), src,
                                       a.get("mirror_symbol"), mir))
            elif kind == "regex_must_match":
                if not _re.search(str(a.get("pattern") or ""), text):
                    failures.append(msg)
            elif kind == "regex_must_not_match":
                if _re.search(str(a.get("pattern") or ""), text):
                    failures.append(msg)
            elif kind == "pair_presence":
                first, second = str(a.get("first") or ""), \
                    str(a.get("second") or "")
                if not (first and second and first in text and second in text):
                    failures.append(msg)
        except (_re.error, SyntaxError, ValueError) as exc:
            failures.append("断言 #%d 检查器执行失败：%s"
                            % (i, type(exc).__name__))
    return {"ok": not failures, "failures": failures, "checked": checked}


def proposed_text(tool, args, *, root="."):
    """取写工具**拟写入**的文件内容（只读，不落盘）。

    支持 fs.write(content=) 与 fs.patch(old_text/new_text)。取不到就返回
    None —— 调用方必须当作「无法预检」如实处理，不能静默放行。
    """
    if not isinstance(args, dict):
        return None
    rel = str(args.get("path") or args.get("file") or "")
    if not rel or rel.startswith("/") or \
            ".." in rel.replace("\\", "/").split("/"):
        return None
    full = os.path.join(root, rel)
    if not os.path.realpath(full).startswith(os.path.realpath(root)):
        return None
    tool = str(tool or "")
    try:
        if "patch" in tool:
            old = str(args.get("old_text") or "")
            new = str(args.get("new_text") or "")
            if not old or not os.path.isfile(full):
                return None
            with open(full, "r", encoding="utf-8") as f:
                cur = f.read()
            if old not in cur:
                return None
            return cur.replace(old, new, 1)
        if "write" in tool:
            if "content" in args:
                return str(args.get("content"))
            # 有些实现用 text / body
            for k in ("text", "body"):
                if k in args:
                    return str(args[k])
    except Exception:                                    # noqa: BLE001
        return None
    return None


def precise_fix(assertion, proposed, *, root="."):
    """给 ast_symbol_sync 失败生成可核对的最小补丁提示（行号+符号名）。"""
    import ast
    kind = str(assertion.get("kind") or "")
    if kind != "ast_symbol_sync":
        return ""
    src_sym = str(assertion.get("source_symbol") or "")
    mir_sym = str(assertion.get("mirror_symbol") or "")
    src = _symbol_values(proposed, src_sym)
    mir = _symbol_values(proposed, mir_sym)
    missing = [x for x in src if x not in mir]
    if not missing:
        return ""
    line = "?"
    try:
        tree = ast.parse(proposed)
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, ast.AnnAssign) else [])
            if any(isinstance(t, ast.Name) and t.id == mir_sym
                   for t in targets):
                line = node.lineno
                break
    except SyntaxError:
        pass
    vals = ", ".join(repr(x) for x in missing[:4])
    return ("镜像符号 %s 缺少 %s；位置 %s:%s（当前 %s=%s，源 %s=%s）"
            "最小补丁：把缺项补进 %s（list 用 %s.append(%s)；"
            "dict 用 %s.update(%s)，按实际类型择一）"
            % (mir_sym, vals, assertion.get("file") or "?", line,
               mir_sym, mir, src_sym, src, mir_sym, mir_sym, vals,
               mir_sym, "{%s: True}" % vals))


def run_pre_commit_check(assertions, *, root=".", tool=None, args=None):
    """写前门禁：用**拟写入内容**跑断言，失败返回 blocked，绝不落盘。"""
    proposed = proposed_text(tool, args, root=root)
    if proposed is None:
        return {"blocked": False, "checked": 0,
                "detail": "无法预检拟写内容（不做假放行判断）",
                "proposed_unavailable": True}
    # 复用同一套只读检查器，但喂拟写内容而不是磁盘内容
    virtual = {}
    for a in (assertions or []):
        if isinstance(a, dict) and a.get("file"):
            virtual[str(a["file"])] = proposed
    verdict = run_post_write_assertions(assertions, root=root,
                                        files=virtual)
    if verdict["ok"]:
        return {"blocked": False, "checked": verdict["checked"],
                "detail": "写前断言通过"}
    fixes = []
    for a in (assertions or []):
        if isinstance(a, dict) and a.get("kind") == "ast_symbol_sync":
            f = precise_fix(a, proposed, root=root)
            if f:
                fixes.append(f)
    return {"blocked": True, "checked": verdict["checked"],
            "failures": verdict["failures"],
            "fixes": fixes,
            "detail": " | ".join(verdict["failures"][:3] + fixes[:2])}


def assertions_from_trace(trace, *, source_symbol=None, mirror_symbol=None,
                          file=None):
    """MOD-EVO-01-T2：从失败→成功成对轨迹提炼**声明式写后断言**。

    只从轨迹里真实出现的符号对提炼，不针对某一条样本硬编码答案。
    提炼出的断言必须经过 conditional_skill 的白名单校验才能用。
    """
    import json as _json
    blob = _json.dumps(trace or [], ensure_ascii=False)
    if not (source_symbol and mirror_symbol):
        # 从 stderr/输出里找「A ... B 不同步」这类成对符号：取大写下划线标识符
        pairs = re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b.{0,80}?"
                           r"\b([A-Z][A-Z0-9_]{2,})\b", blob)
        for a, b in pairs:
            if a != b and {"FIELDS", "REQUIRED", "ALLOWED", "RULES"} & {a, b}:
                source_symbol, mirror_symbol = a, b
                break
    if not (source_symbol and mirror_symbol):
        return []
    return [{
        "kind": "ast_symbol_sync",
        "file": file or "solution.py",
        "source_symbol": source_symbol,
        "mirror_symbol": mirror_symbol,
        "message": "写后断言：%s 改了但 %s 没同步"
                   % (source_symbol, mirror_symbol),
    }]


def hooks_from_skill(skill, *, root=None):
    """在写后钩子里自动跑声明式断言（若有）。"""
    hooks = _hooks_from_skill_base(skill)
    assertions = (skill or {}).get("post_write_assertions") or []
    if not assertions:
        return hooks
    base_post = hooks["post_write"]

    def post_write(tool, args, result):
        out = base_post(tool, args, result) if base_post else None
        if tool in ("fs.write", "fs.patch", "code.write"):
            verdict = run_post_write_assertions(assertions, root=root or ".")
            if not verdict["ok"]:
                return {"assertion_failed": True,
                        "failures": verdict["failures"],
                        "verify": (out or {}).get("verify"),
                        "must_fix_before_commit": True}
        return out

    hooks["post_write"] = post_write
    hooks["assertions"] = assertions

    # DEEP-02-T1：写前拦截（Pre-Commit Gate）。先于落盘用拟写内容跑同一套
    # 断言；不一致就拒绝写入，工作区不脏，返回精确缺失符号+行号+最小补丁。
    def pre_commit(tool, args):
        if tool not in ("fs.write", "fs.patch", "code.write"):
            return None
        return run_pre_commit_check(assertions, root=root or ".",
                                    tool=tool, args=args)

    hooks["pre_commit"] = pre_commit
    return hooks
