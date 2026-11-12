# -*- coding: utf-8 -*-
"""生成【盲标页】—— 默认与首轮完全同一批 60 条；可扩成加长批。

为什么必须同一批：评审 §7 要的是「标注者之间」的 Cohen's kappa。
两个人标不同样本算不出 kappa；抽样沿用 human_labels.sample()（按 text_sha256
确定性排序取前 30+30），换机器、换时间跑出来都是同一批 id。

扩样规则（重要）：
  - `sample()` 是「排序后取前 n」，所以 **n=100 的 200 条必然包含 n=30 的 60 条**。
    这样加长批仍能和首轮配对，只是交集先按 60 条算；第一位若再补标加长版，
    交集就能扩到 200 条。
  - 加长批用**独立 tag / 独立 seed / 独立映射表**，不覆盖已发出的 60 条包。
  - 匿名编号仍统一 r2_XXX（score_double_labels.load_labels 靠这个前缀识别），
    但每份页面各自从 001 重新编号，两份之间不对应，避免互相泄题。

盲标约束（对应 docs/external-labeling-protocol.md §三第 2 条）：
  - 页面里**没有**语料标签、没有另一位标注者的答案、没有规则实现、没有「建议」；
  - **case_id 换成匿名编号**：原始 id 前缀（zh_harm / zh_ind_clean …）
    会泄露语料类别，留在 HTML 源码里就不算盲标。映射表另存仓库内部，仅供复算；
  - 判定问法、选项、提示语**一字不改**沿用首轮页面模板 H._HTML，
    保证两人做的是同一个任务（否则一致性没有意义）；
  - 只换：题目呈现顺序（不同哈希种子打散）、导出文件名、一段「请独立完成」的说明。

产物（data/external/label_sheet/ 已被 .gitignore 排除，因为含语料正文）：
  round2_annotator.html              默认 60 条，第二位标注者
  README_for_annotator2.txt         可直接转发给同学的说明
  round2_ext_annotator.html         --n 100 --tag ext_annotator：200 条加长版（给同学）
  round2_ext_self.html             --n 100 --tag ext_self --role self：同批 200 条（给自己补标）
仓库内部（不含正文，可入库）：
  experiments/human_labels_v1/round2_id_map.json
  experiments/human_labels_v1/round2_ext_annotator_id_map.json
  experiments/human_labels_v1/round2_ext_self_id_map.json

用法：
    python scripts/make_round2_sheet.py                       # 60 条，默认包（字节不变）
    python scripts/make_round2_sheet.py --n 100 --tag ext_annotator
    python scripts/make_round2_sheet.py --n 100 --tag ext_self --role self
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import human_labels as H  # noqa: E402

DEFAULT_SEED = "|round2"
SHEET_SEED = "|ext_annotator"
SELF_SEED = "|ext_self"

NOTE = """<div style="background:#eef3ff;border:1px solid #c7d7fe;border-radius:8px;
 padding:10px;font-size:13px;margin-bottom:14px;line-height:1.7">
<b>%(title)s · 请独立完成</b><br>
下面的判定问题与%(other)s完全相同。请凭直觉作答，<b>不要查资料、不要和别人讨论</b>；
没有标准答案，也不会有人核对你「对不对」——要看的正是你自己的判断。<br>
页面不会保存进度，请一次做完；文本只在本机浏览器里，<b>不联网、不上传</b>。
</div>"""

REMARK_DEFAULT = """这是第二位标注者用的盲标页，与第一位标注者标的是完全同一批 60 条，
用于计算「标注者之间」的 Cohen's kappa。页面里不含语料标签、不含第一位
标注者的答案、不含检测规则，避免污染标注独立性。"""

REMARK_EXT = """这是第二位标注者用的加长版盲标页（200 条），其中含首轮那 60 条，
用于把「标注者之间」的 kappa 从 60 条扩到最多 200 条。页面里不含语料标签、
不含第一位标注者的答案、不含检测规则，避免污染标注独立性。"""

REMARK_SELF = """这是第一位标注者补标用的加长版，与发给第二位标注者的加长版是同一批
200 条（顺序与匿名编号各自独立）。页面里不含对方的答案、不含语料标签。"""

EXTRA_EXT = """
【重要】这一份是加长版（200 条），里面已经包含之前那份 60 条的题目。
只标这一份就行，旧的 60 条链接不用再点，别两套都标。
"""

EXTRA_SELF = """
【说明】这是给你自己补标用的加长版，题目和发给同学那份完全同一批 200 条，
但顺序不同、编号独立，你看不到对方的答案。标完把导出的 json 留着即可。
"""

README_TXT = """EvoGuardLab 人工标注 —— 麻烦花%(approx)s%(mins)d 分钟帮个忙

【做什么】
1. 双击打开 %(html)s（Chrome / Edge 都行）。
   不需要联网，不需要安装任何东西，一个 HTML 文件就够了。
2. 里面有 %(total)d 条中文文本。逐条读，只回答一个问题：
       「这条文本是不是『注入 / 越狱』？」
   觉得是        -> 点「是注入」
   觉得不是/拿不准 -> 点「不是」
3. 凭直觉点就行。不用查资料，也不要和别人讨论，独立完成。
   没有标准答案，别猜我想要什么答案。
4. %(total)d 条点完（页面顶部计数变成 %(total)d / %(total)d），拉到最底部点
   「导出 %(export)s」。
5. 把导出的那个 json 文件原样发回来就行，不用改文件名、不用改内容。
   （文件里的编号是 r2_001 这类匿名编号，正常的，不是出错。）
%(extra)s
【注意】
- 页面不自动保存进度，中途关掉就要重来，请一次坐定点完。
- 文本只在你的浏览器里，不上传、不联网，也不会记录你是谁。
- 大约 %(mins)d 分钟，%(total)d 次点击。谢谢！

【给收件人的备注（转发时可删）】
%(remark)s
"""


def out_paths(tag):
    """tag 为空时输出路径与最初版本完全一致，保证已发出的包不被改动。"""
    s = "_" + tag if tag else ""
    return (
        os.path.join(H.SHEET_DIR, "round2%s_annotator.html" % s
                     if not tag else "round2_%s.html" % tag),
        os.path.join(H.SHEET_DIR, "README_for_annotator2%s.txt" % s),
        os.path.join(H.OUT, "round2%s_id_map.json" % s),
    )


def export_name(tag, role):
    if not tag:
        return "labels_annotator2.json"
    if role == "self":
        return "labels_ext_self.json"
    return "labels_ext_annotator2.json"


def note_and_remark(tag, role):
    if role == "self":
        return ("第一位标注者（补标）", "第二位标注者", REMARK_SELF, EXTRA_SELF)
    if tag:
        return ("第二位标注者", "第一位标注者", REMARK_EXT, EXTRA_EXT)
    return ("第二位标注者", "第一位标注者", REMARK_DEFAULT, "")


def picked(n_each, seed):
    """同一批 id，换哈希种子打散呈现顺序（防顺序记忆，不影响集合）。"""
    _, attacks, benign = H.load_cases()
    rows = ([dict(r) for r in H.sample(attacks, n_each)]
            + [dict(r) for r in H.sample(benign, n_each)])
    rows.sort(key=lambda r: hashlib.sha256(
        (r["text_sha256"] + seed).encode()).hexdigest())
    return rows


def make(n_each=H.N_EACH, tag="", seed=None, role="annotator2"):
    if seed is None:
        seed = SELF_SEED if role == "self" else (SHEET_SEED if tag else DEFAULT_SEED)
    html_path, txt_path, map_path = out_paths(tag)
    title, other, remark, extra = note_and_remark(tag, role)
    export = export_name(tag, role)

    rows = picked(n_each, seed)
    id_map = {}
    cases = []
    for i, r in enumerate(rows, 1):
        anon = "r2_%03d" % i
        id_map[anon] = r["case_id"]
        cases.append({"id": anon, "text": r["text"]})
    total = len(cases)

    html = H._HTML
    html = html.replace("__CASES__", json.dumps(cases, ensure_ascii=False))
    html = html.replace("__N__", str(total))
    html = html.replace("a.download = 'labels.json'",
                        "a.download = '%s'" % export)
    html = html.replace("<h1>", (NOTE % {"title": title, "other": other})
                        + "\n<h1>", 1)

    readme = README_TXT % {
        "approx": " " if not tag else "约 ",
        "mins": max(1, round(total / 6)),
        "total": total,
        "html": os.path.basename(html_path),
        "export": export,
        "extra": extra,
        "remark": remark,
    }

    os.makedirs(H.SHEET_DIR, exist_ok=True)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(readme)

    core = {r["case_id"] for r in
            (H.sample(H.load_cases()[1], H.N_EACH)
             + H.sample(H.load_cases()[2], H.N_EACH))}
    batch = set(id_map.values())
    os.makedirs(H.OUT, exist_ok=True)
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump({
            "purpose": "map anonymous ids back to real case_ids for inter-annotator kappa",
            "sampling": "human_labels.sample(): sort by text_sha256, first %d attack + %d benign"
                        % (n_each, n_each),
            "presentation_order_seed": seed,
            "tag": tag,
            "role": role,
            "export_filename": export,
            "n": total,
            "contains_core_60": core <= batch,
            "anon_to_case_id": id_map,
        }, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return html_path, txt_path, map_path, total


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成盲标页（默认 60 条，可扩成加长批）")
    ap.add_argument("--n", type=int, default=H.N_EACH, help="每类抽多少条（默认 30 → 共 60 条）")
    ap.add_argument("--tag", default="", help="产物后缀；留空则生成原 60 条包")
    ap.add_argument("--seed", default=None, help="呈现顺序的哈希种子")
    ap.add_argument("--role", default="annotator2",
                    choices=["annotator2", "self"], help="self = 第一位标注者补标版")
    a = ap.parse_args(argv)
    h, t, m, n = make(a.n, a.tag, a.seed, a.role)
    print("[√] 盲标页：%s" % h)
    print("[√] 转发说明：%s" % t)
    print("[√] id 清单：%s" % m)
    print("    共 %d 条，n_each=%d，seed=%s" % (n, a.n, a.seed or "(默认)"))


if __name__ == "__main__":
    main()
