# -*- coding: utf-8 -*-
"""造一份「已知错误」的合成轨迹，用来证明规则真的抓得住。

为什么要合成：真实数据里不一定同时存在这四类问题，而且真实样本会随数据集变化，
测试会不稳。合成样本可以精确控制「哪一步坏、坏成什么样」，
这样测试断言的是**规则本身**，不是某次运行的运气。

产出：tests/fixtures/trace_synthetic_bad.json（OTLP 格式，4 条轨迹）
    t-swallow-*    工具报错后没重试就作答   → error_swallowed
    t-writefirst-* 第一次调用就是写操作     → write_before_read
    t-repeat-*     同工具同参数调 4 次      → repeated_tool_call
    t-empty-*      答案为空                → empty_answer

跑法：python scripts/make_synthetic_traces.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "tests", "fixtures", "trace_synthetic_bad.json")


def span(tid, i, name, op, attrs, status="STATUS_CODE_OK"):
    a = [{"key": "gen_ai.operation.name", "value": {"stringValue": op}},
         {"key": "gen_ai.agent.name", "value": {"stringValue": "synthetic-agent"}}]
    a += [{"key": k, "value": {"stringValue": json.dumps(v, ensure_ascii=False)
                               if isinstance(v, (dict, list)) else str(v)}} for k, v in attrs.items()]
    return {"traceId": tid, "spanId": "%s-%02d" % (tid, i), "name": name,
            "startTimeUnixNano": str(i * 1000000), "attributes": a,
            "status": {"code": status}}


def tool(tid, i, name, args, result, err=None):
    attrs = {"gen_ai.tool.name": name,
             "gen_ai.tool.call.arguments": args,
             "gen_ai.tool.call.result": result}
    if err:
        attrs["error.type"] = err
    return span(tid, i, "execute_tool %s" % name, "execute_tool", attrs,
                "STATUS_CODE_ERROR" if err else "STATUS_CODE_OK")


def chat(tid, i, answer, err=None):
    attrs = {"gen_ai.completion": answer}
    if err:
        attrs["error.type"] = err
    return span(tid, i, "chat synthetic", "chat", attrs,
                "STATUS_CODE_ERROR" if err else "STATUS_CODE_OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    spans = []

    # 1) 工具报错后没重试就作答
    tid = "t-swallow-0001"
    spans.append(tool(tid, 1, "order.get_order", {"order_id": "X1"}, {"items": []},
                      err="timeout"))
    spans.append(chat(tid, 2, {"order_id": "X1", "total": 999.0}))
    spans.append(chat(tid, 3, {"order_id": "X1", "total": 999.0}))

    # 2) 第一次调用就是写操作
    tid = "t-writefirst-0002"
    spans.append(tool(tid, 1, "order.settle", {"order_id": "X2", "total": 100.0},
                      {"settled": True}))
    spans.append(chat(tid, 2, {"order_id": "X2", "total": 100.0}))

    # 3) 同工具同参数反复调用
    tid = "t-repeat-0003"
    for i in range(1, 5):
        spans.append(tool(tid, i, "kb.search", {"query": "退货"}, {"docs": [{"doc_id": "KB-01"}]}))
    spans.append(chat(tid, 5, {"text": "7 个工作日", "cited": ["KB-01"]}))

    # 4) 答案为空
    tid = "t-empty-0004"
    spans.append(tool(tid, 1, "task.make_plan", {"goal_key": "release"},
                      {"steps": ["冻结代码"]}))
    spans.append(chat(tid, 2, {}))

    doc = {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "synthetic-agent"}}]},
        "scopeSpans": [{"scope": {"name": "fixture"}, "spans": spans}]}]}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("写出 %s（%d 个 span，4 条轨迹）" % (OUT, len(spans)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
