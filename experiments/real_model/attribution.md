# C / D 命中归因（不是新实验，只是把已有 run 的原因摊开）

> 由 `ai-service/real_model_attribution.py` 从 `runs.jsonl` + `data/real_model.db` 生成。
> 用途：解释「C 为什么在这个数字上」，避免把匹配问题误读成模型问题。

## 一、每个 agent 在 C / D 下命中的技能

| 组 | 模式 | 成功/总数 | 命中的技能（次数） | 策略链（次数） |
|---|---|---|---|---|
| single/order | C | 72/72 | sk_6ce4f4c1c1×72 | ["replay_checkpoint"]×72 |
| single/order | D | 72/72 | (无技能·按根因查表)×60, sk_96d522a039×6, sk_e960cae01c×6 | ["replay_checkpoint"]×18 / ["retry"]×12 / ["fallback_tool"]×12 / ["fallback_tool", "cross_verify"]×12 / ["cross_verify"]×12 / ["reformat"]×6 |
| single/kb | C | 66/66 | sk_60cd58202d×66 | ["replay_checkpoint"]×66 |
| single/kb | D | 66/66 | (无技能·按根因查表)×48, sk_19da6633ce×6, sk_b81a20cf45×6, sk_f36c1971dd×6 | ["retry"]×12 / ["fallback_tool"]×12 / ["fallback_tool", "cross_verify"]×12 / ["replay_checkpoint"]×12 / ["cross_verify"]×12 / ["reformat"]×6 |
| single/planner | C | 66/66 | sk_6e6104cc98×66 | ["replay_checkpoint"]×66 |
| single/planner | D | 60/66 | (无技能·按根因查表)×60, sk_6b0d3aaece×6 | ["fallback_tool"]×18 / ["retry"]×12 / ["replay_checkpoint"]×12 / ["human_takeover"]×6 / ["reformat"]×6 / ["cross_verify"]×6 / ["fallback_tool", "cross_verify"]×6 |
| composite/order | C | 0/12 | sk_6ce4f4c1c1×12 | ["replay_checkpoint"]×12 |
| composite/order | D | 12/12 | sk_96d522a039×12, sk_e960cae01c×12 | ["fallback_tool", "cross_verify"]×24 |
| composite/kb | C | 0/12 | sk_60cd58202d×12 | ["replay_checkpoint"]×12 |
| composite/kb | D | 12/12 | sk_19da6633ce×12, sk_b81a20cf45×12 | ["fallback_tool", "cross_verify"]×24 |
| composite/planner | C | 0/6 | sk_6e6104cc98×6 | ["replay_checkpoint"]×6 |
| composite/planner | D | 6/6 | sk_6b0d3aaece×12 | ["fallback_tool", "cross_verify"]×12 |

## 二、复合故障：按故障组合看各模式

| 故障组合 | A | B | C | D |
|---|---|---|---|---|
| incorrect_planning+premature_termination | 0/6 | 0/6 | 0/6 | 6/6 |
| missing_field+silent_corruption | 0/6 | 0/6 | 0/6 | 6/6 |
| retrieval_failure+tool_hallucination | 0/6 | 0/6 | 0/6 | 6/6 |
| stale_data+context_loss | 0/6 | 0/6 | 0/6 | 6/6 |
| wrong_tool+format_violation | 0/6 | 0/6 | 0/6 | 6/6 |
