# 对标 NeMo Guardrails 的整改记录（2026-11-11）

这份文件回答一个问题：**那份评审报告列的问题，哪些真的修了、证据在哪、哪些没修。**

评审来源：`EvoGuardLab_对标NeMoGuardrails_不足与整改方案_2026-11-11.docx`
（评审对象是 2026-11-11 09:54 的代码快照；本文件描述的是整改之后的快照）。

规矩还是那一条：**说"修了"就得有可执行的证据**。每条都给出「改在哪 / 靠什么证明 /
还剩什么问题」。没有证据的写「没修」。

---

## 一、总览

| 编号 | 问题 | 状态 | 证据 |
|---|---|---|---|
| R01 | 执行路径没调用契约与意图门禁 | **已修** | `tests/test_execution_entry.py`（15 项，含调用次数=0） |
| R02 | 同一语义多字段无统一解释（金额别名） | **已修** | `tests/test_tool_contract.py`＋`execution` 归一化 |
| R03 | 全局规范化把不同参数变成同一摘要 | **已修** | `tests/test_approval.py::test_canonical_no_false_equivalence` |
| R04 | 候选版本存储但消费时不比对 | **已修** | `tests/test_approval_hardening.py`（10 项） |
| R05 | 一次性消费不是原子状态迁移 | **已修** | 同上，两个并发消费者只有一个成功 |
| R06 | 意图判定只看到参数前 400 字符 | **已修** | `tests/test_execution_entry.py::TestIntentInputCoverage` |
| R07 | 召回与误报使用不同统计单位 | **已修** | `experiments/action_scope_v2`（逐 case 为主口径） |
| R08 | 中文语料形状断言失败 | **已修** | `tests/test_chinese_corpus.py`（16 项） |
| R09 | 基线指纹漏掉主要安全模块 | **已修** | `tests/test_baseline_fingerprint.py`（9 项） |
| — | 从 NeMo 融合「按工具声明双向护栏」 | **已补做** | `tool_rails.py`＋`tests/test_tool_rails.py`（22 项） |
| — | 评审 §9「能力表」 | **已补做** | `docs/capability-matrix.md` |
| — | 人工标注 | **成本已降到 60 次点击**，仍待他本人执行 | `scripts/human_labels.py` |
| — | 官方 AgentDojo 排行榜 | **未做** | 需要官方模型/API |
| — | 真实业务开发集上的阈值标定 | **未做** | 需要真实业务数据 |

全量回归：**748 项通过**（`python -m unittest discover -s tests -q`）。

---

## 二、R01 执行路径没有统一接入（最重要的一条）

### 问题复述

`runtime.Ctx.call` 依次做「工具白名单 → guard_tool_args → sim.validate_args → 缓存 →
可选 ApprovalGate → sim.call_tool」。`tool_contract.authorize` 和
`security.guard_tool_intent` 都写好了、也都有单测，**但真实调用路径一次都没调**。
评审把两者替成恒定 deny，工具照样执行了一次。

这是「模块通过 ≠ 真实生效」的典型：单测测的是模块，不是接线。

### 改法

新增 `ai-service/evoguard/execution.py`：

- `build_config()` 生成**生效配置**并校验，返回的对象可以导出（`evoguard config`）；
- `preflight()` 按顺序跑 契约 → 意图，任一层 stop 就返回 `stop=True`；
- `call_id()` 生成稳定 id，轨迹与审计能对上。

`Ctx.call` 在 `sim.validate_args` 之后、**缓存与审批之前**调用 `preflight`。
`stop` 时记录轨迹、写 `call_stopped`、`raise ToolError`，**不碰 `sim.call_tool`**。

### 配置分两档

| 档位 | 行为 |
|---|---|
| `legacy`（默认） | 保持历史实验行为；缺口写进 `gaps` 字段但不禁用运行 |
| `controlled` | 写副作用工具没有审批后端 / 身份未知 / 检查器缺失 → **启动期就抛错** |

`AGENTLAB_EXEC_MODE` 打错字（比如 `enforce!`）**不是回退到默认**，而是配置错误 ——
否则打错一个字符就悄悄降级成不防护。

### 证据

`tests/test_execution_entry.py`：
- `test_contract_deny_means_zero_tool_calls`：契约 deny → `sim.call_tool` 调用 **0** 次
- `test_intent_deny_means_zero_tool_calls`：意图 deny → **0** 次
- `test_normal_call_executes_exactly_once`：正常 → 恰好 1 次
- `test_write_tool_without_approval_backend_refuses_to_start`：controlled 档拒绝启动

### 还剩什么

- `legacy` 档仍然是默认档，所以「开箱即用」的情况下写操作没有强制审批后端。
  这是为了不破坏历史实验可比性而做的妥协，**报告里必须写明当前跑的是哪一档**。
- 意图门禁依赖本地模型（默认关）。模型不可用时 controlled 档转 review，legacy 档不阻断。

---

## 三、R02 / R03 输入契约与摘要

### R02：同一语义多个字段

原来 `tool_contract._find` 只返回**第一个非空别名**。传 `amount=1, total=20000`，
金额检查只看 `amount=1`（合规），下游读 `total=20000`（越界）。

现在的规则（`tool_contract.TOOL_SCHEMAS`）：

1. 每个工具声明 `required` / `fields` / `aliases`；
2. 未知字段 → 拒绝；
3. 别名组内取值不一致 → `alias_conflict` 拒绝，**并且不替你选一个**（冲突时干脆不归一化）；
4. 归一化只做一次（`fold_aliases`），之后的授权、审批摘要、真实执行**消费同一个对象**。

`authorize()` 返回 `verified_args`，`runtime.Ctx.call` 用它替换原参数并留一条轨迹。

**注意一个取舍**：`order.settle` 的 canonical 金额名沿用 `total`（sim 里本来就叫这个），
所以 `amount` 会被折成 `total`，而不是反过来。改下游字段名风险更大。

### R03：摘要的假等价

原来 `approval.canonical` 把所有数字转 float、四舍五入到 6 位，所有字符串 strip。后果：

| 输入 A | 输入 B | 原来 | 现在 |
|---|---|---|---|
| `9007199254740992` | `9007199254740993` | 同摘要 | **不同** |
| `1.0000001` | `1.0000002` | 同摘要 | **不同** |
| `"file"` | `" file "` | 同摘要 | **不同** |
| `504`（金额） | `504.0`（金额） | 同摘要 | **相同**（这是要保留的） |

改法：`canonical(value, key)` 按**字段语义**处理 —— 金额字段定标到分（`Decimal`），
其它整数保整数精度、其它浮点用 `repr`、字符串**不再 strip**。
摘要里写入 `canonical-v2` 版本号，老审批自然失效（宁可要求重新审批，不可误复用）。

**方向性判断**：把"假等价"改成"假不等价"是安全的 —— 后者只会让人多批一次，
前者会让改了参数的调用复用旧批准。

---

## 四、R04 / R05 审批的版本绑定与原子消费

### R04

审批表里早就存了 `candidate_version` 和 `idempotency_key`，但 `require` / `consume`
根本不比对。用 v1 批的票能消费 v2 的调用。

现在 `_version_mismatch()` 逐项比对：候选版本、幂等键、工具 schema 版本。
规则是**缺值不当通配符**：审批里是 NULL、调用方给了值 → 不一致。
版本变了 → 旧待批票作废、要求重新审批，不静默继承。

### R05

原来是「先读 approved，再在另一段连接里 `UPDATE ... WHERE id=?`」，两个线程都能读到
approved，于是都返回 `consumed`。

现在 `_consume_atomic()` 用条件更新：
`UPDATE ... WHERE id=? AND status='approved' AND consumed_at IS NULL`，
**只有 `rowcount == 1` 才算抢到**。`approve` / `reject` 也改成条件迁移。

证据：`tests/test_approval_hardening.py` 用 `threading.Barrier` 把竞争窗口撑开，
断言两个并发消费者里**恰好一个** `consumed`，另一个拿到明确的拒绝原因。

### 还剩什么

- SQLite 单机场景下条件更新足够；换成跨进程/跨库时要靠事务隔离级别，本仓库没有实现。
- 审批与业务成功是两件事：审批消费成功 ≠ 结算成功。`SettlementLedger` 的幂等键管后者。

---

## 五、R06 意图判定看不到参数尾部

原来 `" %s(%s)" % (tool, body[:400])` —— 长参数尾部的收款人、金额、目标资源
对判定器完全不可见，它看着一段无害前缀说 allow。

现在 `security.intent_payload()` 按**字段语义优先级**挑：
动作类（action/operation/method…）→ 接收方（to/account/url/email…）→ 金额 →
资源 ID / 路径。预算 2000 字符，放不下的字段记进 `omitted_fields`。

关键一条：**`omitted_fields` 非空时不许返回 allow**，一律转 `review`。
看不全就不能说"一致"。

另外，判定器抛异常以前会落到 `review`（这条原来就有），现在在 `preflight` 层也兜了一层：
`fn()` 抛错 → controlled 档 stop（review），不让异常穿到调用方变成"没判定=放行"。

---

## 六、R07 评测口径

### 问题

`eval_action_scope` 的召回按**话术分组 any-hit**（一句话术下任一工具命中就算命中），
误报按**逐 case** 算。两个单位不同，却拿去和"逐 case 的语义召回 5%"比较，
于是 80.6% 被读成"提升十六倍"。

### 改法

`evaluate()` 现在同时输出：

- **逐 case 召回**（主指标）
- 逐话术任一命中率（参考）
- 逐工具宏平均（参考）
- 最差工具家族（参考）

报告里写明"三个数字分母不同，不能混着比"。

**一个诚实的发现**：在这份数据上，逐 case 与逐话术**恰好相等**，而且所有工具的召回也相等。
原因不是统计没做，是 **InjecAgent 把同一句攻击话术原样放进 17 个工具返回里** ——
同一句话要么全中要么全不中。所以"最差工具家族"在这份数据上没有区分度，
报告里明写了这一点，避免被误读成"某类工具更难"。

### 测试集被跑了三次

| 轮次 | 改了什么 | 开发集 | 测试集 |
|---|---|---|---|
| v1 | 初版 | 0.677 | 0.806 |
| v2 | 补否定式过滤 | 0.677 | 0.806 |
| v3 | 补中文动词与主语 | 0.710 | 0.839 |

三次的改动来源都不是这份英文数据（v2 来自中文文档误报，v3 来自自建中文集漏检），
但既然跑了三次，就不再是"一尘不染的 held-out"。报告里按这个事实写，不写成"只验一次"。

---

## 七、R08 中文语料

### 断言为什么失败

`zh_jb/B2_ctf_slot` 不含中文。回查上游：这条在 `ammo_matrix.json` 里**本来就写成英文**
（CTF 包装那条），不是槽位没填、也不是取错字段。

处理方式（评审给了两条路，这里取第三条并说明理由）：

- 不删断言，不删样本；
- 给每条加 `lang` 字段，由内容计算；
- 测试改成「`lang` 必须存在，且非中文占比必须 < 2%」—— 把"混进了一条英文"变成
  一个**被显式记录的事实**，而不是一个被隐藏的失败。
- 模板类材料（JailBench 的 `{decoded_query}`）**必须实例化**后再入集，
  用无害问题填充并标 `instantiated: true`。

### 语料扩到 100+

原来的 31 条不够。现在三类分开：

| 类别 | 条数 | 来源 | 许可 |
|---|---|---|---|
| `direct_jailbreak` | 34 | pojia-breaker（31）+ JailBench 包装模板（3） | MIT / Apache-2.0 |
| `harmful_request` | 108 | JailBench seed | Apache-2.0 |
| `indirect_injection` | 120 | **本项目自建** | 本项目 |
| `indirect_clean` | 120 | 本项目自建（对照） | 本项目 |
| `benign` | 31 | 本项目自建 | 本项目 |

合计 413 条，攻击 262 条，**不同攻击文本 262 条**，不同手法 47 种。

### 三个任务的数字（不合并）

| 类别 | 入口 | 逐 case 召回 | 说明 |
|---|---|---|---|
| direct_jailbreak | user_input / L1 | **0.029**（1/34） | 正则在中文上是死路 |
| harmful_request | user_input / L1 | **0.000**（0/108） | 任务不匹配，不是缺陷 |
| indirect_injection | tool_output / action_scope | **0.917**（110/120） | **样本内自测**，误报 0/120 |

最后一行必须加限定：规则就是在这 120 条上定稿的，只能说明"这类写法现在能认出来"，
不能当泛化能力。泛化证据是英文 InjecAgent 的复测（v3 从 0.806 涨到 0.839）。

---

## 八、R09 基线指纹

原来 `baseline.CODE_FILES` 只有 11 个文件，`security.py` / `approval.py` /
`tool_contract.py` / `semantic_guard.py` / `action_scope.py` / `scan_pipeline.py` /
`code_guard.py` **一个都不在**——"把检测器改松一半"不会让 manifest 变化。

现在：

- `CODE_FILES` 补到 21 个，含全部防护模块与新增的 `execution.py`；
- 新增 `CONFIG_FILES`（阈值、inspect 配置）；
- 新增 `SECURITY_SWITCHES`：11 个已知安全开关的生效值进 `config_hash`，
  并进 `default_config()`（连同检测器清单、提示词指纹）；
- **缺文件不再静默跳过**：项目根缺必需模块 → `BaselineMismatch`；
  合成根（没有 `ai-service/evoguard` 目录）记 `code_coverage.project_root=false`；
- manifest 里新增 `code_coverage`：列了几个、哈希了几个、缺了哪些。

证据不是"断言清单里有 8 个名字"，而是造真实差异：

- 改 `action_scope.py` 一个字节 → 指纹变；
- 改 `thresholds_v1.json` → 指纹变；
- 设 `AGENTLAB_SECURITY_INTENT=1` → `config_hash` 变；
- 删 `approval.py` → 拒绝冻结。

---

## 九、没做的部分（不粉饰）

1. **真实人工双标注 + Cohen's kappa**：仍然是代理标注。模型间一致性不等于人工一致性。
2. **官方 AgentDojo 排行榜**：需要官方模型与 API；本地只跑了适配器链路。
3. **真实业务开发集上的阈值标定**：`config/thresholds_v2_calibrated.json` 是候选，
   没有在真实业务分布上标定过，**未启用**。
4. **拼音/拆字类中文攻击**：120 条里 10 条漏检全部属于这一类。要修得做拼音归一化，
   属于另一个方向的工程量，本轮没做。
5. **PromptGuard 2 等价物**：需要 HF gated 权重 + torch，本仓库刻意保持零依赖，没加。
6. **跨进程/跨库审批原子性**：只在单机 SQLite 上验证。
7. **P2 阶段的受控并发与预算**：API 层仍是单用户回环假设，没有做请求级隔离。

---

## 十、怎么复核这份记录

```bash
python -m unittest discover -s tests -q          # 726 项
python scripts/eval_action_scope.py               # experiments/action_scope_v2
python scripts/import_chinese_corpus.py           # 需要网络；产物不入库
python scripts/eval_chinese_real.py               # experiments/chinese_corpus_v2
evoguard config                                   # 本次生效配置
```

评审原始证据（反例脚本与 JSON）在 `E:\agent\_review_work_20261111\`，
不在仓库里 —— 它含临时路径，不属于项目产物。

---

## 十一、从 NeMo 到底融合了什么（不只"读过"）

评审说「最值得吸收的是每个工具在调用前和返回后都能绑定明确规则，配置缺项能够尽早报错，
判定失败有独立状态」。这三条之前只做到了一部分（返回侧有 `action_scope`，
配置校验散在 `execution`，判定失败只有 review 一种状态）。现在补了 `tool_rails.py`。

### 融合的对照表

| NeMo 的机制 | 本仓库的实现 | 一致性 |
|---|---|---|
| `rails.tool_call.per_tool` / `rails.tool_result.per_tool`：同一工具两个方向各挂检查 | `tool_rails.DEFAULT_RAILS = {tool: {call: [...], result: [...]}}` | 一致 |
| 配置缺项加载期失败，报错点名 flow 与 tool | `load()` 抛 `RailsConfigError`，消息含工具名、方向、写错的名字 | 一致 |
| `@tool_call_validation`：未声明工具在判官之前挡住 | `check_call` 返回 `not_declared`，**一个扫描器都不跑** | 一致（有调用次数断言） |
| `$argument=`：只把指定字段送判 | `{"scanner": ..., "fields": [...]}`，仅在 call 方向可用 | 一致（result 方向配 fields 会报错） |
| `parse_tool_safety_verdict`：只认 safe/unsafe，其它算坏输出 | `status ∈ {safe, unsafe, error, not_declared}` | 一致 |
| 用 LLM 判官做调用前检查 | 用**结构契约 + 严格参数模型**替代 | **刻意不同**：结构检查更严、更便宜、可解释 |

### 刻意不同的那一条，理由写清楚

NeMo 用 LLM 判官看 tool call 参数，是因为它没有结构化契约 —— 它不知道「这个 Agent
能不能碰这个订单号」。本仓库有 `tool_contract`：身份、资源 ID 格式、域名白名单、
金额上限、别名冲突，全是确定性检查，`0` 次模型调用。再叠一层 LLM 看同样的东西，
只会让延迟翻倍而召回不变。

所以默认配置里，调用侧只给**结构契约管不到**的两个工具挂检查：
`shell.exec`（任意命令）和 `http.send`（自由文本载荷）。返回侧则全量挂 —— 那一侧
没有结构可依，只能看内容。

这不是"少做"，是"不重复做"。真要开，改 `DEFAULT_RAILS` 就行，机制是通的。

### 证据

`tests/test_tool_rails.py`（22 项）里最关键的 5 条：

1. `test_unknown_scanner_names_tool_flow_and_name` —— 配错名字，报错必须带三样东西；
2. `test_undeclared_tool_blocks_before_any_scanner_runs` —— 断言扫描器调用列表为空；
3. `test_broken_checker_is_error_not_safe` —— 检查器崩了，状态是 `error` 不是 `safe`；
4. `test_only_named_field_reaches_the_scanner` —— `fields=` 真的少送了数据；
5. `test_controlled_mode_turns_checker_error_into_review` —— 受控档下坏判定会停调用。

### 没融合的部分

- NeMo 的 `$variant=`（同一个 prompt 复用到多个工具）没做：本仓库的检查器是代码不是
  prompt，天然可复用，不需要这层间接。
- NeMo 的 `IORails` 运行时没接：那是一个完整框架，接进来就得背上它的依赖与模型配置，
  与本仓库「零依赖可复现」的取向冲突。评审也说「有收益后再引入框架依赖」。
