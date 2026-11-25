# -*- coding: utf-8 -*-
"""SARIF 2.1.0 导出（纯标准库）。

把 inspect 的发现转成 GitHub Code Scanning / 任意 CI 安全面板可直接吃的
SARIF 2.1.0。只做格式映射，不改判定、不增删发现。

严重度映射（SARIF level）：error -> error, warn -> warning, info -> note。
位置：优先 issue 里的 file/line；轨迹分析类发现没有源码行号时用 1，
并用 physicalLocation.artifactLocation.uri 明确来源，绝不伪造精确行号。
"""
import json

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
VERSION = "2.1.0"
LEVEL = {"error": "error", "warn": "warning", "warning": "warning",
         "info": "note", "note": "note"}


def _uri(issue):
    for k in ("file", "path", "source", "artifact_uri"):
        v = issue.get(k)
        if isinstance(v, str) and v.strip():
            return v.replace("\\", "/")
    return "evoguard://trace/%s" % str(issue.get("trace_id") or "unknown")


def _line(issue):
    for k in ("line", "lineno", "step_seq"):
        v = issue.get(k)
        if isinstance(v, int) and v > 0:
            return v
    return 1


def from_issues(issues, *, tool_name="evoguard-inspect", tool_version="1.0.0"):
    """issues -> 完整 SARIF 2.1.0 文档（dict）。"""
    rules, results = {}, []
    for i, issue in enumerate(issues or []):
        if not isinstance(issue, dict):
            continue
        rid = str(issue.get("rule_id") or issue.get("code")
                  or "EV0000")
        if rid not in rules:
            rules[rid] = {
                "id": rid,
                "name": rid.replace("-", "_"),
                "shortDescription": {
                    "text": str(issue.get("title") or rid)[:200]},
                "fullDescription": {
                    "text": str(issue.get("detail") or
                                issue.get("title") or rid)[:1000]},
                "helpUri": "https://github.com/workbuddy/EvoGuardLab",
            }
        if issue.get("false_positive"):
            rules[rid]["properties"] = {
                "falsePositive": str(issue["false_positive"])[:500]}
        sev = str(issue.get("severity") or "info").lower()
        msg = str(issue.get("detail") or issue.get("title") or rid)
        results.append({
            "ruleId": rid,
            "ruleIndex": list(rules).index(rid),
            "level": LEVEL.get(sev, "note"),
            "message": {"text": msg[:1000]},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": _uri(issue)},
                    "region": {"startLine": _line(issue)},
                }
            }],
            "properties": {
                "group": str(issue.get("group") or "security"),
                "agent": str(issue.get("agent") or ""),
                "step": str(issue.get("step_name") or ""),
                "fix": str(issue.get("fix") or "")[:500],
            },
        })
    return {
        "$schema": SCHEMA,
        "version": VERSION,
        "runs": [{
            "tool": {
                "driver": {
                    "name": tool_name,
                    "version": tool_version,
                    "informationUri": "https://github.com/workbuddy/EvoGuardLab",
                    "rules": list(rules.values()),
                }
            },
            "results": results,
        }],
    }


def dumps(issues, **kw):
    return json.dumps(from_issues(issues, **kw), ensure_ascii=False, indent=2)
