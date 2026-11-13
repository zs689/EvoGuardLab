# 低分项升级报告（2026-11-12）

> 目标来自用户指令：「低分的地方赶紧升级，去网上找、融合，要达到企业级」。
> 本文件如实记录**做到了什么、没做到什么**。结论先说：
> **工程基线从「原型」升到「可复现、可发布的研究原型」；「企业级」没有达成，
> 也不该靠一次代码提交宣称达成。**

## 一、上一轮评估的低分点

| 维度 | 上轮判断 | 本轮动作 |
|---|---|---|
| 核心语义检测 | ~3/10 | 联网核实一手方法后**重新预注册并实现 v2**，实测（结果见 §三） |
| 真实世界验证 | ~2/10 | 尝试接公开基准；HF 端点不可达，改用许可清楚的本地样本并如实标注局限 |
| 发布就绪度 | clean clone 回归断裂 | 修到干净 clone 可复现，CI 补安装/冒烟/版本矩阵 |
| 服务层 / 企业级 | 无鉴权、无限流、无结构化日志 | 按 OWASP 一手清单补最小基线（见 `docs/service-controls.md`） |
| 元数据 | 个人路径、占位符、版本漂移、许可声明含糊 | 逐项清理（§五） |

## 二、联网核实了什么、融合了什么、没融合什么

一手/权威来源：

- StackOne *Detection Isn't the Hard Part*（全文）：SFE **逐字段**按形状判 pass/drop，
  drop ~45% 字段，FP 1.7% → 0.2%。
- DeFuse arXiv:2609.22510v1（PDF 已下载校验）：max-pooling 文档级误报按
  `1−(1−p)^N` 复合；固定阈值的 F1 只量阈值位置，需长度感知阈值 + 报 AUC。
- Meta Llama Prompt Guard 2（模型卡/文档）：重叠分窗 + 取 max、报 AUC 与
  固定 FPR 预算下的召回。
- *The Guardrail Cries Wolf*（Zenodo 页面摘要）：截断扫描对末尾 payload 8.3%，
  分窗恢复到 75% 但长 benign 误报 6% → 18%，代价必须同报。
- OWASP AI Agent Security Cheat Sheet / Agentic Top 10 2026：最小权限、限流、
  fail-closed、审计日志、stop switch（ASI02/ASI03/ASI08/ASI10）。
- InjecGuard / NotInject（MIT，ACL 2025）：良性但含触发词的**过度防御**探针思路。

**明确没融合**：Prompt Guard 2 的权重（gated Llama 4 Community License，需
transformers）、任何第三方包、任何本地模型服务。项目继续 `dependencies = []`。
**没接到的公开数据**：NotInject / HF eval suite，官方端点 502、镜像 503，
本次不可达；用本地许可清楚的样本 + 1860 条确定性合成负例替代，
**不冒充公开基准成绩**。

## 三、检测能力 v2：真提升，但预注册判定未通过

新预注册 `docs/semantic-injection-v2-plan.md`，新冻结集
`experiments/semantic_injection_v2/frozen_split.json`，**v1 的判定线与结果未动**。
新模块 `ai-service/evoguard/injection_scorer.py`：逐字段过滤 + 重叠分窗取 max +
按窗口数分桶的 FP 预算阈值 + AUC（阈值无关）评价。

| 组 | 留出 AUC | 留出 FP | 末尾 payload 检出 | 过度防御 FP | 四条全过 |
|---|---|---|---|---|---|
| baseline | 0.9917 | 0.000 | **0.000** | **0.1477** | 否 |
| `v2_window_only` | **0.9947** | 0.000 | **1.000** | 0.1477 | 否 |
| `v2_full`（申报组） | 0.9593 | 0.000 | **1.000** | **0.009** | 否 |

**总判定：未通过**。`v2_full` 的 AUC 比 baseline 低 0.032，超过允许的 −0.01。

- 确实修好了一个真实缺陷：`MAX_SCAN_CHARS=8000` 截断让末尾 payload 全漏，
  重叠分窗把它从 0 拉到 1.0，AUC 还不降反升，FP 守住 5% 预算。
- 逐字段过滤把过度防御误报 14.8% → 0.9%，但**全丢结构字段会损失排序质量**。
- 两个机制各解决一半、简单叠加互相拖累。按预注册**没有**放宽判定线、
  **没有**拿 test 调策略、**没有**把未通过改写成通过。
- 逐组判定留在 `result.json` 的 `per_view_checks`，由
  `tests/test_injection_scorer.py`（11 项）守门。

## 四、发布就绪度：clean clone 回归修复

上一轮实测：干净 clone 上 `3 failures + 5 errors + 35 skipped`，而 README 写 791 pass、
CI 只跑这一条。本轮：

- `.gitattributes` 锁 LF，修掉 `core.autocrlf=true` 把 fixture 从 16110 → 16122 字节、
  触发 sha256 守门失败的问题。
- 4 个 AgentDojo 产物测试、中文样本测试、源文件 sha 校验改为显式 `skipUnless`；
  `test_reproducibility` 只跳过 3 个缺语料的脚本、其余 4 个字节一致性照验；
  `evoguard eval` 缺语料时跳过 4 项并打印前置条件，不再抛 traceback。
- 修 `chinese_injections_v1.py` 的真实 bug：离线缓存缺失抛的是 `SystemExit`
  （不是 `Exception`），原 `except Exception` 接不住。
- CI 补 `pip install -e .`、`evoguard config`、隔离输出的 demo 冒烟、
  Python 3.10/3.11/3.13 矩阵与一个 Windows 组合、`evoguard eval` 子集。
- README 中英写明「复现分层」：814 是语料齐全时的全量套件，clean clone 上
  依赖语料的用例如实显示 skipped，不静默计为通过。
- **两轮实测 clean clone**：服务层落地前 `Ran 802 tests ... OK (skipped=41)`；
  全部改动提交后 **`Ran 814 tests ... OK (skipped=41)`**。
  同一份干净 clone 上 `evoguard config` / `eval` / `defense_demo` 全部冒烟通过，
  `pyproject.toml` 元数据（0.6.0 / `dependencies=[]` / CLI 入口）复核一致。

## 五、元数据与合规

- 清掉 `C:/Users/zs/...`、`E:/agent/learn/...`、`F:\Qwen3`、`/f/ollama` 等
  开发机路径；文档命令改用相对路径与占位符。
- `pyproject.toml` 删掉 `OWNER` 占位 URL、README 删掉 `<this-repo>` 占位。
- 新建 `tests/fixtures/external/README.md`：说明 12 条 InjecAgent 夹具的
  MIT 许可与来源，并精确化 README「语料正文不入库」的例外。
- 版本统一由服务层任务处理（见 `docs/service-controls.md`）。

## 六、企业级：结论是「没有达成」

> 本节是**上一轮**的结论。本轮（见 §八）把工程形态补齐了，但
> 「检测没过自己定的线」和「隔离只在应用层」两条仍然成立 —— 所以
> 整体结论没有变成「已达到企业级」，只是从「缺形态」变成「形态有、两处硬缺口」。

补上的是**工程基线**（鉴权、限流、结构化日志/request-id、配置校验、
stop switch、Dockerfile、CI 可复现），对应 OWASP 若干控制项。
`ai-service/evoguard/service_controls.py` 独立可测（12 条守门全绿），
接入 `app.py` 的统一请求入口后 `test_api_smoke` 14 条仍全绿、
默认行为向后兼容；版本改为从 `pyproject.toml` 单一来源读取。
`docs/service-controls.md` 明确列出默认关闭鉴权的理由与风险：
**默认态下路由仍全开放，只靠 127.0.0.1 回环兜底**。

仍然缺、且**不是加代码就能自证**的：

1. **核心检测能力本身没达标** —— v2 预注册判定未通过；企业级不会部署一个
   在自己留出集上没过线的检测器。
2. 真实业务流量、真实身份体系与多租户隔离（当前单 SQLite、无 tenant 字段）。
3. TLS / 反向代理 / WSGI、集中式日志与指标、持久化审计表与 schema 迁移。
4. SLA、容量、限流策略的压测、故障演练、值班与事故流程。
5. 公开基准未接通（网络不可达），跨域泛化仍未在独立公开语料上验证。

所以准确说法是：**「可复现、可发布、工程基线齐备的研究原型」**，
不是「企业级安全产品」。把这层说清楚，比写一个假的「已达到企业级」更值钱。

## 七、复现与最终验收

```bash
# 本地（语料齐全）
python -m unittest discover -s tests -q                    # 814 tests, OK
python scripts/run_semantic_injection_v2.py --verify
python scripts/run_semantic_injection_v2.py --all          # 需要 data/external/
evoguard demo
evoguard config

# 干净 clone（无 data/external、无 _sandbox）
python -m unittest discover -s tests -q                    # 814 tests, OK (skipped=41)
evoguard config
evoguard eval                                              # 跑 2 项、跳过 4 项并说明前置
```

最终验收记录：本地 814 全绿；干净 clone 814 全绿（41 项语料相关显式 skip）；
`tests/test_api_smoke`(14) / `test_service_controls`(12) / `test_injection_scorer`(11)
单独复跑合计 37 项 OK；v2 冻结集 `integrity_sha256` 校验通过；
主仓库 `git status` 干净。

## 八、本轮：从「缺形态」到「形态齐、两处硬缺口」

上一轮补的是工程基线（鉴权/限流/日志），这一轮补的是**整体形态**。
判据还是那条：能自己验一遍的才叫真实现，验不了的留清晰接口。

### 8.1 新增（都能自己验）

| 能力 | 落地物 | 怎么验的 |
|---|---|---|
| 多租户 | `enterprise/tenancy.py` | 租户解析/配额/未知租户收紧/SQL 守门，单测 + 真进程 |
| 可观测性 | `enterprise/observability.py` | Prometheus 文本、日志管道、span、采样白名单 |
| 审计与迁移 | `enterprise/audit.py` | v1→v3 幂等迁移；**逐行哈希链**，改一行/删一行都能发现 |
| 韧性 | `enterprise/resilience.py` | 熔断三态、超时预算、SLO（分母 0 返回 `None`） |
| 传输安全 | `enterprise/tls_proxy.py` + `tests/test_tls_handshake.py` | 真 TLS1.3 握手、明文客户端被拒、min_version 挡住旧协议 |
| 进程级验收 | `scorecard.probe_live` | 起真进程发真 HTTP：401/200、request-id 回显、租户进上下文、审计落库 |
| 压测底数 | `scripts/loadtest.py` | 1000 请求 / 20 并发：487 / 708 rps，可用性 100% |
| 故障演练 | `scripts/drill.py` | 5 条：注入→断言→**撤销→断言恢复**，5/5 |
| 抓取与告警 | `deploy/prometheus.yml` + `deploy/alerts.yml` | 7 条告警规则，指标名与代码逐个对齐 |
| 自评与守门 | `evoguard scorecard --min` | CI 每次推送跑，低于线就红 |

### 8.2 过程中修掉的真 bug（都是自己写的）

1. `scorecard.ROOT` 少退一级（指到 `ai-service` 而不是仓库根），检测维度会全 0。
2. 8 个探针里 **7 个把 `(ok, label, evidence)` 写反了** —— 分数照算、
   检查项名字全变成 `True`。现在 `_pack` 会显式拒绝反序元组。
3. 新的 Prometheus `h_metrics` **盖掉了**旧 JSON `h_metrics`，前端
   `/api/metrics`（`frontend/index.html:760`）会 500。改为 JSON 留在原路径，
   Prometheus 走 `/metrics` 与 `/api/metrics/prometheus`。
4. `evoguard serve --port 8770` 必崩：`main()` 只认裸位置参数，
   `int("--port")` 抛 `ValueError`。补 `_parse_port()`。
5. 压测脚本卡死：给子进程 `stderr=PIPE` 又没人读，访问日志写满缓冲后
   子进程堵在 `write` 上 —— 表现为「请求超时」，看着像性能问题。改写文件。
6. 审计库配置错误时**静默降级**成不落库。改为 `/api/health` 返回
   `audit_error` + stderr 告警 —— 审计没了必须让人知道。

### 8.3 自评分数（可复现）

`evoguard scorecard` → **84.8 / 100**，明细见 `docs/enterprise-scorecard.md`。

八个维度里七个接近满分，**唯一低分是检测能力 2.0/5**，因为它诚实地反映了
「预注册 v2 没过线」。这正是分数该起作用的地方：如果这里也是 5/5，
那这个 scorecard 就没在测东西。

剩余缺口（就两条，且都不打算靠加代码糊过去）：

1. **检测器 v2 未过预注册判定线** —— 要另立 v3 预注册，不是改口径。
2. **数据库层行级隔离** —— sqlite 没有 RLS；`require_tenant_filter()` 是
   应用层字符串守门，能挡常见错误，但不是隔离。要真隔离得换 PostgreSQL。

### 8.4 复现

```bash
evoguard scorecard                       # 84.8/100 + 缺口清单
python scripts/loadtest.py --requests 1000 --workers 20
python scripts/drill.py                  # 5/5
python -m unittest discover -s tests -q  # 861 tests, OK
```

修复记录：本轮把测试从 814 提到 **861**（企业模块 34 + 防篡改 5 + 真握手 3 +
进程级探针 2 等），全绿。

### 8.5 最终复验（提交后）

```
本地         : Ran 861 tests ... OK
干净 clone    : Ran 861 tests ... OK (skipped=41)
干净 clone 打分: 84.8 / 100（与主仓库逐字节一致）
守门线        : evoguard scorecard --min 80 → 通过
CLI          : evoguard config / scorecard / serve --port 均可用
工作树        : git status 干净
```

`skipped=41` 全部是依赖 `data/external/` 语料的用例（该目录按许可与体积
不入库），不是失败。干净 clone 上分数与主仓库**完全一致**，说明打分依赖的
是仓库里的产物，而不是这台机器上的任何东西。

## 九、独立审查整改（2026-11-12 ~ 13）

依据 `EvoGuardLab_企业与高性能客观审查_2026-11-12.json`（revision `793c6d1`，
27 项发现：3 P0 / 20 P1 / 4 P2）。**审查明确「仓库自评分数不计入结论」** ——
上一轮 §八 的 84.8/100 是自评，不再当进度用。

逐项台账：`docs/review-remediation/README.md`；修前证据 `before.md`；
修后证据 `after.md`。状态汇总 **✅5 / 🟡12 / ⬜10**。

### 真做完的（有可复验产物）

`EG-002` 企业模式启动期 fail-closed、`EG-004` 独立 wheel 在源码树外可用、
`EG-007` 审计归档锚点、`EG-013` 探针拆分与指标口径、`EG-023` CI 逐项硬门槛。
另有 12 项部分完成、10 项明确没做，逐条写明原因与接口位置，不假装实现。

### 关键负结果（必须写在前面）

改后 `POST /api/run` 8 并发 P95 **3500.09 ms**，独立审查改前测的是
**3534.7 ms** —— 同一量级。把 schema DDL 移出热路径、加索引**没有解决写瓶颈**，
瓶颈在同步执行与 SQLite 写锁（EG-009 / EG-010 仍未完成）。两组参数不完全一致，
不做百分比对比；要下结论得在改前提交上用同一脚本重跑，本轮没做。

### 验收数字

```
python scripts/review_checks.py --gate    14/14 pass, GATE_EXIT=0
python scripts/enterprise_smoke.py        9/9
python scripts/wheel_smoke.py             10/10（独立 venv、源码树外空目录）
python scripts/check_docker_context.py    5/5（规则级；镜像层未实测）
python scripts/check_wheel_manifest.py    PASS（对 wheel 制品本身）
python -m unittest tests.test_api_contract  8/8
python scripts/drill.py                  5/5
python -m unittest discover -s tests -q Ran 874 tests ... OK
```

### 最终复验（干净 clone，提交 `db57159` + `6b214b4` 之后）

```
干净 clone : Ran 874 tests ... OK (skipped=41)
           : scripts/review_checks.py --gate -> 14/14 pass
           : evoguard config / scorecard --min 80 -> PASS
           : pip wheel . -> 独立 venv 安装 -> check_wheel_manifest PASS
           : wheel_smoke 10/10（serve 六个路由全 200）
主仓库     : 工作树干净，874 tests OK
```

详见 `docs/review-remediation/after.md` §六。

### 明确没验的

Docker daemon 不可达（镜像层未 build）、GitHub CI 未实跑（全部本地工作、
未创建远程仓库/推送/发布）、真实浏览器交互未自动化、真实企业流量与
SSO/多副本未验、v3 预注册未做。

## 十、第二轮整改（2026-11-13，同日续）

第一轮收尾时明确「下一轮该动的是 v3 预注册和持久作业」。这一轮照做。
逐项台账 `docs/review-remediation/README.md`，第二轮证据 `after.md` §七，
性能对比 `perf-before-after.md`。

### 做了什么

- 持久作业（EG-009）：`enterprise/jobs.py` + `/api/jobs*`，幂等键/租约/
  unknown 不自动重放；`tests/test_jobs.py` 10/10、`jobs_smoke.py` 11/11。
  8 并发提交 P95 **14.1 ms**，完成 P95 1785 ms —— 请求线程不再被卡住。
- 审计跨进程（EG-008）：`BEGIN IMMEDIATE` + 流式校验，两独立进程并发写 2/2。
- v3 预注册（EG-017）：8 条判定线冻结（`v3_gate.py` + sha256），
  `tests/test_v3_prereg.py` 7/7；**实验未执行**，v2 负结果归档不动。
- 详情读取预算、前端企业令牌会话、数据分类 + 合成敏感标记、缓存 LRU/TTL。

### 关键负结果（同负载对比，不是拿不同参数凑的）

git worktree 检出 `b39e462` 与当前工作树用**同一份脚本、同一参数、
各自全新隔离库**各跑 3 轮 × 4 场景 × 24 请求 / 8 并发：

| 场景 | 改前 P95 | 改后 P95 | availability |
|---|---|---|---|
| write_run | 3216.41 ms | 3446.68 ms | 1.0 → 1.0 |
| read_health | 26.44 ms | 23.13 ms | 1.0 → 1.0 |
| read_runs | 14.60 ms | 23.26 ms | 1.0 → 1.0 |

写路由吞吐 5.5 → 5.35 rps。**没有实质性能改善**。
中途加 WAL 曾让 P95「降到 1521 ms」，但那是每轮 3–4 个 500 快速返回
把分布拉低（`availability=0.875`），traceback 是
`attempt to write a readonly database`；按证据撤回 WAL 后 24/24 成功。

### 最终数字

```
Ran 903 tests ... OK
review_checks.py --gate   19/19 pass
jobs_smoke 11/11 / audit_multiprocess 2/2 / v3_prereg 7/7
semantic_cache 7/7 / data_governance 3/3
```

台账 ✅5/🟡12/⬜10 → **✅6/🟡17/⬜4**。仍未解决：检测 v2 未过线、
隔离不是 RLS、写路径性能、单进程、多副本与真实业务流量；
Docker 镜像层、GitHub CI、真实浏览器与 Linux wheel 未实测。
