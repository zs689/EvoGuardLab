# -*- coding: utf-8 -*-
"""EvoGuard Lab · AI 服务核心包。

模块（每个都对应规格里的一层，没有空壳）：
  core        数据表 / 状态机 / 错误码
  sim         模拟业务 + 工具 + 故障变形（无真实外部依赖）
  agents      三个 Target Agent
  gateway     模型网关（mock / 本地 OpenAI 兼容）
  runtime     执行 / 检查点 / 诊断 / 恢复 / 审批
  invariants  业务不变量（借鉴 AgentRx 的思路，自己实现）
  experience  经验提取 + 校验 + 记忆治理
  skills      候选技能 / 独立验证 / 晋升 / 回滚 / 负迁移检测
  evaluate    A/B/C/D 四组对照 + 指标 + 报告

尚未实现（不建空壳）：RAG 向量检索、FastAPI 接口、Java 业务层、Vue 前端。
"""
__version__ = "0.2.0"
