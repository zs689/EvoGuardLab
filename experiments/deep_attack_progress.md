# DEEP 攻坚方案执行进度（2026-11-22）

对应方案：`EvoGuardLab_深水区攻坚方案_64到85分_2026-11-22.json`

**原则**：不把 NOT_RUN、p=1.0 或 daemon 不可达的项写成「完成」。

| Workstream | 状态 | 可核验产物 |
|---|---|---|
| DEEP-01 默认硬拦截漏口 | **完成**（1 项 gate 因 Docker 关闭 skip） | `env.py`、`openai_proxy.py`、`tests/test_openai_proxy.py`、`review_checks.py` |
| DEEP-02 coupling 写前拦截 | **实现完成，验收部分达标** | `skill_hooks.py` pre_commit、`autonomous_agent.py` 写前门禁、`tests/test_declarative_assertions.py`、`env_bench_deep_r1..r3.json` |
| DEEP-03 第二真实模型 | **阻塞（需用户决策）** | 本机 Ollama 已卸载（仅剩 `F:\ollama\OllamaSetup.exe`），其余 5 个 provider 无 API key。未伪造 DONE。 |
| DEEP-04 公开语料 + L2 | **L2 判官修复并重跑中** | `scripts/eval_public_benchmarks.py`（judge 适配器）、`pub_bench_layered.json` |
| DEEP-05 巨石文件拆分 | **部分完成** | `autonomous_agent.py` 1057→498 ✅；`security.py` 1201→~810（未达 500）；`runtime.py` 1320（未拆） |

## DEEP-01：确认为真漏洞并修复

`ENTRY_SECURE_DEFAULTS` 确实漏了 `EVOGUARD_OUTPUT_BLOCK=1`。
更关键的是：**该变量原本是死变量**——除 `profile_meta` 展示外无人读取，
所以只在 env.py 补一行不会有任何效果。

修复：
- `env.py` 补默认项；
- `openai_proxy.output_blocking_enabled()` 接入非流式输出门禁
  （显式 0 只关额外拦截，不会把 security=enforce 也关掉）；
- 红队测试：AWS Key / RSA 私钥在裸启动默认下返回 `content_filter`。

gate 的 `live_capability` 从 8 项增至 **11 项**：
裸启动双拦、taint Base64 编码外发、Docker 孤儿容器回收。
第三项因本机 Docker daemon 关闭**如实 SKIP**（未用桩伪造通过）。

## DEEP-02：三轮真实实测（不粉饰）

| 轮次 | 总体 no→with | p | coupling no→with | pagination no→with |
|---|---|---|---|---|
| r1 | 0.35→0.75 | 3.1e-05 | 0.80→1.00 (p=0.5) | 0.00→0.00 |
| r2 | 0.38→0.75 | 6.1e-05 | 1.00→1.00 (p=1.0) | 0.00→0.00 |
| r3 | 0.38→0.72 | 5.2e-04 | 0.90→0.90 (p=1.0) | 0.00→0.00 |

**达标项**：coupling 三轮 with 均值 **96.7%**、无单轮倒退；
pagination 0-shot **0%**（恢复了区分度）。

**未达标项（如实记录）**：
- coupling 的 McNemar p 全部 ≥0.5：因为 No-Skill 臂也有运行时探针反馈，
  两臂都接近满分，**差异不显著**。不能声称写前拦截相对基线有统计显著增益。
- pagination 的 With-Skill 仍 0%：训练只触发了"尾页 None"一条规约，
  注入的 hint 不完整；这是负结果，未粉饰为成功。

顺带修掉一个真 bug：`skill_hooks._symbol_values` 不支持 dict 字面量，
导致 dict 形式的 `REQUIRED` 永远被判为不同步（此前 coupling p=1.0 的原因之一）。

## DEEP-04：发现并修复"假 L2 实测"

原 `eval_public_benchmarks.py` 传给 `guard_input_semantic` 的是裸函数，
而后者要求 `.judge()` 对象 → 136 条全部记为 `L2_unavailable`，
`L2_semantic=0` 是**假的规则层实测**，不是判官真的不行。
已加 `_JudgeAdapter` 把任意 ProviderSpec 模型适配成契约格式并重跑。

## DEEP-03：为什么是 NOT_RUN 而不是伪造 DONE

- 本机 Ollama 程序与 `qwen3:4b` 模型此前已删除，只剩安装包；
- `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` / `GLM_API_KEY` / `MOONSHOT_API_KEY` 均未设置；
- 方案约束「严禁把 NOT_RUN 当已完成」——故不生成假的 `model2_*.json`。

要推进需要二选一：提供第二家 API key，或允许重新安装 Ollama + 拉取开源编码模型。

## DEEP-05：拆分进度

- `autonomous_agent.py` **1057 → 498 行**（达标）。拆出 session_store /
  parallel_tools / agent_utils / tool_naming / model_call / reflection，
  经 `agent_reexport` 门面统一 re-export（含下划线名），全量测试通过。
- `security.py` 1201 → ~810 行：拆出 security_rules / semantic_detectors /
  text_hygiene。继续拆 guard/intent/DB 块的跨模块全局状态同步风险高，
  **未强行降到 500**，避免为了行数指标破坏安全语义。
- `runtime.py` 1320 行本轮未拆。
