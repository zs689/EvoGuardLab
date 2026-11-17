# 外部审查响应（2026-11-17）

> 收到 20 条 finding（P0×5 / P1×10 / P2×5）。**逐条核实后**再动手：
> 抽查 8 条全部为真，其中 1 条比报告说的更严重、1 条部分不成立。
> 下面按「已修 / 部分修 / 未修（附原因）」如实登记。

## 总览

| 处置 | 条数 | 说明 |
|---|---|---|
| **已修**（有代码+验证） | 9 | 含 3 条 P0 |
| **部分修** | 4 | 修了主路径，边界仍在 |
| **标注/归档**（不重写） | 2 | agentlab |
| **未修**（架构/环境/需人） | 5 | 每条给方案与前置条件 |

---

## P0

### ✅ EG-PKG-001 轮子漏声明子包 —— **比报告更严重**

报告说漏 `evoguard_inspect.static`。实际 `find` 一下：`loaders/`、`rules/`、
`static/` **三个子包全漏了**，wheel 里 `evoguard_inspect` 只有 `__init__.py`。

- `pyproject.toml` 补齐三个子包；
- `check_wheel_manifest.py` REQUIRED 加 5 个关键文件；
- `check_build_context.py` 增加**反向校验**：源码树里每个含 `__init__.py`
  的目录都必须在 `packages` 里（防同类问题复发）。
- 重建 wheel 验证：`static/` 5 个、`rules/` 5 个、`loaders/` 4 个文件都在，门禁 PASS。

### ✅ EG-GEN-002 ONNX 判官硬编码路径 + 读管道无超时

- 去掉 `Scripts\python.exe` 硬编码与越界父目录：改跨平台探测
  （`.venv`/`_pi_venv` × `Scripts/python.exe`/`bin/python`，`EVOGUARD_PI_PYTHON` 可覆盖）。
- `readline` 改为线程 + `join(timeout)`，超时 **kill 子进程并重建**。
  原实现超时后线程会持着 `self._lock` 永久阻塞 → 整个服务死锁。
- 新增 2 项测试：假子进程睡 5 秒，0.3s 内返回 `error` 且不阻塞。

### ✅ EVG-BUG-001 evidence-gate 三个语义 bug

| bug | 修法 | 实测 |
|---|---|---|
| 多引用惩罚 | 先把所有 cite 的证据 **union**，再统一判覆盖率与数字 | `[差旅#3.1][差旅#2.1]` + 450/5500 → **可核验**（原来必报） |
| 子串漏检 | 数字改**集合比对** + 归一化（去千分位/尾零） | 证据 `5500`、回答写 `550` → **数字对不上**（原来漏放） |
| 章节号误报 | corpus 的 section 文本保留 **`sec_id` + 标题** | 「根据 3.1 条规定」→ **可核验**（原来报错） |

### ⚠️ AL-BUG-001 agentlab 作弊式恢复 —— 标注而非重写

`_recover()` 确实靠 `suppress` 关闭故障注入来「恢复」。本轮：
- 把硬编码 `0` 的延迟改为真实累计耗时；
- 新增 `DEPRECATED.md`，如实写明「恢复靠跳过故障」「历史指标无效」
  「Mode B 文档与实现不符」「并发不安全」，并说明**不要修复、直接用 EvoGuardLab**。

不重写的原因：修复成本高于在 EvoGuardLab 重做，而 EvoGuardLab 已有 984 项回归。

### ❌ EG-GEN-001 工具契约/职责/角色硬编码

**未修**。这是架构级改造：要把 `DEFAULT_CONTRACTS`、`TOOL_SCHEMAS`、
`TOOL_SCOPE`、`AGENT_TOOLS` 从源码常量抽成注册表，并写
JSON Schema / OpenAPI / MCP Tool 三种适配器。方案：

1. 新增 `tool_registry.py`：`register_tool(name, schema, scope, danger)` + 从
   JSON Schema 生成 `TOOL_SCHEMAS`；
2. `TOOL_SCOPE` 改为「未声明的工具按 `unknown` 处理并默认 `deny`」
   （现在是 `info` 放行，这是真实风险）；
3. 工作量 2–3 天，需配套测试。本轮未动，避免半成品。

---

## P1

| ID | 处置 | 说明 |
|---|---|---|
| EG-GEN-003 probe 写死订单域 | ✅ 已修 | 按 `manifest.example_task/tools/dangerous_tools` 构造探针，无声明才回退 |
| EG-SEC-002 JSON 解析脆/fail-open | ✅ 已修 | 改「首 `{` 到末 `}`」+ 逐个起点重试；失败仍报 `error` → 网关转 review |
| EG-SEC-001 双重否定绕过 | ⚠️ 部分 | 已剔除「不要忘记/别忘记/never fail to」等双重否定；**24 字窗口与陈述句式未动** |
| EG-ARC-002 空壳目录/pyc/前缀 | ✅ 已修 | 删 `backend/infra/datasets`、616 个 `__pycache__`；新增 `evoguard/env.py` 做 `EVOGUARD_*`→`AGENTLAB_*` 启动期映射（零调用点改动） |
| EVG-BUG-002 docx 句柄/preamble 栈污染 | ✅ 已修 | `with zipfile.ZipFile(...)`；preamble 不再压进父级栈 |
| EVG-CODE-001 私人文案 | ✅ 已修 | 「发给阿光看」改为「反馈给维护者」 |
| EG-SEC-003 v3 未跑 / L1 召回 0.029 | ❌ 未修 | 预注册要求「未参与调参的一方复跑」，本机无法自证；中文 L1 召回低是已知负结果，已归档 |
| EG-ARC-001 单进程内存态 | ❌ 未修 | 限流/熔断/指标/作业队列要换 Redis+PG，环境不具备；已写进 `docs/enterprise-readiness.md` |
| EVG-ARC-001 evidence-gate v1/v2 割裂 | ❌ 未修 | 缺 6 个模块（依赖图/混合检索/受约束生成…），周级工作量 |
| EVG-GEN-001 分词只二元/标题扁平 | ❌ 未修 | 需加 unigram + 多级标题树，改动集中在 `tokenize.py`/`corpus.py`，下轮可做 |

---

## P2

| ID | 处置 |
|---|---|
| AL-GEN-001 agentlab 白盒/线程安全 | ⚠️ 已随 `DEPRECATED.md` 标注 |
| AL-CFG-001 agentlab 配置漂移 | ⚠️ 同上（废弃，不修） |
| WS-HYG-002 根目录散落 | ⚠️ 部分：`labels.json` **复制**到 `EvoGuardLab/experiments/labels/human_labels_60.json`（**原件保留**）；`NVIDIA Corporation/`、`_nemo_doc.txt` 未动（不是我的文件） |
| WS-HYG-001 工作区 3.22GB | ⚠️ 部分：删掉实测无改善的 `model_ft.onnx*`（−700MB，`_pi_model` 2.1G→1.4G）；`_enterprise_review_*`/`_review_work_*`/`_plan_work*` **没删**——需要你确认后再动 |
| REF-ORG-001 learn/github 无 manifest | ❌ 未修 |

---

## 诚实说明

1. **报告的 `EVG-BUG-001` 第 2 条**（子串绕过）在 `gate.py` 确实存在，但
   `agent_watch` 的语义判官**不存在**报告担心的 fail-open：网关看到
   `error` 就转 `review`，不会当成「安全」。修的是解析脆弱性，不是新漏洞。
2. **`WS-HYG-001` 的临时目录我没删**。`_enterprise_review_*` 等不是本轮产物，
   里面可能有你要留的审查证据。要删请明确说，我按清单逐项确认后再动。
3. **`EG-SEC-001` 只修了双重否定**。24 字窗口和陈述句式（「系统规程要求…」）
   仍是绕过面，属已知未修。
