# -*- coding: utf-8 -*-
"""检查本地模型是否就绪，并做一次真实结构化输出探针。

用法（在 EvoGuardLab 目录下）：
    python scripts/check_model.py
    python scripts/check_model.py --base http://127.0.0.1:1234/v1 --model qwen3-3b

它会告诉你三件事：
  1. 服务通不通
  2. 有没有已加载的模型
  3. 这个模型能不能按我们的要求输出 JSON（这是能不能接进评测的前提）
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ai-service", "evoguard"))

import gateway  # noqa: E402

PROBE = ('只输出 JSON：{"order_id": "A1001", "total": 504.0}')

SYS = ("你是业务 Agent。只看用户给出的上下文回答，不要使用上下文以外的知识。"
       "只输出一个 JSON 对象，不要输出解释、不要复述要求、不要输出思考过程。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AGENTLAB_LLM_BASE",
                                                    "http://127.0.0.1:1234/v1"))
    ap.add_argument("--model", default=os.environ.get("AGENTLAB_LLM_MODEL"))
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    p = gateway.LocalProvider(base=args.base, model=args.model, timeout=args.timeout)
    print("目标：%s" % p.base)

    try:
        models = p.list_models()
    except gateway.LLMError as e:
        print("[×] 服务不通：%s" % e.message)
        print("    在 LM Studio 里：加载模型 -> Developer 标签 -> 启动 Local Server（端口 1234）")
        return 1
    print("[√] 服务可达，已加载模型：%s" % (models or "（无）"))
    if not models:
        print("[×] 没有已加载的模型，先在 LM Studio 里加载一个")
        return 1

    try:
        model = p.resolve_model()
        text, tokens, mid = p.complete(
            [{"role": "system", "content": SYS},
             {"role": "user", "content": PROBE}],
            temperature=0.0, max_tokens=128)
    except gateway.LLMError as e:
        print("[×] 调用失败：%s" % e.message)
        return 1

    print("[√] 模型：%s ｜ 用了 %d tokens" % (mid, tokens))
    print("    原始输出：%r" % (text[:160] if text else ""))
    parsed = gateway.extract_json(text)
    if isinstance(parsed, dict):
        print("[√] JSON 可解析：%s" % parsed)
        print("\n结论：可以接进评测。设置环境变量后重跑实验：")
        print('      set AGENTLAB_LLM=local & set AGENTLAB_LLM_BASE=%s' % p.base)
        return 0
    print("[×] JSON 解析失败。这个模型当前的输出格式达不到要求，")
    print("    接进去只会得到一堆 bad_model_output。先调系统提示词，或换一个更大的模型。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
