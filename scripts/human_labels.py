# -*- coding: utf-8 -*-
"""人工标注的最小工具：点 60 次，10 分钟，不需要懂标注。

为什么需要它：`experiments/independent_labels_v1` 给出的 kappa 是**两个自动标注器
之间**的一致性，不是人工一致性。那条数字在简历/README 里只能写成「代理标注一致性」。
要把它变成「人工一致性」，只差一次人真的点一遍 —— 所以把成本压到最低：
一个离线 HTML，一条一条看，点「是注入 / 不是」，导出 JSON，再跑一条命令算 kappa。

两条模式：

    python scripts/human_labels.py --make
        # 从中文语料里确定性地抽 30 条攻击 + 30 条正常，生成单页 HTML
        # 产物在 data/external/label_sheet/（**不入库**，因为它含正文）

    python scripts/human_labels.py --score ~/Downloads/labels.json
        # 读回导出的标签，算 Cohen's kappa 与分歧清单
        # 产物在 experiments/human_labels_v1/

为什么抽 60 条而不是全部：人工标注的成本随条数线性涨，而 kappa 的置信区间在
60 条时已经能给出有意义的结论。抽多了他就不做了 —— 一个做完的 60 条，
胜过一份永远没开始的 262 条。

抽样必须可复算：按 `text_sha256` 排序取前 N，不用 random。换台机器跑出来是同一批。
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "external", "chinese", "cases.jsonl")
SHEET_DIR = os.path.join(ROOT, "data", "external", "label_sheet")
OUT = os.path.join(ROOT, "experiments", "human_labels_v1")

N_EACH = 30


def load_cases():
    if not os.path.exists(DATA):
        raise SystemExit("没有语料：先跑 scripts/import_chinese_corpus.py")
    with open(DATA, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    # 只取「攻击 / 正常」两类，且都要有正文
    attacks = [r for r in rows if r["label"] == "attack" and r["text"].strip()]
    benign = [r for r in rows if r["label"] != "attack" and r["text"].strip()]
    return rows, attacks, benign


def sample(rows, n):
    """按内容哈希排序取前 n —— 确定性抽样，换机器结果一样。"""
    return sorted(rows, key=lambda r: r["text_sha256"])[:n]


def cohen_kappa(a, b):
    """两列 0/1 标签的 Cohen's kappa。分母为 0 时返回 None（不写 0）。"""
    n = len(a)
    if not n:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1 = sum(a) / n
    pb1 = sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe >= 1:
        return None
    return round((po - pe) / (1 - pe), 3)


# case_id 前缀 → 语料类别。这几类在 docs/attack-datasets.md 第十节里本来就是分开报的，
# 混成一个 kappa 会把「口径差」误读成「标注能力差」，所以 score() 必须同时给分档数。
CATEGORY = {
    "zh_harm": "harmful_request",
    "zh_direct": "direct_jailbreak",
    "zh_ind": "indirect_injection",
    "zh_ind_clean": "indirect_clean",
    "zh_benign": "benign",
}
# 项目真正要验的入口：间接注入 + 它的成对干净对照（等价于 InjecAgent 的 base/对照结构）
INJECTION_SCOPE = ("zh_ind", "zh_ind_clean")


def _category(case_id):
    return CATEGORY.get(case_id.split("/")[0], "other")


def _stat(ks, truth, human):
    """一档内的 n / 一致率 / kappa / 分歧数。kappa 算不出就 None，不写 0。"""
    a = [truth[k] for k in ks]
    b = [human[k] for k in ks]
    n = len(ks)
    return {
        "cases": n,
        "agreement_rate": round(sum(1 for x, y in zip(a, b) if x == y) / n, 3)
        if n else None,
        "cohen_kappa": cohen_kappa(a, b),
        "disagreement_count": sum(1 for x, y in zip(a, b) if x != y),
    }


def make(n_each=N_EACH, out_dir=SHEET_DIR):
    _, attacks, benign = load_cases()
    picked = ([dict(r, _truth="attack") for r in sample(attacks, n_each)]
              + [dict(r, _truth="benign") for r in sample(benign, n_each)])
    # 打散顺序：固定按哈希排，避免「前一半都是攻击」这种可猜的规律
    picked.sort(key=lambda r: hashlib.sha256(
        (r["text_sha256"] + "|sheet").encode()).hexdigest())
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_HTML.replace("__CASES__", json.dumps(
            [{"id": r["case_id"], "text": r["text"]} for r in picked],
            ensure_ascii=False)))
    return path, len(picked)


def score(labels_path, out=OUT, n_each=N_EACH):
    with open(labels_path, encoding="utf-8") as f:
        got = json.load(f)
    # 人工觉得「是注入」= 1
    human = {k: (1 if v == "attack" else 0) for k, v in got.items()}
    _, attacks, benign = load_cases()
    truth = {r["case_id"]: (1 if r["label"] == "attack" else 0)
             for r in sample(attacks, n_each) + sample(benign, n_each)}
    keys = sorted(k for k in truth if k in human)
    if len(keys) < len(truth):
        print("! 只拿到 %d/%d 条标签，kappa 按已有的算" % (len(keys), len(truth)))
    a = [truth[k] for k in keys]
    b = [human[k] for k in keys]
    disagree = [{"case_id": k, "corpus_label": truth[k], "human_label": human[k]}
                for k in keys if truth[k] != human[k]]
    by_cat = {}
    for cat in sorted(set(_category(k) for k in keys)):
        by_cat[cat] = _stat([k for k in keys if _category(k) == cat], truth, human)
    inj = [k for k in keys if k.split("/")[0] in INJECTION_SCOPE]
    res = {
        "version": 2,
        "annotator": "human (single)",
        "cases": len(keys),
        "agreement_rate": round(sum(1 for x, y in zip(a, b) if x == y) / len(keys), 3)
        if keys else None,
        "cohen_kappa_vs_corpus": cohen_kappa(a, b),
        "by_category": by_cat,
        "injection_scope": _stat(inj, truth, human),
        "disagreements": disagree,
        "disagreement_count": len(disagree),
        "note": "这是**人工 vs 语料标签**的一致性。语料标签本身也是本项目/上游给的，"
                "所以它衡量的是「人工与既有标注是否一致」，不是「标注绝对正确」。"
                "只有一位标注者，因此没有「标注者之间」的 kappa。"
                "顶层 cohen_kappa_vs_corpus 把「有害请求」与「间接注入」混在一个二分类里，"
                "会被口径差压低，**不能单独当作标注质量或准确率**；请优先看 "
                "injection_scope 与 by_category（分入口报，与 docs/attack-datasets.md 第十节一致）。",
    }
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return res


_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>EvoGuardLab 人工标注（60 条）</title>
<style>
 body{font:15px/1.6 -apple-system,"Microsoft YaHei",sans-serif;max-width:860px;
      margin:0 auto;padding:24px;color:#111827;background:#fff}
 h1{font-size:19px;margin:0 0 6px} .sub{color:#6b7280;font-size:13px;margin-bottom:18px}
 .card{border:1px solid #e5e7eb;border-radius:10px;padding:14px;margin:12px 0}
 .text{white-space:pre-wrap;background:#f9fafb;border-radius:6px;padding:10px;
       font:13px/1.55 ui-monospace,Consolas,monospace;max-height:220px;overflow:auto}
 .row{margin-top:10px;display:flex;gap:10px;align-items:center}
 label{cursor:pointer;padding:6px 12px;border:1px solid #d1d5db;border-radius:999px}
 input:checked + span{font-weight:700}
 .done{opacity:.55}
 #bar{position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #eee;
      display:flex;gap:12px;align-items:center}
 button{padding:8px 16px;border-radius:8px;border:1px solid #111827;background:#111827;
        color:#fff;cursor:pointer;font-size:14px}
 .hint{color:#6b7280;font-size:12.5px}
</style></head><body>
<h1>人工标注：这条文本是不是「注入/越狱」？</h1>
<div class="sub">只有两个选项，凭直觉点。不确定就选「不是」 —— 宁可漏，不要猜。
共 __N__ 条，约 10 分钟。点完按底部按钮导出 JSON。</div>
<div id="bar"><strong id="cnt">0 / __N__</strong>
  <span class="hint">每点一条自动记一条；中途关掉页面进度会丢，请一次做完。</span></div>
<div id="list"></div>
<div style="margin:26px 0 60px"><button onclick="exportJSON()">导出 labels.json</button>
  <span class="hint" id="warn"></span></div>
<script>
const CASES = __CASES__;
const ans = {};
const list = document.getElementById('list');
CASES.forEach((c, i) => {
  const d = document.createElement('div');
  d.className = 'card'; d.id = 'c' + i;
  d.innerHTML = '<div class="text"></div>' +
    '<div class="row">' +
    '<label><input type="radio" name="a'+i+'" value="attack"><span>是注入</span></label>' +
    '<label><input type="radio" name="a'+i+'" value="benign"><span>不是</span></label>' +
    '</div>';
  d.querySelector('.text').textContent = c.text;
  d.querySelectorAll('input').forEach(el => el.onchange = () => {
    ans[c.id] = el.value;
    d.classList.add('done');
    document.getElementById('cnt').textContent = Object.keys(ans).length + ' / ' + CASES.length;
  });
  list.appendChild(d);
});
function exportJSON(){
  const missing = CASES.length - Object.keys(ans).length;
  if (missing > 0 && !confirm('还有 ' + missing + ' 条没标，仍然导出？')) return;
  const blob = new Blob([JSON.stringify(ans, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'labels.json'; a.click();
  document.getElementById('warn').textContent = '已导出。把它放到任意位置，跑 --score 即可。';
}
</script></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="人工标注：生成单页 + 算 kappa")
    ap.add_argument("--make", action="store_true", help="生成标注页")
    ap.add_argument("--score", metavar="LABELS_JSON", help="读回导出的标签算 kappa")
    ap.add_argument("--n", type=int, default=N_EACH, help="每类抽多少条（默认 30）")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.make:
        path, n = make(a.n)
        html = open(path, encoding="utf-8").read().replace("__N__", str(n))
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print("[√] 标注页：%s" % path)
        print("    共 %d 条（攻击 %d + 正常 %d，顺序已按哈希打散）" % (n, a.n, a.n))
        print("    用浏览器打开，点完导出 labels.json，再跑：")
        print("    python scripts/human_labels.py --score <labels.json>")
        return 0
    if a.score:
        r = score(a.score, a.out, a.n)
        print("[√] 产物：%s" % os.path.join(a.out, "result.json"))
        print("    全体 %d 条 ｜ 一致率 %s ｜ kappa %s（混合口径，勿单独引用）"
              % (r["cases"], r["agreement_rate"], r["cohen_kappa_vs_corpus"]))
        s = r["injection_scope"]
        print("    间接注入档 %d 条 ｜ 一致率 %s ｜ kappa %s  ← 主口径"
              % (s["cases"], s["agreement_rate"], s["cohen_kappa"]))
        print("    分档（n / 一致率 / kappa）：")
        for cat, st in sorted(r["by_category"].items()):
            print("      %-20s %2d / %s / %s"
                  % (cat, st["cases"], st["agreement_rate"], st["cohen_kappa"]))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
