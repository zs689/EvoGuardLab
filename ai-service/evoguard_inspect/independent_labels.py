# -*- coding: utf-8 -*-
"""Q01 / Q03：三种真值分开存 + 独立标注协议（含 Cohen's kappa）。

推进计划 §7 Q01：分别保存**执行状态**、**可观察故障**、**业务结果标签**。
任务失败不意味着任意轨迹规则都应能检出；轨迹自洽但业务错误时需要外部业务判据；
`recovered` 里确实存在过的攻击发现也不一定是假阳性，不能仅因最终成功就否定安全规则。

推进计划 §7 Q03：外部轨迹 ≥30 条、独立标注者（不看规则实现）、第二人复核争议、
双人标注算 kappa。**目前这些条件都不具备**：
  - 37 条全部来自本仓库 mock 执行，不是可授权使用的外部业务轨迹；
  - 标注者就是规则作者本人；
  - 没有第二位复核者，kappa 无法计算。
所以本模块只建立协议与「待复核」清单，所有条目标 `pending_independent_review`，
**绝不把自生成标签改称人工标签**。
"""
import json

TRUTH_DIMENSIONS = {
    "execution_state": "执行状态：clean / recovered / failed（来自注入动作，不是人工阅读结论）",
    "observable_fault": "可观察故障：注入的故障类型；轨迹里能不能看到是另一回事",
    "business_outcome": "业务结果：最终 success 与否；与「规则该不该报」不是同一件事",
}

LABEL_BASIS = {
    "execution_ground_truth": "来自执行时的注入状态（当前全部样本）",
    "independent_human": "独立标注者按任务约束标注，未读取规则实现（当前 0 条）",
    "external_policy": "外部政策版本 / 流程定义等业务判据（当前 0 条）",
}

REVIEW_PENDING = "pending_independent_review"
REVIEW_NEEDS_DISCUSSION = "needs_discussion"
REVIEW_RESOLVED = "resolved"


def make_label_record(entry):
    """把一条自生成标注转成三档真值 + 待复核状态。"""
    return {
        "key": entry["key"],
        "source": entry.get("file"),
        "agent": entry.get("agent"),
        "truth": {
            "execution_state": entry.get("label"),
            "observable_fault": entry.get("fault"),
            "business_outcome": ("success" if entry.get("success")
                                 else "failure" if entry.get("success") is not None
                                 else None),
        },
        "label_basis": "execution_ground_truth",
        "independent_annotation": None,
        "reviewers": [],
        "review_status": REVIEW_PENDING,
        "note": entry.get("note", ""),
        "why_not_human_label": "标签来自注入状态，标注者即规则作者；没有第二位复核者",
    }


def cohen_kappa(a, b, labels=None):
    """双人标注一致性。分母为 0 或完全一致导致 pe=1 时给 None，不写 0。"""
    if len(a) != len(b):
        raise ValueError("两份标注长度不同")
    n = len(a)
    if n == 0:
        return None
    cats = sorted(set(a) | set(b)) if labels is None else list(labels)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    if pe >= 1.0:
        return None
    return round((po - pe) / (1 - pe), 3)


def disagreements(a_records, b_records, dimension="execution_state"):
    """列出分歧，标 `needs_discussion` —— 不自动取多数，也不静默丢弃。"""
    b_by_key = {r["key"]: r for r in b_records}
    out = []
    for r in a_records:
        other = b_by_key.get(r["key"])
        if not other:
            continue
        va = r.get("truth", {}).get(dimension)
        vb = other.get("truth", {}).get(dimension)
        if va != vb:
            out.append({"key": r["key"], "dimension": dimension,
                        "a": va, "b": vb,
                        "status": REVIEW_NEEDS_DISCUSSION,
                        "note": "分歧先讨论再定，不直接取多数"})
    return out


def build_registry(labels_doc, external_traces=None):
    """生成待复核清单。external_traces 目前只接受显式传入的可授权外部轨迹。"""
    entries = [make_label_record(e) for e in labels_doc.get("entries", [])]
    ext = list(external_traces or [])

    # 外部轨迹必须自带来源与授权说明，否则不算「可授权使用」。
    valid_ext = []
    rejected_ext = []
    for t in ext:
        if not t.get("source") or not t.get("authorization"):
            rejected_ext.append({"key": t.get("key"),
                                 "reason": "缺来源或授权说明，不计入外部轨迹"})
        else:
            valid_ext.append(t)

    return {
        "version": 1,
        "protocol": "independent-label-v1",
        "truth_dimensions": TRUTH_DIMENSIONS,
        "label_basis": LABEL_BASIS,
        "counts": {
            "self_generated": len(entries),
            "external_authorized": len(valid_ext),
            "external_rejected": len(rejected_ext),
            "independent_human_labels": 0,
            "resolved": 0,
            "pending_independent_review": len(entries),
            "needs_discussion": 0,
        },
        "kappa": None,
        "kappa_reason": "没有第二位独立标注者，双人一致性无法估计；零分母给 null 不写 0",
        "required_for_real_accuracy": [
            "至少 30 条可授权使用的外部轨迹，覆盖正常 / 恢复成功 / 失败 / 攻击尝试",
            "独立标注者按任务约束标注，不读取规则实现",
            "第二位复核者核争议案例，分歧先讨论再定",
            "两人标同一批并算 Cohen's kappa",
            "开发与测试集按任务家族划分，冻结后不根据失败题继续调规则",
        ],
        "entries": entries,
        "external_traces": valid_ext,
        "external_rejected": rejected_ext,
        "limits": [
            "❌ 不能把 execution_ground_truth 改称人工标签。",
            "❌ 不能把 37 条自生成样本说成外部真实业务轨迹。",
            "❌ 不能把现有两档 recall/precision 写成真实业务准确率。",
            "✅ 可以写：已建立独立标注协议与待复核清单，外部轨迹与独立标注尚未到位。",
        ],
    }
