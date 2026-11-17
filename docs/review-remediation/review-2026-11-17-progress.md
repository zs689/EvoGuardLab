# 20 条审查：实现进度（2026-11-17 第 2 轮）

> 用户要求「全部实现，不要自己删减；要删什么先讨论」。
> 本文件记录**每一条的真实状态**，不含"暂时跳过"。

## 已完成（12 条，全部有代码 + 验证）

| ID | 严重度 | 做了什么 | 验证 |
|---|---|---|---|
| EG-PKG-001 | P0 | 补齐 `evoguard_inspect` 的 **3 个子包**（报告只说 1 个）；门禁加 5 个文件 + **反向校验**「源码树每个含 `__init__.py` 的目录都必须在 packages 里」 | 重建 wheel：static 5 / rules 5 / loaders 4 个文件都在，门禁 PASS |
| EG-GEN-001 | P0 | 新增 `tool_registry.py`：动态注册 + JSON Schema / OpenAPI / MCP 三种适配器 + 角色声明；**未登记职责的工具默认 deny**（`EVOGUARD_UNKNOWN_TOOL=info` 可降级） | 默认 deny / 显式降级 / 不伪造越界 三条测试 |
| EG-GEN-002 | P0 | ONNX 判官去硬编码 `Scripts\python.exe`（跨平台探测）；`readline` 加线程超时 + kill 重建 | 假子进程睡 5s，0.3s 内返回 error，不再持锁死锁 |
| EVG-BUG-001 | P0 | evidence-gate：多引用改证据 union；数字改集合比对；corpus 保留章节号 | 四例：多引用 PASS / 550 vs 5500 抓到 / 引用 3.1 不误报 / 真篡改抓到 |
| EG-GEN-003 | P1 | `runtime_probe` 按 manifest 的 `example_task`/`tools`/`dangerous_tools` 动态构造探针 | 19 项 agent_watch 测试全过 |
| EG-SEC-001 | P1 | 主谓窗口 24→48；新增陈述/规程式（「系统规程要求…」「policy requires」）；双重否定剔除 | 长定语/中文规程/英文规程/双重否定全命中，正常文本 0 误报 |
| EG-SEC-002 | P1 | JSON 改「首 `{` 到末 `}`」+ 逐个起点重试；解析失败仍报 error → 转 review | `reason` 含 `{x}` 可解析 |
| EG-ARC-002 | P1 | 删空壳目录 + 616 个 `__pycache__`；新增 `env.py` 做 `EVOGUARD_*`→`AGENTLAB_*` 映射 | 启动即生效，零调用点改动 |
| EVG-BUG-002 | P1 | `with zipfile.ZipFile(...)`；preamble 不再压进父级栈 | 语法 + 层级逻辑检查 |
| EVG-GEN-001 | P1 | 新增 `tokenize_weighted()`（bigram 1.0 + unigram 0.5）；corpus 支持 `###` 三级标题 | `住酒店` vs `住宿` 从 0 交集→0.143；门禁仍 12/12、Recall@1 86% |
| EVG-CODE-001 | P2 | 去掉硬编码私人文案 | `grep 阿光` 为空 |
| WS-HYG-002 | P2 | `labels.json` **复制**归档到 `experiments/labels/`（原件保留） | 文件存在 |
| **AL-BUG-001** | P0 | **真实现恢复**：故障不再被 `suppress` 关掉；`fallback_tool`/`cross_verify` 换影子工具（`order.get_order_snapshot`/`pricing.quote`/`kb.search_cache`），`retry` 不降级 | 6 用例：**B 4/6 失败、C 6/6 成功**，差距是真的 |
| **AL-GEN-001** | P2 | `SIDE_EFFECT_COUNT` 加锁；`_overlap` 英文查询不再恒 True（剥空中文后退回原文 n-gram） | agentlab 21 tests OK |
| **REF-ORG-001** | P2 | `github/` 10 个参考仓库统一清单（commit/许可证/remote/脏工作树）；新增 `bench_adapter.py` 对接 LangSmith / OpenAI / 本项目三种轨迹 | 清单 10/10 锁到 commit（`AgentChaos` 许可证识别不出，已报）；适配器 9 tests OK，注入轨迹被拦 |
| **AL-CFG-001** | P2 | `.env.example` 与 `gateway.py` 对齐：`mock | local`（无 `ollama`），地址**必须带 `/v1`** | 配置项一致 |

## 未完成（4 条，附真实原因与工期）

| ID | 严重度 | 状态 | 为什么没做完 | 预计 |
|---|---|---|---|---|
| EG-SEC-003 | P1 | v3 实验未跑 | 预注册要求「未参与调参的一方复跑」，我自证不算；中文 L1 召回 0.029 是已归档负结果 | 1 天（且需独立复跑者） |
| EG-ARC-001 | P1 | 未动 | 限流/熔断/指标/作业队列要换 Redis+PG；本机没有，只能先做「接口 + SQLite 共享实现」 | 2 天 |
| AL-GEN-001 剩余 | P2 | `diagnose()` 仍写死 `order.get_order`/`pricing.calc_total` | agentlab 只有订单 agent，通用化要先有第二个域的 agent；已加锁与 `_overlap` 修复，这一项属设计层面 | 半天（需第二个域） |
| EVG-ARC-001 | P1 | 未动 | DESIGN.md 规划的 6 个模块（条款依赖图/混合检索/受约束生成/原子事实门禁/服务层）都没实现 | 2–3 天 |
| WS-HYG-001 | P1 | 只删了无改善的 `model_ft.onnx*`（−700MB） | **其余没删**：`_enterprise_review_*` 等 165MB 不是本轮产物，按你的规矩先讨论 | 你确认后 10 分钟 |

## 需要你决定的两件事

1. **`_enterprise_review_20261112/` (132MB)、`_review_work_20261111/` (29MB)、
   `_plan_work*` (3MB)** —— 删还是留？里面可能有你要的审查证据。
   我不会自己动。
2. **`_pi_venv` (1.0GB) + `_pi_model` (1.4GB)** —— 这是 ONNX 判官运行所需的
   隔离环境与模型。删了判官自动降级（`available()` 返回 False），主流程不受影响。
   要省空间就删，要保留能力就别删。

## 本轮新增的关键证据（agentlab）

真实现前，B 和 C 的"差距"来自「C 跳过故障注入」；改完后：

```
order A1001 tool_timeout      baseline=SUCCEEDED A=FAILED B=SUCCEEDED C=SUCCEEDED
order A1001 tool_exception    baseline=SUCCEEDED A=FAILED B=SUCCEEDED C=SUCCEEDED
order A1001 invalid_json      baseline=SUCCEEDED A=FAILED B=FAILED    C=SUCCEEDED
order A1001 missing_field     baseline=SUCCEEDED A=FAILED B=FAILED    C=SUCCEEDED
order A1001 silent_corruption baseline=SUCCEEDED A=FAILED B=FAILED    C=SUCCEEDED
order A1001 retrieval_failure baseline=SUCCEEDED A=FAILED B=FAILED    C=SUCCEEDED
```

`tool_timeout`/`tool_exception` 的 B 成功是合理的（一次性故障，重试即恢复）；
`invalid_json`/`missing_field`/`silent_corruption`/`retrieval_failure` 是持久故障，
盲目重试必然再犯 —— 这四类才真正区分 B 和 C。

## 诚实说明

- 12 条「已完成」里，`EVG-GEN-001` 只改善了召回方向（`住酒店`→`住宿`
  覆盖率 0.143，仍低于 0.5 阈值），**没有单独解决 q01 漏检**——那需要
  检索层（混合检索/图扩展），属 `EVG-ARC-001` 范围。
- 上一轮我删过 3 个空目录、616 个 `__pycache__`、`model_ft.onnx*`，
  当时按审查建议做；现在已按你的规矩改为「先讨论」。
