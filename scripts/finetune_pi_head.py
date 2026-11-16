#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""微调 DeBERTa 的**分类头**（冻结 backbone），导出 ONNX 供 onnxruntime 用。

    _pi_venv/Scripts/python.exe scripts/finetune_pi_head.py --fit --json <path>

为什么只训分类头：
- 可用中文标注只有 120 条训练样本，全量微调 184M 参数必然过拟合；
- 冻结 backbone 只训 2×768+2 = 1538 个参数，在这点数据上能收敛；
- 导出 ONNX 后主服务仍走 onnxruntime（**不需要 torch**），依赖面不变。

对比三档：原始 DeBERTa / 域内校准头 / 微调分类头。
"""
import argparse
import io
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, HERE)

MODEL_DIR = os.environ.get("AGENTLAB_PI_MODEL_DIR") or os.path.join(
    os.path.dirname(ROOT), "_pi_model")
PROBES = os.path.join(ROOT, "docs", "review-remediation", "probes")
TRAIN = os.path.join(PROBES, "pi_calib_train.jsonl")
EVAL = os.path.join(PROBES, "pi_calib_eval.jsonl")
OUT_ONNX = os.path.join(MODEL_DIR, "model_ft.onnx")
OUT_META = os.path.join(PROBES, "pi_finetune.json")


def rows(path):
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def fit(epochs=10, lr=2e-3, batch=8, seed=17):
    import torch
    from torch import nn
    from transformers import (AutoModelForSequenceClassification,
                              AutoTokenizer)
    torch.manual_seed(seed)
    random.seed(seed)

    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_DIR, num_labels=2)
    frozen = 0
    for name, p in model.named_parameters():
        if "classifier" not in name:
            p.requires_grad = False
            frozen += p.numel()
    trainable = [p for p in model.parameters() if p.requires_grad]
    n_train = sum(p.numel() for p in trainable)

    data = rows(TRAIN)
    opt = torch.optim.AdamW(trainable, lr=lr, weight_decay=0.01)
    lossf = nn.CrossEntropyLoss()
    model.train()
    hist = []
    for ep in range(epochs):
        random.shuffle(data)
        tot = 0.0
        for i in range(0, len(data), batch):
            chunk = data[i:i + batch]
            enc = tok([r["text"] for r in chunk], padding=True,
                      truncation=True, max_length=256, return_tensors="pt")
            labels = torch.tensor([int(r["label"]) for r in chunk])
            out = model(**enc)
            loss = lossf(out.logits, labels)
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss.detach())
        hist.append(round(tot / max(1, len(data) // batch), 4))

    model.eval()
    # 先把分类头权重落盘：导出失败也不能丢掉训练成果
    import torch as _t
    _ckpt = os.path.join(MODEL_DIR, "classifier_head.pt")
    _t.save({"classifier": model.classifier.state_dict()}, _ckpt)
    # 导出 ONNX：主服务用 onnxruntime 加载，不再依赖 torch
    dummy = tok(["test"], padding=True, truncation=True, max_length=64,
                return_tensors="pt")
    torch.onnx.export(
        model, (dummy["input_ids"], dummy["attention_mask"]), OUT_ONNX,
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={"input_ids": {0: "batch", 1: "seq"},
                      "attention_mask": {0: "batch", 1: "seq"},
                      "logits": {0: "batch"}},
        opset_version=14, do_constant_folding=True)
    size = os.path.getsize(OUT_ONNX) / 1048576.0
    return {"frozen_params": frozen, "trainable_params": n_train,
            "epochs": epochs, "lr": lr, "loss_history": hist,
            "onnx": OUT_ONNX, "onnx_mb": round(size, 1),
            "head_checkpoint": _ckpt}


def score_with(onnx_path, texts):
    import onnxruntime as ort
    from tokenizers import Tokenizer
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    names = {i.name for i in sess.get_inputs()}
    tk = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
    out = []
    for t in texts:
        enc = tk.encode(str(t))
        ids = enc.ids[:256] or [0]
        mask = enc.attention_mask[:256] or [1]
        feed = {"input_ids": [ids], "attention_mask": [mask]}
        if "token_type_ids" in names:
            feed["token_type_ids"] = [[0] * len(ids)]
        lg = sess.run(None, feed)[0][0]
        out.append(1 if lg[1] > lg[0] else 0)
    return out


def evaluate():
    ev = rows(EVAL)
    texts = [r["text"] for r in ev]
    labels = [int(r["label"]) for r in ev]
    res = {}
    for name, path in (("original", os.path.join(MODEL_DIR, "model_fp32.onnx")),
                       ("finetuned", OUT_ONNX)):
        if not os.path.isfile(path):
            res[name] = {"error": "missing " + os.path.basename(path)}
            continue
        pred = score_with(path, texts)
        fp = sum(1 for p, y in zip(pred, labels) if p and not y)
        tp = sum(1 for p, y in zip(pred, labels) if p and y)
        neg = sum(1 for y in labels if not y)
        pos = sum(1 for y in labels if y)
        res[name] = {"fp": fp, "neg": neg,
                     "fp_rate": round(fp / float(neg), 3) if neg else None,
                     "tp": tp, "pos": pos,
                     "recall": round(tp / float(pos), 3) if pos else None}
    # 校准头作为第三档
    cal_path = os.path.join(PROBES, "pi_calib_weights.json")
    if os.path.isfile(cal_path):
        import calibrate_pi as C
        import prompt_injection_onnx as P
        sc = P.OnnxScorer()
        cal = json.load(io.open(cal_path, encoding="utf-8"))
        fp = tp = 0
        for r in ev:
            p = sc.score(r["text"])["p_injection"]
            x = C.features(r["text"], p)
            z = sum(a * c for a, c in zip(cal["weights"], x)) + cal["bias"]
            pred = 1 if C._sig(z) >= 0.5 else 0
            if pred and not r["label"]:
                fp += 1
            if pred and r["label"]:
                tp += 1
        neg = sum(1 for r in ev if not r["label"])
        pos = sum(1 for r in ev if r["label"])
        res["calibrated"] = {"fp": fp, "neg": neg,
                             "fp_rate": round(fp / float(neg), 3),
                             "tp": tp, "pos": pos,
                             "recall": round(tp / float(pos), 3)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    rep = {}
    if a.fit:
        rep["train"] = fit()
    rep["eval"] = evaluate()
    rep["note"] = ("训练/评估数据均为模板构造（120/120），只能证明方法有效性，"
                   "不证明线上泛化；冻结 backbone 只训分类头")
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    print(json.dumps(rep.get("eval", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
