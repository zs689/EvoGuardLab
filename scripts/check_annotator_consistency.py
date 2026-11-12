# -*- coding: utf-8 -*-
"""同一位第二标注者**两次作答**的稳定性（intra-annotator，不是标注者间 kappa）。

背景：第二位标注者先后导出了 60 条基础页和 200 条加长页两份结果。
加长批必然包含首轮那 60 条，所以可以拿同一批 case_id 比对这 60 条上
他自己的两次判断，估计「标注噪声」有多大。

这个数**不能**冒充：
  - 标注者间 kappa（那是 `score_double_labels.py` 算的 A vs B）；
  - 准确率 / 标注质量（没有金标准）；
  - 严格重测信度（两次间隔很短、顺序与匿名编号独立但可能仍有记忆/疲劳效应）。

用法：
    python scripts/check_annotator_consistency.py \
        --b60 <labels_annotator2.json> --b200 <labels_ext_annotator2.json>
产物：experiments/human_labels_v1/intra_annotator_check.json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import human_labels as H              # noqa: E402
import score_double_labels as S       # noqa: E402

DEFAULT_MAP60 = os.path.join(H.OUT, "round2_id_map.json")
DEFAULT_MAP200 = os.path.join(H.OUT, "round2_ext_annotator_id_map.json")
DEFAULT_OUT = os.path.join(H.OUT, "intra_annotator_check.json")


def check(b60_path, b200_path, map60=DEFAULT_MAP60, map200=DEFAULT_MAP200,
          out=DEFAULT_OUT, write=True):
    m60, m200 = S.load_map(map60), S.load_map(map200)
    core = set(m60.values())
    if not core <= set(m200.values()):
        raise ValueError("加长批没有完整包含首轮 60 条，无法比对同一批")
    b60 = S.load_labels(b60_path, m60)
    b200 = S.load_labels(b200_path, m200)

    keys = sorted(core & set(b60) & set(b200))
    if len(keys) != len(core):
        raise ValueError("有一方缺条，拒绝出数（缺 %d 条）"
                         % (len(core) - len(keys)))

    diffs = [{"case_id": k, "category": H._category(k),
              "first_60_sheet": b60[k], "second_200_sheet": b200[k]}
             for k in keys if b60[k] != b200[k]]
    by_cat = {}
    for cat in sorted({H._category(k) for k in keys}):
        ks = [k for k in keys if H._category(k) == cat]
        by_cat[cat] = S._stat(ks, b60, b200)

    res = {
        "version": 1,
        "metric": "intra-annotator consistency (one person, two sittings)",
        "cases": len(keys),
        "agreement_rate": S._stat(keys, b60, b200)["agreement_rate"],
        "cohen_kappa_same_annotator": H.cohen_kappa(
            [b60[k] for k in keys], [b200[k] for k in keys]),
        "disagreement_count": len(diffs),
        "disagreements": diffs,
        "by_category": by_cat,
        "inputs": {
            "first": os.path.basename(b60_path),
            "second": os.path.basename(b200_path),
            "map_first": os.path.basename(map60),
            "map_second": os.path.basename(map200),
        },
        "note": "这是**同一个人两次作答**的一致性，不是两位标注者之间的 kappa，"
                "不是准确率，也不是标注质量（没有金标准）。两次页码、顺序、匿名编号"
                "各自独立，但间隔很短且可能受记忆/疲劳影响，不能当严格重测信度。"
                "它只用于解释：A-vs-B 在两份页上的 kappa 差异，有多少来自 B 自身噪声。",
    }
    if write:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="同一位标注者两次作答的一致性")
    ap.add_argument("--b60", required=True, help="60 条基础页导出")
    ap.add_argument("--b200", required=True, help="200 条加长页导出")
    ap.add_argument("--map60", default=DEFAULT_MAP60)
    ap.add_argument("--map200", default=DEFAULT_MAP200)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    try:
        r = check(a.b60, a.b200, a.map60, a.map200, a.out)
    except ValueError as e:
        print("[×] %s" % e)
        return 2
    print("[√] 产物：%s" % a.out)
    print("    同一人两次 %d 条 ｜ 一致率 %s ｜ kappa %s ｜ 分歧 %d 条"
          % (r["cases"], r["agreement_rate"],
             r["cohen_kappa_same_annotator"], r["disagreement_count"]))
    print("    （这是同人两次，不是标注者间 kappa）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
