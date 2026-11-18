# 生产化与通用化方案执行进度（2026-11-18）

依据 `EvoGuardLab_生产化与通用化终极补齐方案_2026-11-18.json`。

| WS | 状态 | 证据 |
|---|---|---|
| PROD-01 PostgresStore 共享存储 | ✅ **真 PG 实测 passed** | `probes/pg_store_e2e.json`：副本1写、副本2读到；租户B读不到A。核心零依赖不变 |
| PROD-02 透明代理（OpenAI+MCP） | ✅ 端到端 | `/v1/chat/completions` 真 HTTP 测试 6 项；`evoguard mcp-proxy` 8 项；注入输入转发前就拦 |
| PROD-03 轻量双语分类器 | ✅ 留出达标 | recall **0.8831** / FPR **0.0223**（742 条留出）；推理 p95 **0.10ms** 纯标准库 |
| PROD-04 沙箱 + BM25 记忆 | ✅ 完成 | 命令/SSRF 对抗 12 项；BM25 混合检索 + 跨名称召回测试 |
| PROD-05 GitHub 托管/外部复跑 | ⚠️ **本地准备完，外部动作待确认** | CI 加了 PG16+Redis7 services job，YAML 校验通过；**未推远端**（需用户批准） |

## 逐条实测证据（不估算、不硬编码）

- **PROD-01**：真 postgres:16-alpine + 普通角色 evoguard_app；`pg8000` 只在隔离
  venv，核心 `dependencies=[]`。踩坑：SET LOCAL 不能参数化、pg8000 execute
  返回 None、dict(tuple) 失败、历史表默认值停在 'default'、策略重复创建——
  全部在代码里修成幂等，不是靠重跑掩盖。
- **PROD-02**：注入输入断言上游 opener 调用数=0；未注册工具被拒；
  顺带补 `injection_override` 的「忽略用户目标」分支（真检测缺口）。
- **PROD-03**：先实测现有英文 DeBERTa 中文 recall 0.47/FPR 0.34 不可用；
  HF SSL 断、ModelScope 模型 409MB 许可不明 → 改自训 char n-gram+LR。
  阈值在**训练集内部**按「FPR<=0.05 下最大 recall」校准，不碰留出集。
- **PROD-04**：复用 evidence_gate.bm25，中文用 2-gram（自带 tokenize 是英文）。
- **PROD-05**：本地已加 integration job；`independent_reproduction` 仍为 false。

## 未做/待确认（如实）

1. **推 GitHub 仓库 + 远端 CI 实跑**：`git remote -v` 为空，且这是外部动作，
   未经你明确同意我不会推。
2. **邀请非作者独立复跑 v3**：同样需要你决定与操作。
3. 英文 DeBERTa 的中文误报问题已用自训轻量模型绕开，但**没有替换**原 ONNX
   权重；两套并存，按 `EVOGUARD_LIGHT_GUARD` 开关切换。

验收：`1178 tests OK / 0 ResourceWarning / 29-29 gate / build-context PASS`。
