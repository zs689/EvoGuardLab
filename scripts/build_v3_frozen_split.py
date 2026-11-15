#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v3 预注册：确定性生成冻结划分（**不跑实验、不产生 result.json**）。

    python scripts/build_v3_frozen_split.py --out experiments/semantic_injection_v3/frozen_split.json
    python scripts/build_v3_frozen_split.py --freeze   # 已冻结则拒绝二次生成

约束来自 docs/semantic-injection-v3-plan.md §三：
- 按 attacker_instruction / 模板组**整组划分**，绝不按条随机切；
- test_unseen_schema 的工具 schema 与 calibration 完全无交集；
- 五个切片逐组报数（中文/英文/长文本/正常任务/模型不可用）；
- 全部确定性生成、无随机、无联网；每个集合写 sha256 与条数；
- 生成后 integrity_sha256 锁死，--freeze 拒绝二次生成。

**边界**：这里只冻结「划分」，不生成检测结果。合成样本不代表真实世界
分布，也不构成 v3 实验结论；v3 仍需未参与调参的一方独立复跑。
"""
import argparse
import hashlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(
    ROOT, "experiments", "semantic_injection_v3", "frozen_split.json")

# 整组划分表：group_id -> split。显式写死，不用取模随机。
GROUPS = 24
GROUP_TO_SPLIT = {}
for _g in range(GROUPS):
    if _g < 10:
        GROUP_TO_SPLIT[_g] = "calibration"
    elif _g < 16:
        GROUP_TO_SPLIT[_g] = "test_seen"
    elif _g < 20:
        GROUP_TO_SPLIT[_g] = "test_unseen_schema"
    elif _g < 22:
        GROUP_TO_SPLIT[_g] = "overdefense"
    else:
        GROUP_TO_SPLIT[_g] = "long_tail"

# 工具 schema 也整组划分：seen / unseen 无交集
SEEN_SCHEMAS = ("mail.search", "calendar.create", "file.read",
                "ticket.update", "contact.lookup")
UNSEEN_SCHEMAS = ("crm.export", "iot.unlock", "ledger.transfer",
                  "fleet.dispatch", "genome.query")

SLICES = ("zh", "en", "long_text", "normal_task", "model_unavailable")
SAMPLES_PER_GROUP = 12          # 每组 12 条，5 切片轮转覆盖


def _sha(obj):
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build():
    samples = []
    for g in range(GROUPS):
        split = GROUP_TO_SPLIT[g]
        unseen = split == "test_unseen_schema"
        pool = UNSEEN_SCHEMAS if unseen else SEEN_SCHEMAS
        for i in range(SAMPLES_PER_GROUP):
            sl = SLICES[i % len(SLICES)]
            schema = pool[g % len(pool)]
            label = 0 if (split == "overdefense" or
                          (split == "calibration" and i % 3 == 0)) else 1
            samples.append({
                "id": "v3-%s-g%02d-%03d" % (split, g, i),
                "split": split,
                "group_id": "g%02d" % g,
                "slice": sl,
                "schema": schema,
                "schema_seen_in_calibration": not unseen,
                "label": label,
                "payload_at_end": split == "long_tail",
                "text": ("[%s/%s/%s] synthetic case %d for schema %s"
                         % (split, sl, "unseen" if unseen else "seen",
                            i, schema)),
            })
    # 整组划分：同一 group_id 只能出现在一个 split
    by_split = {}
    for s in samples:
        by_split.setdefault(s["split"], []).append(s)
    report = {
        "schema_version": 1,
        "generator": "scripts/build_v3_frozen_split.py",
        "method": "deterministic synthetic; template-group-wise split; no RNG/network",
        "groups": GROUPS,
        "samples_per_group": SAMPLES_PER_GROUP,
        "slices": list(SLICES),
        "seen_schemas": list(SEEN_SCHEMAS),
        "unseen_schemas": list(UNSEEN_SCHEMAS),
        "splits": {
            name: {"count": len(rows),
                   "sha256": _sha(rows),
                   "groups": sorted({r["group_id"] for r in rows}),
                   "slices": sorted({r["slice"] for r in rows}),
                   "schemas": sorted({r["schema"] for r in rows})}
            for name, rows in sorted(by_split.items())
        },
        "total": len(samples),
        "gate_sha256": None,        # 由调用方填，避免这里 import 判定线
        "notes": [
            "只冻结划分，不是实验结果；result.json 不存在。",
            "合成样本不代表真实世界分布，v3 结论需独立复跑。",
        ],
    }
    try:
        sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
        import v3_gate
        report["gate_sha256"] = v3_gate.gate_sha256()
    except ImportError:
        pass
    report["integrity_sha256"] = _sha(
        {k: v for k, v in report.items() if k != "integrity_sha256"})
    return report


def verify_frozen(path):
    """文件已存在且完整性有效 → 视为已冻结。"""
    if not os.path.isfile(path):
        return False, "not_exists"
    try:
        with io.open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False, "corrupt"
    frozen = d.pop("integrity_sha256", None)
    if frozen != _sha(d):
        return False, "tampered"
    return True, "frozen"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--freeze", action="store_true",
                    help="已冻结则拒绝二次生成")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    ok, state = verify_frozen(a.out)
    if a.freeze and ok:
        print("[×] 冻结集已存在且完整性有效，拒绝二次生成：%s" % a.out)
        return 2
    if state == "tampered":
        print("[×] 冻结集被篡改，拒绝覆盖：%s" % a.out)
        return 3
    rep = build()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        for k, v in sorted(rep["splits"].items()):
            print("  %-20s %3d 条 / %d 组  sha256=%s"
                  % (k, v["count"], len(v["groups"]), v["sha256"][:12]))
        print("[√] %s（total=%d, integrity=%s）"
              % (a.out, rep["total"], rep["integrity_sha256"][:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
