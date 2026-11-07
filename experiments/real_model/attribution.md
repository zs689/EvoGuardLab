# C / D 命中归因（不是新实验，只是把已有 run 的原因摊开）

> 由 `ai-service/real_model_attribution.py` 从 `runs.jsonl` + `data/real_model.db` 生成。
> 用途：解释「C 为什么在这个数字上」，避免把匹配问题误读成模型问题。

## 一、每个 agent 在 C / D 下命中的技能

| 组 | 模式 | 成功/总数 | 命中的技能（次数） | 策略链（次数） |
|---|---|---|---|---|
| single/order | C | 72/72 | order/wrong_tool→["fallback_tool", "cross_verify"](active v2)×72 | ["fallback_tool", "cross_verify"]×72 |
| single/order | D | 72/72 | (无技能·按根因查表)×60, order/missing_field→["fallback_tool", "cross_verify"](active v2)×6, order/wrong_tool→["fallback_tool", "cross_verify"](active v2)×6 | ["replay_checkpoint"]×18 / ["retry"]×12 / ["fallback_tool"]×12 / ["fallback_tool", "cross_verify"]×12 / ["cross_verify"]×12 / ["reformat"]×6 |
| single/kb | C | 66/66 | kb/context_loss→["fallback_tool", "cross_verify"](active v2)×66 | ["fallback_tool", "cross_verify"]×66 |
| single/kb | D | 66/66 | (无技能·按根因查表)×48, kb/retrieval_failure→["fallback_tool", "cross_verify"](active v2)×6, kb/context_loss→["fallback_tool", "cross_verify"](active v2)×6, kb/stale_data→cross_verify(active v2)×6 | ["retry"]×12 / ["fallback_tool"]×12 / ["fallback_tool", "cross_verify"]×12 / ["replay_checkpoint"]×12 / ["cross_verify"]×12 / ["reformat"]×6 |
| single/planner | C | 66/66 | planner/premature_termination→["fallback_tool", "cross_verify"](active v2)×66 | ["fallback_tool", "cross_verify"]×66 |
| single/planner | D | 66/66 | (无技能·按根因查表)×54, planner/incorrect_planning→cross_verify(active v2)×6, planner/premature_termination→["fallback_tool", "cross_verify"](active v2)×6 | ["fallback_tool"]×18 / ["retry"]×12 / ["replay_checkpoint"]×12 / ["cross_verify"]×12 / ["reformat"]×6 / ["fallback_tool", "cross_verify"]×6 |
| composite/order | C | 12/12 | order/wrong_tool→["fallback_tool", "cross_verify"](active v2)×24 | ["fallback_tool", "cross_verify"]×24 |
| composite/order | D | 12/12 | order/missing_field→["fallback_tool", "cross_verify"](active v2)×12, order/wrong_tool→["fallback_tool", "cross_verify"](active v2)×12 | ["fallback_tool", "cross_verify"]×24 |
| composite/kb | C | 12/12 | kb/context_loss→["fallback_tool", "cross_verify"](active v2)×24 | ["fallback_tool", "cross_verify"]×24 |
| composite/kb | D | 12/12 | kb/retrieval_failure→["fallback_tool", "cross_verify"](active v2)×12, kb/context_loss→["fallback_tool", "cross_verify"](active v2)×12 | ["fallback_tool", "cross_verify"]×24 |
| composite/planner | C | 6/6 | planner/premature_termination→["fallback_tool", "cross_verify"](active v2)×12 | ["fallback_tool", "cross_verify"]×12 |
| composite/planner | D | 6/6 | planner/premature_termination→["fallback_tool", "cross_verify"](active v2)×12 | ["fallback_tool", "cross_verify"]×12 |

## 二、复合故障：按故障组合看各模式

| 故障组合 | A | B | C | D |
|---|---|---|---|---|
| incorrect_planning+premature_termination | 0/6 | 0/6 | 6/6 | 6/6 |
| missing_field+silent_corruption | 0/6 | 0/6 | 6/6 | 6/6 |
| retrieval_failure+tool_hallucination | 0/6 | 0/6 | 6/6 | 6/6 |
| stale_data+context_loss | 0/6 | 0/6 | 6/6 | 6/6 |
| wrong_tool+format_violation | 0/6 | 0/6 | 6/6 | 6/6 |
