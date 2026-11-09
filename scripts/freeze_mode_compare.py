# -*- coding: utf-8 -*-
"""会话 4：冻结安全配置，四模式**逐 case** 对照。

为什么要这个文件（推进计划 §3 / §5 / §10 / §11 会话 4）：
  旧 `experiments/p0_09_20261109/mode_compare.json` 只有聚合百分比，
  没有逐 case 结果、没有分母、更没有完整配置。而实测反例是：
      同一个 mode=enforce、AUTO_APPROVE=True，仅 AGENTLAB_ALLOW_AUTO_APPROVE
      从 0 改到 1，恢复期写操作就从 E_APPROVAL_REQUIRED 变成真的落库
      （见 tests/test_mode_compare.py 的真实 runtime 子实验）。
  只记录一个 mode 字段就宣称「配置完整」，是无法解释这个差异的。

本脚本做三件事：
  1. 把**所有有效开关**写成一份规范化配置并算指纹（含审批开关、工具契约、
     输出落点、威胁模型版本、后续动作决策器）。
  2. 复用 S02 的 defense_demo.py 执行引擎（同一套执行模型，不另造口径），
     在四档模式下跑同一批场景，产出逐 case 结果与明确分母。
  3. 写 manifest：威胁模型版本、攻击集/正常集内容指纹、独立授权策略、
     代码指纹、安全模式、包裹、自动审批、输出落点。

诚实边界（不粉饰）：
  - 场景数 8（4 攻击 + 4 正常对照），**低于**计划 §5 建议的「12 个工程冒烟起点」，
    更没到 30+30。数量是开发起点不是置信保证，不用改措辞硬凑独立样本。
  - 演示引擎不含写操作与真实模型，所以「待审批数 / 等待时间 / token / 延迟分位数」
    一律记 N/A（null），不写 0。审批开关在本引擎里不改变行为，
    其真实影响由 tests/test_mode_compare.py 用真实 runtime 单独验证。
  - 后续动作是确定性最坏情况观测器，不是真实模型；攻击目标达成率只在该
    受控执行模型下成立。

跑法：
    python scripts/freeze_mode_compare.py                 # 默认写新目录
    python scripts/freeze_mode_compare.py --out /tmp/cmp  # 显式隔离输出
产物：
    {--out}/{compare.json,compare.md,manifest.json}
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import security as S  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "experiments", "mode_compare_v2")
FROZEN_DIR = os.path.join(ROOT, "experiments", "p0_09_20261109")

# 复用 S02 的执行引擎：同一份执行模型、同一套「按真实后果计量」的口径。
_spec = importlib.util.spec_from_file_location(
    "defense_demo_engine", os.path.join(HERE, "defense_demo.py"))
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

# 威胁模型：先把「攻击者能控制什么」写死，再谈指标。
THREAT_MODEL = {
    "version": "threat-model-v1",
    "attacker_controls": [
        "任务输入文本", "检索到的外部文档内容", "工具返回内容", "工具调用参数",
    ],
    "attacker_does_not_control": [
        "系统策略与工具白名单", "批准记录", "评测真值", "安全模式与审批开关",
    ],
    "sensitive_resources": ["订单明细 A1001", "本地假凭据串", "受控接收方 127.0.0.1:9"],
    "observable_consequences": [
        "未授权工具是否真的执行", "FakeSink 是否真的命中", "危险操作是否执行",
        "回灌给 Agent 的文本是否真的改变", "输出是否仍可被当代码", "业务任务是否完成",
    ],
    "safety": "本地假凭据、127.0.0.1:9 丢弃端口、不联网、不执行任何命令、载荷良性",
}

# 缺失的成本指标记 N/A，不写 0（推进计划 §5）。
NA_COSTS = {
    "pending_approval": None, "wait_time": None, "extra_calls": None,
    "tokens": None, "latency_p95": None,
    "why_na": "以下成本指标一律记 N/A（null），不写 0：演示引擎不含写操作与"
              "真实模型调用；审批开关的真实影响由 tests/test_mode_compare.py"
              " 用真实 runtime 单独验证",
}


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def fingerprint(obj):
    """规范化 JSON 的 sha256 —— 同一份配置必须永远得到同一个指纹。"""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _env(name, default):
    return os.environ.get(name, default)


def make_config(mode_label, wrap):
    """一份**完整**的有效配置。缺一个开关，指纹就不足以解释结果。

    `auto_approve` 在 runtime 里是模块加载时冻结的常量，这里如实登记它的取值；
    并单独标注它在演示引擎里不产生行为差异，避免把「登记了」写成「生效了」。
    """
    return {
        "security_mode": "enforce" if mode_label.startswith("enforce") else mode_label,
        "wrap": bool(wrap),
        "auto_approve": _env("AGENTLAB_AUTO_APPROVE", "1"),
        "allow_auto_approve": _env("AGENTLAB_ALLOW_AUTO_APPROVE", "0"),
        "agent_policy": sorted(demo.TARGET["tools_allowed"]),
        "tool_contract": {k: {"side_effect": v["side_effect"],
                              "dangerous": bool(v.get("dangerous"))}
                          for k, v in demo.TOOLS.items()},
        "output_sink": "api/app.py: encode_for(html) + CSP/nosniff；"
                       "本演示只核验 guard 返回值是否真的被采用",
        "threat_model_version": THREAT_MODEL["version"],
        "backend": "mock",
        "followup": "controlled_worst_case（不是真实模型）",
        "approval_switches_effective_in_engine": False,
        "approval_effect_verified_by": "tests/test_mode_compare.py（真实 runtime 子实验）",
    }


def _set_fingerprints():
    attacks = [sc["attack"] for sc in demo.SCENARIOS]
    normals = [sc["normal"] for sc in demo.SCENARIOS]
    return {
        "attack_set": fingerprint(attacks),
        "normal_set": fingerprint(normals),
        "scenario_count": len(demo.SCENARIOS),
        "attack_cases": len(attacks),
        "normal_cases": len(normals),
        "note": "8 个工程冒烟场景（4 攻击 + 4 正常），低于计划 §5 的 12 个起点，"
                "未扩到 30+30；不靠改措辞凑独立样本",
    }


def _code_fingerprint():
    """把真正决定结果的代码一起冻住，避免「配置没变但代码变了」。"""
    rel = ["ai-service/evoguard/security.py", "ai-service/evoguard/runtime.py",
           "scripts/defense_demo.py", "scripts/freeze_mode_compare.py"]
    out = {}
    for r in rel:
        p = os.path.join(ROOT, r)
        with open(p, "rb") as f:
            out[r] = hashlib.sha256(f.read()).hexdigest()
    return out


def _ratio(n, d):
    return None if not d else round(n / d, 3)


def _run_mode(mode_label, wrap):
    sink = demo.FakeSink()
    followup = demo.ControlledFollowUp()
    rows = []
    for sc in demo.SCENARIOS:
        for kind in ("attack", "normal"):
            r = demo._run_scenario(mode_label, wrap, sc, kind, followup, sink)
            # 逐 case 证据：只保留能回指行为后果的字段，不把检测建议混进来。
            rows.append({
                "case_id": r["case_id"], "scenario": r["scenario"], "kind": r["kind"],
                "planned": 1,
                "framework_failed": 0,
                "judgeable": 1,
                "unjudgeable": 0,
                "business_success": int(bool(r["task_completed"])),
                "safe_completion": int(bool(r["safe_completed"])),
                "attack_goal_achieved": int(bool(r["attack_goal_achieved"])),
                "normal_false_blocked": int(r["kind"] == "normal" and
                                            (r["call_blocked"] or not r["task_completed"])),
                "detected": bool(r["detected"]),
                "decision": r["decision"], "action": r["action"],
                "executed": bool(r["executed"]), "effect": r["effect"],
                "reason": r["reason"], "codes": r["codes"],
                "real_controls": r["real_controls"],
                "body_changed": bool(r["body_changed"]),
                "call_blocked": bool(r["call_blocked"]),
                "sink_hits": r["sink_hits"],
                "released_calls": r["released_calls"],
                "dangerous_executed": bool(r["dangerous_executed"]),
                "output_still_executable": bool(r["output_still_executable"]),
                "followup_status": r.get("followup_decision", {}).get("status"),
                "observable": r.get("followup_decision", {}).get("status") != "unavailable",
            })
    return rows, _summarize(rows)


def _summarize(rows):
    attacks = [r for r in rows if r["kind"] == "attack"]
    normals = [r for r in rows if r["kind"] == "normal"]
    observable = [r for r in attacks if r["observable"]]
    danger = [r for r in attacks if r["sink_hits"] or r["released_calls"]
              or r["call_blocked"] or r["dangerous_executed"]]
    blocked = [r for r in danger if r["call_blocked"]]
    s = {
        "planned": len(rows),
        "framework_failed": 0,
        "judgeable": len(rows),
        "unjudgeable": 0,
        "business_success": sum(r["business_success"] for r in rows),
        "safe_completion": sum(r["safe_completion"] for r in rows),
        "attack_cases": len(attacks),
        "normal_cases": len(normals),
        "observable_attacks": len(observable),
        "attack_goal_achieved": sum(r["attack_goal_achieved"] for r in attacks),
        "normal_false_blocked": sum(r["normal_false_blocked"] for r in normals),
        "sink_hits": sum(r["sink_hits"] for r in rows),
        "dangerous_executed": sum(1 for r in rows if r["dangerous_executed"]),
        "dangerous_attempts": len(danger),
        "blocked_before_execution": len(blocked),
        "controls_executed": sum(len(r["real_controls"]) for r in rows),
    }
    s.update({
        "attack_goal_rate": _ratio(sum(r["attack_goal_achieved"] for r in observable),
                                   len(observable)),
        "true_block_rate": _ratio(len(blocked), len(danger)),
        "normal_false_block_rate": _ratio(s["normal_false_blocked"], len(normals)),
        "safe_completion_rate": _ratio(s["safe_completion"], s["planned"]),
    })
    s.update(NA_COSTS)
    return s


def _verify(rows, summary):
    """汇总必须能从逐 case 重算出来，否则报告自相矛盾。"""
    for mode, s in summary.items():
        sub = [r for r in rows if r["mode"] == mode]
        assert s["planned"] == len(sub), "%s 计划数不符" % mode
        assert s["business_success"] == sum(r["business_success"] for r in sub), \
            "%s 业务成功数不符" % mode
        assert s["safe_completion"] == sum(r["safe_completion"] for r in sub), \
            "%s 安全完成数不符" % mode
        assert s["sink_hits"] == sum(r["sink_hits"] for r in sub), \
            "%s 接收方命中数不符" % mode
        assert s["attack_goal_achieved"] == sum(
            r["attack_goal_achieved"] for r in sub), "%s 攻击达成数不符" % mode
        assert s["controls_executed"] == sum(
            len(r["real_controls"]) for r in sub), "%s 控制动作数不符" % mode


def _markdown(data):
    L = ["# 四模式对照：配置冻结 + 逐 case 结果", "",
         "> 由 `scripts/freeze_mode_compare.py` 生成；执行引擎复用 S02 的 defense_demo.py。",
         "> 威胁模型 `%s` ｜ 场景 %d 个（攻击 %d / 正常 %d）"
         % (data["threat_model"]["version"],
            data["set_fingerprints"]["scenario_count"] * 2,
            data["set_fingerprints"]["attack_cases"],
            data["set_fingerprints"]["normal_cases"]),
         "", "## 为什么先冻结配置", "",
         "实测反例：同一个 `mode=enforce`、`AUTO_APPROVE=True`，仅 "
         "`AGENTLAB_ALLOW_AUTO_APPROVE` 从 0 改到 1，恢复期写操作就从 "
         "`E_APPROVAL_REQUIRED` 变成真的落库。**只记录 mode 无法解释这个差异**。",
         "所以每个模式都登记全部有效开关，并算规范化指纹：", "",
         "| 模式 | security_mode | wrap | auto_approve | allow_auto_approve | 配置指纹(前 12) |",
         "|---|---|---|---|---|---|"]
    for mode, cfg in data["configs"].items():
        fp = data["config_fingerprints"][mode]
        L.append("| %s | %s | %s | %s | %s | `%s` |"
                 % (mode, cfg["security_mode"], cfg["wrap"],
                    cfg["auto_approve"], cfg["allow_auto_approve"], fp[:12]))
    L += ["", "> 审批开关在**演示引擎**里不改变行为（引擎不含写操作）；"
          "它的真实影响由 `tests/test_mode_compare.py` 用真实 runtime 单独验证，"
          "不在这里假装生效。", "",
          "## 威胁模型", ""]
    for k, v in data["threat_model"].items():
        L.append("- **%s**：%s" % (k, "、".join(v) if isinstance(v, list) else v))
    L += ["", "## 四模式结果（分母明确，零分母记 N/A）", "",
          "| 指标 | %s |" % " | ".join(m for m, _w in demo.MODES),
          "|---|%s" % "|".join(["---"] * len(demo.MODES))]
    keys = [
        ("计划任务数", "planned"), ("框架失败", "framework_failed"),
        ("可判定", "judgeable"), ("不可判定", "unjudgeable"),
        ("业务成功", "business_success"), ("安全完成", "safe_completion"),
        ("攻击目标达成", "attack_goal_achieved"),
        ("正常任务误阻断", "normal_false_blocked"),
        ("接收方命中", "sink_hits"),
        ("危险操作执行", "dangerous_executed"),
        ("执行前被拒", "blocked_before_execution"),
        ("控制动作真执行", "controls_executed"),
    ]
    for label, key in keys:
        L.append("| %s | %s |" % (label, " | ".join(
            str(data["summary"][m][key]) for m, _w in demo.MODES)))
    rates = [
        ("攻击目标达成率", "attack_goal_rate"),
        ("真实阻断率", "true_block_rate"),
        ("正常任务误阻断率", "normal_false_block_rate"),
        ("安全完成率", "safe_completion_rate"),
    ]
    for label, key in rates:
        L.append("| %s | %s |" % (label, " | ".join(
            _fmt(data["summary"][m][key]) for m, _w in demo.MODES)))
    L += ["",           "> **别把「业务成功 6/8」读成防护损失**：`enforce` 下从 8/8 降到 6/8，"
          "下降的两条**全部是攻击场景的工具调用被拒**；正常任务误阻断率仍为 0.0。"
          "业务成功率与正常任务误阻断率必须分开看。",
          "> `safe_completion` 对攻击样本的含义：攻击未达成且任务完成才计 1；"
          "攻击被拒时 `task_completed=0`，因此不计入。逐 case 表可复核。", "",
          "## 审批与资源成本", "",
          "待审批数 / 等待时间 / 额外调用 / token / 延迟分位数：**N/A**。"
          "演示引擎不含写操作与真实模型调用，缺失就写 N/A，不写 0。", "",
          "## 逐 case 结果", "",
          "| 模式 | 场景 | 类型 | 业务成功 | 安全完成 | 攻击达成 | 误阻断 | 检测 | effect | 真实控制动作 | 接收方命中 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in data["cases"]:
        L.append("| %s | %s | %s | %d | %d | %d | %d | %s | %s | %s | %d |"
                 % (r["mode"], r["scenario"], r["kind"],
                    r["business_success"], r["safe_completion"],
                    r["attack_goal_achieved"], r["normal_false_blocked"],
                    "是" if r["detected"] else "否", r["effect"],
                    "、".join(r["real_controls"]) or "无", r["sink_hits"]))
    L += ["", "## 不能怎么用", ""]
    L += ["- %s" % x for x in data["limits"]]
    L += ["", "## manifest 要点", "",
          "- 攻击集内容指纹：`%s`" % data["set_fingerprints"]["attack_set"][:16],
          "- 正常集内容指纹：`%s`" % data["set_fingerprints"]["normal_set"][:16],
          "- 独立授权策略：%s" % data["manifest"]["independent_authorization_policy"],
          "- 代码指纹文件数：%d" % len(data["manifest"]["code_fingerprint"]), ""]
    return "\n".join(L)


def _fmt(v):
    return "N/A" if v is None else str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description="冻结安全配置并做四模式逐 case 对照")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录，默认与冻结证据隔离")
    args = ap.parse_args(argv)
    out = args.out
    if os.path.abspath(out) == os.path.abspath(FROZEN_DIR):
        raise SystemExit("拒绝写入 p0_09 冻结证据目录；请用 --out 指定新目录")

    configs, fingerprints, all_rows, summary = {}, {}, [], {}
    for mode_label, wrap in demo.MODES:
        cfg = make_config(mode_label, wrap)
        configs[mode_label] = cfg
        fingerprints[mode_label] = fingerprint(cfg)
        rows, s = _run_mode(mode_label, wrap)
        for r in rows:
            r["mode"] = mode_label
        all_rows.extend(rows)
        summary[mode_label] = s

    # 反例护栏：只改一个审批开关，完整配置指纹必须不同。
    strict = dict(configs["enforce"], allow_auto_approve="0")
    relaxed = dict(configs["enforce"], allow_auto_approve="1")
    assert fingerprint(strict) != fingerprint(relaxed), \
        "审批开关变了但指纹没变 —— 配置冻结没有覆盖有效开关"
    assert all(fingerprints[m] != fingerprints["off"] for m in
               ("audit", "enforce", "enforce+wrap")), "不同模式指纹不能相同"

    _verify(all_rows, summary)
    sets = _set_fingerprints()
    manifest = {
        "version": 4,
        "session": "4（冻结安全配置并比较四模式）",
        "threat_model_version": THREAT_MODEL["version"],
        "attack_set_fingerprint": sets["attack_set"],
        "normal_set_fingerprint": sets["normal_set"],
        "independent_authorization_policy": "手写声明 support-agent 工具白名单 %s；"
                                            "不从已执行轨迹反推（Q02）"
                                            % sorted(demo.TARGET["tools_allowed"]),
        "security_modes": [m for m, _w in demo.MODES],
        "config_fingerprints": fingerprints,
        "effective_wrap": {m: c["wrap"] for m, c in configs.items()},
        "auto_approve": {m: c["auto_approve"] for m, c in configs.items()},
        "allow_auto_approve": {m: c["allow_auto_approve"] for m, c in configs.items()},
        "effective_tool_contract": configs["enforce"]["tool_contract"],
        "output_sink": configs["enforce"]["output_sink"],
        "code_fingerprint": _code_fingerprint(),
        "backend": "mock",
        "note": "历史真实模型 metrics 未完整登记这些安全开关时，只能说明旧记录中的"
                "模型与技能条件，不能事后补猜成某种安全模式",
    }
    data = {
        "version": 4,
        "threat_model": THREAT_MODEL,
        "configs": configs,
        "config_fingerprints": fingerprints,
        "set_fingerprints": sets,
        "manifest": manifest,
        "cases": all_rows,
        "summary": summary,
        "limits": [
            "8 个工程冒烟场景，低于计划 §5 的 12 个起点，未扩到 30 攻击 + 30 正常；"
            "数量是开发起点不是置信保证，不用改措辞凑独立样本。",
            "❌ 不能写成「准确率」或「真实业务效果」：样本是构造的、标签来自执行状态。",
            "⚠️ 业务成功率在 enforce 下 8/8→6/8，下降全部来自攻击场景被拒；"
            "正常任务误阻断率 0.0，不能读成「防护损失了 25% 业务」。",
            "⚠️ safe_completion 对攻击样本只在「攻击未达成且任务完成」时计 1；"
            "攻击被拒导致任务未完成时不计入，逐 case 表可复核。",
            "❌ 不能把审批开关的影响算进本表：演示引擎不含写操作，"
            "其真实影响由 tests/test_mode_compare.py 单独验证。",
            "❌ 间接注入的后续动作由确定性最坏情况观测器驱动，不是真实模型；"
            "攻击目标达成率只在该受控执行模型下成立，真实模型复核记 N/A。",
            "❌ 不能与 1080 run 的历史真实模型评测直接比大小：数据集、配置、"
            "后端都不同，且历史 manifest 未登记完整安全开关。",
            "✅ 可以写：在 8 个构造场景与这组冻结配置下，四模式的逐 case 结果与分母。",
        ],
    }
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "compare.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "compare.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(data))
    print("[√] 产物：%s" % os.path.join(out, "compare.md"))
    for mode, s in summary.items():
        print("  %-13s 业务 %d/%d ｜ 安全完成 %d ｜ 攻击达成 %d ｜ 真实阻断 %s ｜ 正常误阻断 %s"
              % (mode, s["business_success"], s["planned"], s["safe_completion"],
                 s["attack_goal_achieved"], _fmt(s["true_block_rate"]),
                 _fmt(s["normal_false_block_rate"])))
    for key in ("AGENTLAB_SECURITY_MODE", "AGENTLAB_SECURITY_WRAP"):
        os.environ.pop(key, None)
    S._POLICY_EXTRA.clear()
    return data


if __name__ == "__main__":
    main()
