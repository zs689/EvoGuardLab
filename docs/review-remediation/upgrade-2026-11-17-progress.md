# 升级方案执行进度（2026-11-17）

> 依据 `E:/agent/EvoGuardLab_升级改造方案_2026-11-17.json` 严格推进。
> 每条按「做到哪 / 证据 / 未完成原因」如实登记，不删减、不假称完成。

## 总览

| WS | 优先级 | 状态 | 提交 |
|---|---|---|---|
| WS-01 发布前脱敏 | P0 | ✅ 完成（3/3） | a35b48a（重写历史后 hash 变化） |
| WS-02 真实多轮 ReAct | P0 | ✅ 完成（3/3） | 5b04df7 |
| WS-03 语义闭环 | P1 | ✅ 完成（3/3，实验**未过线**如实记录） | dcf77f6 |
| WS-04 自进化 | P1 | ⚠️ T1 完成，T2 未做 | 5867052 |
| WS-05 evidence-gate 整合 | P1 | ✅ 完成（2/2） | 49d56dd |
| WS-06 分布式底座 | P2 | ✅ Redis 真压测 + RLS 双进程隔离通过；真 PG 无环境未跑 | 1dcd427 / 52def35 / 本次 |

## 全局约束遵守情况

| 约束 | 遵守 |
|---|---|
| 核心 evoguard/ 零第三方运行时依赖 | ✅ 新模块只用标准库；ONNX/torch 都在独立 venv |
| 不放宽预注册门禁、不删失败用例 | ✅ v3 判定线 sha256 未变；FAIL 如实写 result.json；预注册测试改为校验指纹与诚实标记，保留「不许冒充独立复跑」 |
| 六字段契约、executed==0 才算拦住 | ✅ ReAct 引擎全程六字段；对抗测试判据是 executed；修了 executed=True + effect=call_stopped 自相矛盾的 bug |
| 每完成一个 WS 跑全量回归 | ✅ 每轮都跑 unittest + gate，见下方数字 |

## WS-01（完成）

- T1 路径脱敏：非归档目录 `git grep` 命中 0；
- T2 历史邮箱重写：139 个提交，`git log --format=%ae | sort -u` 无真实手机号；
  因仓库**无 remote**，重写安全；建了 `pre-push-hygiene-20261117` 标签可回退；
- T3 关掉 8 条 sqlite ResourceWarning：`unclosed database` 计数 0。

## WS-02（完成）

- T1 `autonomous_agent.py`：模型自己决定工具与参数，每步强制走 `execution.preflight`
  + `tool_rails`；max_steps + 死循环熔断；无 executor 时绝不编造结果。
- T2 `FORCED_SKILL` 改为 `Ctx.forced_skill` 实例状态；全局仅向后兼容；并发隔离测试。
- T3 20 条真实多轮攻击（间接注入/多轮漂移/返回投毒）：
  **脚本化 20/20、真 qwen3:4b 20/20、0 error、0 bypass**；
  正向任务 `kb.search` 可执行（证明不是一律 deny）。
- 过程中修了 3 个真 bug（契约源没接注册表导致假绿、effect 与 executed 矛盾、越界 IndexError），
  没靠放宽测试掩盖。

## WS-03（完成，但实验**未过线**）

- T1 v3 实验真跑：`result.json` + `report.md`。判定 **FAIL，如实记录**：
  `auc_drop_max=0.4761`（限 0.01）、`tail_gain_min=0.0417`（需 0.10）；其余 6 条达标。
  标 `independent_reproduction=false`——本机自跑**不构成 v3 通过依据**。
  冻结集 text 原是 synthetic 占位符，按冻结结构换成真实模板载荷并标注。
- T2 `semantic_route` 扫描器：L1 命中 BLOCK、灰区路由判官、判官不可用/超时转 review
  （第 7 条线 silent_pass=0）。
- T3 `fetch_pi_model.py` 下载+sha256 到项目 `.cache/models/pi`（gitignored）；
  判官不再依赖父目录；`.dockerignore` 排除后 build context 从 709 MiB 回到 2.5 MiB。

## WS-04（完成）

- T1 ✅ `trajectory_diagnose.py`：通用根因诊断，不认工具名；新领域工具也能诊断。
- T2 ✅ `skill_synthesis.py` 串成完整闭环：诊断→propose→沙箱 accept→
  独立留出集 A/B（含 guard 负迁移）→ promote/rollback。新领域
  file/http/sql 混合工具链验证；错误 Skill 被 schema 拒绝、负迁移被 REJECT、
  训练/验证重叠强制拒绝。过程中补了 propose 老格式缺的 5 个声明式字段
  （是格式适配，不是放宽门禁）。

## WS-05（完成）

- T1 六个模块迁入 `evoguard/evidence_gate/`；`llm.py` 不迁（统一 gateway）；
  wheel 登记；补了 `<w:outlineLvl>` 大纲识别。
- T2 `evidence_grounding` 输出侧扫描器：正确数字放行、错误数字 error、无引用 warn。
  修了「payload 整包转文本导致篡改数字被证据原文洗白」的真 bug。

## WS-06（Redis 真压测通过；PG/RLS 受环境限制）

已实现并真实验证（零依赖）：

- 基础层：RateLimiter 协议 + MemoryLimiter + SQLiteLimiter、
  CircuitBreakerState（修了自死锁）、TenantResolver、WebhookApprovalNotifier
  （HMAC 幂等回执）。
- Redis 后端：`redis_backend.py` 用**纯标准库 socket 手搓 RESP2**（不装 redis-py，
  守住 `dependencies=[]`）：RedisLimiter（Lua 原子滑动窗口）、
  RedisCircuitBreaker、RedisJobQueue（LPOP+ZSET 租约抢占）。
- JWT：`jwt_hs256.py` 标准库 HMAC 验签，alg=none/算法混淆/篡改/exp/nbf/iss/aud
  全部 fail-closed。
- **真双副本压测**（`scripts/distributed_stress.py`，自起自停临时 redis-server
  5.0.9 + multiprocessing）：跨副本限流 10+10=20 不倍增；40 个作业 A21/B19
  恰好各消费一次、0 重复 0 丢失；A 进程写熔断 B 进程可见。全通过，产物
  `probes/distributed_stress.json`。

**PG/RLS 本机可交付部分（已做）**：`pg_rls.py` 提供可部署到 PG14+ 的
RLS_DDL（三张表 ENABLE+**FORCE** ROW LEVEL SECURITY，GUC
`app.current_tenant` 隔离，未 SET 时 fail-closed），以及
`TenantScopedStore` 用 SQLite 强制 tenant_id 过滤做等价隔离语义。
`scripts/rls_stress.py` 双进程压测：物理 2 行、A/B 各只见自己 1 行、
跨租户读返回 null，产物 `probes/rls_stress.json`（ok=true）。

**仍未做（如实）**：真 PostgreSQL 上的 RLS 行为、OIDC discovery / RS256
（需 cryptography）、Docker Compose 全套压测——本机无 postgres 二进制、
`docker info` rc=1（daemon 未起、Docker Desktop 未安装）、无 psql 客户端。
不假称跑过真 PG。修过 SQLiteLimiter 两个真 bug（:memory: 每次新建连接导致
no such table、with 不关闭连接导致泄漏/SIGTERM）与熔断自死锁。

## 验收数字

最终：1071 tests OK / 29-29 gate / build-context PASS / wheel manifest PASS /
真 Redis 双进程压测 passed / 租户隔离双进程压测 passed。

## 诚实边界

1. 提交顺序上 WS-05 早于 WS-04-T1，但两者依赖（WS-01/WS-02）都已满足，功能无影响。
2. v3 实验 FAIL 是**真实结果**，不是「没调好」；要过线需更强中文语义判官 + 独立复跑。
3. 真 Ollama 对抗 20/20 依赖本机 qwen3:4b；换模型需重测。
4. WS-06 的 Redis 后端与真双副本压测已完成；PG/RLS 因本机
   （无 postgres 二进制、无 dockerd、Docker Desktop 未安装）无法起真服务，
   提供可部署的 RLS SQL 与租户隔离语义验证，不假称跑过真 PG。

---

# 第二轮：INT-01 ~ INT-05（2026-11-17 晚）

依据 `EvoGuardLab_下一轮核心补齐方案_2026-11-17.json`。

| WS | 状态 | 证据 |
|---|---|---|
| INT-01 主路径总装接线 | ✅ 完成 | app.py `_build_backends()` 按 env 切 Redis/JWT；`/api/react/run`、`/api/skills/synthesize` 上线；CLI `react`/`synth` 可跑；`evidence_grounding` 进 DEFAULT_RAILS；`test_app_integration_ws.py` 8 项**真启 HTTP server** |
| INT-02 发布卫生 | ✅ 完成 | `-W error::ResourceWarning` 全量 0 警告；历史邮箱二次清洗 0 命中；工作区干净 |
| INT-03 v3 过线 | ✅ T1 PASS / T2 已做（负结果） | 8 条线全 OK（滑动窗口 + 判官 + 3 条真实 L1 缺口）；盲测：中文自建集 0.4389、中文越狱 0.0645、InjecAgent 0.8867 |
| INT-04 沙箱 | ⚠️ T1 完成；长程记忆未接 | `sandbox_executor.py` + 13 项对抗测试；跨任务记忆注入未进 ReAct 上下文 |
| INT-05 真 PG + 多副本 | ✅ **本机实跑通过** | 真 PG（容器）RLS passed=true；compose 5 容器全 Up，ready=200 limiter=RedisLimiter，鉴权三态正确 |

## INT-03：v3 PASS 与盲测负结果必须一起看

v3 的 8 条线是在**模板化载荷**上过的（`payload_source=realized_templates`）。
独立来源复核：

| 数据集 | 来源 | n | 召回 |
|---|---|---|---|
| chinese_selfbuilt | 项目自建 | 413 | 0.4389 |
| chinese_jailbreak | 项目自建 | 62 | 0.0645 |
| injecagent_public | 公开数据集 | 2108 | 0.9032 |

判官本身正常（英文注入 p=0.95 判 True），对中文越狱判 False ——
qwen3-4b 判的是「是否在对 AI 下指令」，越狱集大量是「请求协助绕过防护」。
**不是调用失败，是判据目标不同**；要提升需另设判官。

## INT-05 实跑结果（本机 Docker 引擎可用后补做）

我此前判断「Docker Desktop 未安装」是**错的**：它装在
`%LOCALAPPDATA%\Programs\DockerDesktop`，当时只是引擎未就绪。

真 PostgreSQL（postgres:16-alpine 容器）：
- 首次用 `postgres` 超级用户测 RLS 得到「全部可见」——**这是假结论**：
  superuser 与 BYPASSRLS 角色永远绕过 RLS，`FORCE` 只约束表 owner。
- 改为创建普通角色 `evoguard_app` 连接后：A 见 1 行 / B 见 1 行 /
  无 GUC 见 0 行，`passed=true`。`pg_rls.RLS_DDL` 注释里的错误说法已改。

compose 拓扑（2 副本 + redis + postgres + nginx，5 容器全部 Up）：
- `/api/ready` 200，`limiter=RedisLimiter`、`auth_mode=jwt_hs256`、
  checks(db/audit/redis/backends) 全 True、errors 空；
- 鉴权三态：无 token 401 / demo-token 200 / 伪造 401；
- 突发请求触发 429 —— 跨副本共享 Redis 配额确实生效。
- 产物 `probes/compose_e2e.json`（passed=true）、`probes/pg_rls_live.json`。

实跑中修的真问题：
1. `init-rls.sql` 在表未创建时执行导致 PG 容器 exit 3 →
   加 `RLS_DDL_IF_EXISTS`（幂等条件版）。
2. 容器内服务绑 `127.0.0.1` 导致 nginx 502 → compose 设 `AGENTLAB_BIND=0.0.0.0`。
3. Dockerfile 缺 `config/` 与 `frontend/` → 企业模式 fail-closed 启动失败。

## 仍未做（如实）

1. ~~INT-04 跨任务长程记忆注入~~ 已完成（提交 96393f5）：
   `memory_context.py` 只取 validated 经验，中文 2-gram 相关性排序，
   注入块标不可信；ReAct 与 /api/react/run、CLI react 都接了，
   `memory` 审计字段随结果返回。
2. 符号链接逃逸测试在 Windows 非开发者模式 skip（2 项），
   已用不依赖 symlink 的前缀规则测试补位。
