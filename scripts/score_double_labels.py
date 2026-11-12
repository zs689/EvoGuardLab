# -*- coding: utf-8 -*-
"""两位真人标注者之间的一致性（inter-annotator Cohen's kappa）。

这是评审 §7 要的那个数：**人 vs 人**。它和 `human_labels.py --score` 的
「人 vs 语料标签」是两件完全不同的事，也不能叫准确率。

硬约束（不然后果很严重）：
  1. 两人必须标**完全同一批** case_id。抽样由 human_labels.sample() 确定性复算，
     map 的 values、a、b 三者必须都等于这一批，否则拒绝出数。
  2. 第二位标注者发回的是匿名编号 r2_XXX（盲标页的产物），用 round2_id_map.json
     还原成真实 case_id；映射缺失/越界直接报错，不猜。
  3. 缺条不静默：少一条就在结果里写 received / expected，按交集算并标记。
  4. 单档 kappa 恒为 0/None（每档语料标签是常量），必须攻击+对照成对算，
     所以主口径给 injection_scope，和 human_labels.py v2 保持一致。

输入：
  --a    第一位标注者的 labels.json（key = 真实 case_id，如 Downloads/labels.json）
  --b    第二位标注者的 labels_annotator2.json（key = 匿名 r2_XXX）
  --map  experiments/human_labels_v1/round2_id_map.json（默认）
产物：experiments/human_labels_v1/double_result.json

    python scripts/score_double_labels.py --a <a.json> --b <b.json>
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import human_labels as H  # noqa: E402

DEFAULT_MAP = os.path.join(H.OUT, "round2_id_map.json")
DEFAULT_OUT = os.path.join(H.OUT, "double_result.json")


def expected_batch(n_each=H.N_EACH):
    """从语料复算首轮抽样，换机器结果一样。"""
    _, attacks, benign = H.load_cases()
    return {r["case_id"] for r in
            (H.sample(attacks, n_each) + H.sample(benign, n_each))}


def load_map(path):
    with open(path, encoding="utf-8") as f:
        m = json.load(f)["anon_to_case_id"]
    return m


def _to01(v):
    if v == "attack" or v == 1 or v is True:
        return 1
    if v == "benign" or v == 0 or v is False:
        return 0
    raise ValueError("标签只能是 attack / benign，收到 %r" % (v,))


def load_labels(path, id_map=None):
    """返回 {真实 case_id: 0/1}。匿名编号用 map 还原。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for k, v in raw.items():
        if k.startswith("r2_"):
            if id_map is None or k not in id_map:
                raise ValueError("匿名编号 %s 没有映射（map 缺失或版本不对）" % k)
            cid = id_map[k]
        else:
            cid = k
        if cid in out:
            raise ValueError("映射后出现重复 case_id：%s" % cid)
        out[cid] = _to01(v)
    return out


def _stat(ks, a, b):
    x = [a[k] for k in ks]
    y = [b[k] for k in ks]
    n = len(ks)
    return {
        "cases": n,
        "agreement_rate": round(sum(1 for p, q in zip(x, y) if p == q) / n, 3)
        if n else None,
        "cohen_kappa": H.cohen_kappa(x, y),
        "disagreement_count": sum(1 for p, q in zip(x, y) if p != q),
    }


def score(a_path, b_path, map_path=DEFAULT_MAP, out=DEFAULT_OUT,
          n_each=H.N_EACH, write=True, map_a_path=None):
    id_map = load_map(map_path)
    a_map = load_map(map_a_path) if map_a_path else None
    batch = expected_batch(n_each)

    # 映射表本身必须恰好覆盖这一批，防止拿错版本 / 被改过
    if set(id_map.values()) != batch:
        raise ValueError("第二位的 id 映射表与抽样批次不一致（--n 与 --map 要配套）")
    if a_map is not None and set(a_map.values()) != batch:
        raise ValueError("第一位的 id 映射表与抽样批次不一致（--n 与 --map-a 要配套）")

    # A 默认用真实 case_id；若第一位也用了匿名补标页，用 --map-a 单独还原，
    # 两份页面的匿名编号彼此独立，不能共用一张表。
    a = load_labels(a_path, a_map)
    b = load_labels(b_path, id_map)

    missing_a = sorted(batch - set(a))
    missing_b = sorted(batch - set(b))
    extra_b = sorted(set(b) - batch)
    if extra_b:
        raise ValueError("第二位标注者发回了不属于这一批的 id：%s" % extra_b[:5])
    if set(a) - batch:
        raise ValueError("第一位标注者的标签里有不属于这一批的 id")

    keys = sorted(set(a) & set(b))
    unpaired_b = sorted(set(b) - set(a))
    unpaired_a = sorted(set(a) - set(b))
    warnings = []
    if missing_a or missing_b:
        warnings.append("只拿到 A %d/%d 条、B %d/%d 条；kappa 按两人交集 %d 条算"
                        % (len(a), len(batch), len(b), len(batch), len(keys)))
    if unpaired_b or unpaired_a:
        warnings.append("有 %d 条只有 B 标、%d 条只有 A 标，未计入 kappa"
                        % (len(unpaired_b), len(unpaired_a)))

    by_cat = {}
    for cat in sorted(set(H._category(k) for k in keys)):
        by_cat[cat] = _stat([k for k in keys if H._category(k) == cat], a, b)
    inj = [k for k in keys if k.split("/")[0] in H.INJECTION_SCOPE]

    res = {
        "version": 1,
        "metric": "inter-annotator Cohen's kappa (human vs human)",
        "annotators": ["annotator1 (真实 case_id)", "annotator2 (匿名 r2_XXX)"],
        "expected_cases": len(batch),
        "received_a": len(a),
        "received_b": len(b),
        "cases": len(keys),
        "agreement_rate": _stat(keys, a, b)["agreement_rate"],
        "cohen_kappa_inter_annotator": H.cohen_kappa(
            [a[k] for k in keys], [b[k] for k in keys]),
        "by_category": by_cat,
        "injection_scope": _stat(inj, a, b),
        "disagreements": [
            {"case_id": k, "annotator1": a[k], "annotator2": b[k]}
            for k in keys if a[k] != b[k]
        ],
        "disagreement_count": sum(1 for k in keys if a[k] != b[k]),
        "unpaired": {
            "only_b": len(unpaired_b),
            "only_a": len(unpaired_a),
            "note": "只有一方标注的条目不进入 kappa。加长批里这通常是第二位多标的题，"
                    "先留着；等第一位补标同一批后可重跑，把交集扩到全批。"
                    "这些单人标签不能写成「标注者间一致性」，也不等于准确率。",
        },
        "warnings": warnings,
        "note": "这是**两位真人标注者之间**的 Cohen's kappa，不是「人 vs 语料标签」，"
                "更不是准确率。两位标的是完全同一批 60 条、口径沿用首轮页面原话。"
                "若两人标签完全相同、且两列都含 0 和 1，会得到 kappa 1.0，但这既可能是"
                "真一致、也可能是第二位抄了第一位——数据本身无法判别，只能靠标注独立性"
                "保证（盲标页不含第一位答案、不含语料标签、不含规则实现）。"
                "若两人把整批全标成同一类（两列都是常量），pe=1 分母为 0，kappa 无定义，"
                "返回 None，既不能写成 1.0 也不能写成 0，此时只有一致率可读。"
                "单档 kappa 恒为 0/None，主口径看 injection_scope（攻击+成对对照）。"
                "加长批（--n 100 → 200 条）必然包含首轮 60 条；若第一位只标了首轮，"
                "kappa 只在 60 条交集上算，多出来的题记在 unpaired.only_b，不混进分母。"
                "只有两位标注者、一个样本批次，不能外推成标注质量或系统准确率。",
    }
    if write:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="算两位真人标注者之间的 kappa")
    ap.add_argument("--a", required=True, help="第一位标注者 labels.json")
    ap.add_argument("--b", required=True, help="第二位标注者 labels_annotator2.json")
    ap.add_argument("--map", default=DEFAULT_MAP,
                    help="第二位标注者的匿名编号映射表")
    ap.add_argument("--map-a", default=None,
                    help="第一位若也用了匿名补标页，单独指定它的映射表（默认 A 为真实 case_id）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--n", type=int, default=H.N_EACH,
                    help="每类抽样条数；加长批用 100（共 200 条），默认 30（共 60 条）")
    a = ap.parse_args(argv)
    try:
        r = score(a.a, a.b, a.map, a.out, a.n, map_a_path=a.map_a)
    except ValueError as e:
        print("[×] %s" % e)
        return 2
    print("[√] 产物：%s" % a.out)
    print("    %d/%d 条 ｜ 一致率 %s ｜ 标注者间 kappa %s"
          % (r["cases"], r["expected_cases"], r["agreement_rate"],
             r["cohen_kappa_inter_annotator"]))
    s = r["injection_scope"]
    print("    间接注入档 %d 条 ｜ 一致率 %s ｜ kappa %s  ← 主口径"
          % (s["cases"], s["agreement_rate"], s["cohen_kappa"]))
    u = r["unpaired"]
    if u["only_b"] or u["only_a"]:
        print("    ! 只有 B 标了 %d 条 / 只有 A 标了 %d 条，未计入 kappa"
              % (u["only_b"], u["only_a"]))
    for w in r["warnings"]:
        print("    ! %s" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
