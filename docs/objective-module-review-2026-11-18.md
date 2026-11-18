# EvoGuardLab 模块客观水平评测（2026-11-18）

**方法**：6 组独立精读全部 63 个核心模块（`ai-service/evoguard/**/*.py`），
再对争议结论**逐条回源码核实**——本报告中至少 5 处 agent 结论被一手代码推翻或修正，
包括我自己上一轮的两个错误声明（见"纠正"节）。

**评级尺度**：A=生产级（有事务/并发/边界/审计 + 真测试）；B=工程级（可用，有明确缺口）；
C=实验级（能跑但不可靠/只对样例成立）；D=骨架或演示。

**一句话总评**：这是一个**工程严谨度显著高于平均开源项目**的仓库——
六字段执行契约、fail-closed、可回滚快照、前置注册门槛、冻结判定线都很扎实；
但**"评测用的门"比"线上的门"严**，且自进化/线上指标两块存在真缺口，
整体大致是「B+ 工程原型，局部 A，两处 C」。不是生产级产品，也不是玩具。

---

## A 级（生产级，经核实）

| 模块 | 职责 | 关键证据 | 边界 |
|---|---|---|---|
| `evaluate.py` | 成对 A/B 评估，空分母不估 | 深拷贝双侧、case_id/key 去重、训练重叠检查，违约直接 ValueError | 侧效应成本只在回调提供时记 |
| `v3_gate.py` + `test_v3_prereg.py` | 预注册 8 条判定线 | 门槛硬编码 + SHA256 冻结，`:16,:24-31`，缺项不算通过 | passed 是工具链自证，`independent_reproduction=false` |
| `baseline.py` | 多模式对照实验 | C/A/B/D 四组同 seed 配对，失败不吞 | 结论依赖 sim 工具链，不可外推真实业务 |
| `core.py` | 持久化 Store | `_WriteGuardConn` SQL 感知写串行化 + `_ConnPool` 实测数字（24 线程 locked 18→0，rps 342→368），每行 `with conn()` 真关闭 | WAL 仅池开时启用；SQLite 方言绑死 |
| `security.py` | 一级正则扫描 | 六字段 decision/effect/executed 分离（`:709-719`），`_PRE` 前置词保延迟（实测 10KB<1.5ms），每规则标误报 | 默认 `AGENTLAB_SECURITY_MODE=audit`，只记不拦；error 级才 block |
| `execution.py` | 工具执行前总门禁 | `preflight()` 契约→意图→审批顺序固定，`stop` 时 executed 恒 False | `on_missing_goal` 等依赖配置，legacy 模式下默认放行 |
| `service_controls.py` | 鉴权/限流/kill switch | hmac.compare_digest 恒定时间、token 不进日志、数值解析失败不在构造函数吞 | token 未设置时鉴权**关闭**（有 warning，但生产需显式配） |
| `enterprise/audit.py` | 哈希链审计 | 记录链式 prev_hash，能发现删中间行/改字段 | **无密钥且锚点同库**：持 DB 写权者可整链伪造，尾部截断不可检——不是密码学防篡改 |
| `approval.py` | 写操作审批 | 原子消费确认真原子（条件 UPDATE + rowcount） | 跨外部副作用非原子，依赖人工对账 |
| `jwt_rs256.py` | RS256 验签 | 模幂 + 完整 PKCS#1 v1.5 填充（00 01 FF.. 00 + DigestInfo）、compare_digest、kid 精确选、**没有** Bleichenbacher 式"解密成功即通过"错误 | 依赖调用方提供可信 JWKS；discovery 校验 issuer 防劫持 |
| `jwt_hs256.py` | HS256 验签 | alg 白名单、exp/nbf/iss/aud 全查 | 对称密钥管理是部署责任 |

## B 级（工程级，可用但有明确缺口）

| 模块 | 职责 | 关键证据 | 缺口 |
|---|---|---|---|
| `skills.py` | 技能库与晋升回滚 | promote/rollback 事务内追加**不可变快照**，回滚从快照整份写回（不是只改版本号） | 查询索引排序依赖 rowid 兜底，历史数据顺序敏感 |
| `experience.py` | 经验治理与检索 | 默认只返 validated，`include_pending=True` 是**显式**越权通道，rejected 永不返回 | 溯源校验在 `save()` 而非强制读路径 |
| `tool_contract.py` / `tool_registry.py` / `tool_rails.py` / `action_scope.py` | 工具契约/注册/扫描/职责 | 未声明工具在跑检查**之前**就拒（`tool_rails.py:309`），动态注册工具有来源与 scope | rails 默认配置未覆盖全部工具；职责表靠关键词 |
| `autonomous_agent.py` | ReAct 主循环 | preflight 通过才 executed=True（一手核实 `:137-152`），输出侧命中替换观察值（`:272-296`） | legacy 模式下输出侧 fail-open（设计如此，非 bug），需显式 enforce 才 CONTROLLED |
| `agent_watch.py` | 护栏聚合 + ONNX 判官 | 多扫描器聚合，ONNX 判官有明确模型缺失路径 | 二级字符模型在 user_input 上误报高（自建集实测 16/16 全报），默认关是有意决定 |
| `gateway.py` | 模型网关 | 多 provider 抽象、Ollama 原生 think:false 实测 0.5s | mock provider 是显式 `is_mock` 标注，不静默冒充 |
| `sim.py` / `agents.py` / `real_tasks.py` / `bench_adapter.py` | 实验工具链 | 文件头明标"仅内存模拟、不连真实系统" | `/api/run`、`/api/jobs` 底层就是这套；**结论不可外推真实业务** |
| `enterprise/jobs.py` | 持久作业 | 状态机 + 取消/重试入口 | 单 worker 语义，多副本共享队列靠外部 Redis |
| `enterprise/observability.py` | 指标/追踪 | Prometheus 文本输出 | 指标无租户维度隔离校验 |
| `enterprise/resilience.py` | 熔断 | 状态机完整 | HALF_OPEN 并发探测未做单飞（多请求可同时探测） |
| `enterprise/tenancy.py` | 租户上下文 | `require_tenant_filter` 守门 + 每租户配额 | SQLite 靠"每租户一文件"，非真正 RLS；PG 侧由 `pg_store` 补 |
| `redis_backend.py` | 可选 Redis 后端 | 纯标准库 RESP，连接失败**显式抛错不静默降级**（已核），EVAL Lua 原子窗口 + 双进程真压测通过 | RESP 单连接非线程安全、无自动重连 |
| `distributed.py` | 分布式接口 + 默认实现 | Memory/SQLite limiter、Breaker、TenantResolver（无 verifier fail-closed） | 修过自死锁；Redis/PG 是可选层 |
| `pg_store.py` | PG 共享存储 | 复用 core.Store 全部业务方法，`?`→`%s` 有限状态机**正确处理字面量内 ?**（已核，无静默损坏），SET LOCAL 绑租户 + 白名单防注入 | **注释里的 `?` 仍会被替换**（如 `-- what?` → `-- what%s`），当前 schema 无此写法所以未触发；普通角色非表 owner 时 ALTER 会失败（CI 里靠独立库规避） |
| `pg_rls.py` | RLS DDL + SQLite 等价语义 | FORCE RLS + GUC + 未 SET 时 fail-closed；真库 A/B 隔离 passed | 生产实际用 `pg_store` 的迁移脚本，本文件 DDL 仅提供模板 |
| `openai_proxy.py` | OpenAI 兼容透明代理 | 注入输入在转发**之前**拦（实测 opener 调用数=0），tool_calls 逐个 preflight，恒 executed=False（已核） | 不替第三方执行工具——这是边界，不是缺陷，但调用方必须理解 |
| `mcp_proxy.py` | MCP stdio 安全代理 | tools/list 注册 + tools/call 门禁，恶意返回诱导写工具时 executed=0（已核） | 本身无 SSRF/路径面（它不发网络请求），域名防护在 `sandbox_executor.NetworkSandbox` |
| `sandbox_executor.py` | 受限文件/命令/网络沙箱 | realpath 带分隔符前缀防 `/root2` 绕过、命令白名单 shell=False、SSRF 内网拦截、opener 调用前拦（已核） | **符号链接逃逸测试在 Windows 非开发者模式 skip 2 项**——代码逻辑在，但本机未实测 |
| `harmful_request_judge.py` / `persona_jailbreak_judge.py` | 分层越狱判官 | error 单列不静默 allow；双判官并联使英文留出 0.53→0.90（FPR 0） | prompt 工程非训练分类器；中文越狱仍 0.8065 |
| `memory_context.py` | 长程记忆检索 | 只取 validated、不可信标记、有界；BM25 是 `evidence_gate/bm25.py` 的真实现（ Robertson IDF + k1/b），中文 2-gram 补分词 | 跨任务"语义"仍是 n-gram 近似，不是向量语义 |
| `conditional_skill.py` | Skill 沙箱门禁 | schema + 权限双校验，禁止代码字段，拒绝越权声明工具 | 只验结构不验语义正确性 |
| `evaluation` in `skills.py` | 独立留出集 A/B | 同 case/seed 成对、训练重叠拒绝、guard 负迁移单独统计 | 见下方 C 级说明 |
| `app.py` | 主服务入口 | 26 条真路由（含 `/v1/chat/completions`、`/api/react/run`），鉴权/限流/租户/审计/作业全接（**一手核实，非孤岛**） | 评测类模块（evaluate/v3_gate/baseline）没有 HTTP 端点——这是有意的，不是漏接 |

## C 级（实验级，只对样例成立）

| 模块 | 为什么是 C | 证据 |
|---|---|---|
| `skill_synthesis.evaluate_and_promote` | **回滚判据是死代码**：`_detect_regression(ev)` 用**验证阶段**的 `neg_transfer_rate/gain_pp`，而 PROMOTE 时它们必然是 0 和正数，所以 `promoted=True` 后 `rolled_back` 永远为 False（一手核实 `:100-110`）。这轮没有真实线上指标回灌。 | 代码：只在 PROMOTE 分支调 `_detect_regression(ev)`，输入是同一个 evaluation |
| `light_classifier.py` + 训练脚本 | **中文泛化未验证**：我上一轮报的"留出 recall 0.8831"里，742 条留出版**只有 2 条中文**（按源分组 + 全局 70% 切分，中文 474 条全在训练侧）。真实中文泛化率未知；回看中文后30% 是 recall=1.0/FPR=0.15（记忆率，不是泛化）。 | 核实：`k=1731` 落在 `jb_classification` 内部；holdout 中文数=2 |
| `semantic_route.py` / `semantic_guard.py` | 语义层是规则+关键词驱动的近似，v3 模板集通过但自建中文非模板集召回 0.4389 | 盲测产物 `experiments/blind_eval/result_l1.json` |
| `deep_scan.py` / `injection_scorer.py` | 二级字符模型在没见过的业务域误报 33%，默认关 | `docs/detector-evolution.md` 有实测记录 |

## D 级（骨架/演示）

无严格 D 级模块——所有模块都有可运行路径。最接近骨架的是 `evidence_gate/__init__.py`（仅 re-export，无编排）和 `distributed.describe().not_implemented` 里诚实列出的未实现项，但它们本身不是独立模块评级对象。

---

## 我这轮的 5 处纠正（重要，别只记好评）

1. **纠正我自己（上一轮）**：PROD-03 声称"中文盲测 0.8831"是错的——那是英文留出。中文泛化未测。
2. **纠正 subagent**：声称 "app.py 新能力全未上线" 与一手代码矛盾——ROUTES 表里 `/api/react/run`、`/v1/chat/completions`、`/api/skills/synthesize` 都在。
3. **纠正 subagent**：声称 "runtime 恢复期无条件绕过审批" 过于严重——enforce 模式下 `allow_auto=False` 会抛 `E_APPROVAL_REQUIRED`；只有默认 demo/audit 模式自动放行且留 `approval_bypassed` 标记。
4. **纠正 subagent**：声称 "RS256 有 Bleichenbacher 风险" 不成立——实现是签名验证不是解密，且填充与 DigestInfo 全查。
5. **纠正 subagent**：把 SSRF 面归给 mcp_proxy 不对——该模块不发网络请求；防护在 sandbox_executor，已实测。

## 总体结论（我的判断）

**可以达到生产使用的部分**：核心 Store/审计/审批/鉴权/限流/工具前置门禁/RS256 验签/Redis 原子限流——这些有事务、并发、fail-closed、真测试支撑。

**不能当"已解决"的部分**：自进化的线上退化回滚（死代码）、中文泛化能力（未真正留出）、语义层对真实非模板注入的召回（0.44）、v3 的独立复跑（false）。

**最值得先修的一件事**：给 `skill_synthesis` 接真实线上指标回灌，让回滚判据不再是验证指标的自我循环。这比再加一个检测器更有价值。

你可以否。
