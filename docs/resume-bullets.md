# 简历里的项目写法（基于当前代码的真实数字）

> 用法：简历一栏放「精简版」；面试/作品集放「完整版」；最后一节是**不能写的话**，
> 写了会被追问穿。所有数字都能在仓库里查到（文件路径已标）。

---

## 一、精简版（简历里放 3 条，约 4 行）

**EvoGuardLab — Agent 可靠性保障与安全防护系统**（个人项目 · Python · SQLite · 本地 LLM）

- 自建离线检查器：不运行 Agent、不调模型，只读执行轨迹与定义即可定位问题，支持 OTel GenAI / LangSmith / 本地运行库三种输入，内置 31 条轨迹规则 + 20 条静态规则（6 组分类）。
- 设计「成对验证 + 样本充分性」的技能晋升门禁：逐 case/seed 对比前后成功率并计算负迁移率，拦下增益不足或证据不足的候选（真实模型下 127 个候选仅 11 个通过）。
- 在真实模型（Qwen3 4B）上完成 1080 次运行的全量对照：复合故障场景中「严格验证」策略库 53.3%，未验证库 0%；单故障 79.9%。

---

## 二、完整版（作品集 / 面试自述，5–6 条）

**EvoGuardLab · Agent 可靠性保障与安全防护**

一句话：让团队能回答「这个 Agent 现在能不能放心让它自己跑，哪一维没测」。

- **离线检查器（0 依赖，纯标准库）**：定义统一的轨迹模型（Trace/Step），把 OTel GenAI、LangSmith、本地 SQLite 运行记录归一到同一结构，格式差异全部挡在 loader 层；规则层只依赖统一模型，新增格式不改规则。
- **规则库与误报治理**：沉淀 31 条确定性轨迹规则（答案/工具/流程/资源/结构/攻击面 6 组）+ 20 条静态规则；每条强制声明「什么情况下会误报」。用 `confidence` 分级把真实框架扫描的 298 条噪音压到 2 条真线索。
- **不把「零命中」当「通过」**：新增规则适用性统计，缺 token 用量/时间戳/工具白名单时明确判「覆盖有限」并列出跳过的规则；报告每维固定写「测了什么 / 没测什么」。
- **技能晋升门禁**：成对评估（逐 case/seed 比前后）、负迁移率、样本充分性（目标/对照/对照基线成功各 ≥3 个独立任务）、候选正文与验证集指纹、24h 有效期、幂等晋升；不信任汇总列，一切从成对明细重算。
- **可复现基线**：manifest 冻结产物哈希 + 关键代码指纹 + 技能库内容指纹 + 配置哈希；技能库按正文算指纹（不哈希 SQLite 字节）；续跑前校验指纹，拒绝跨基线混跑。
- **安全层**：24 个确定性检测器（间接注入/越权工具/数据外带/凭据/危险命令/输出侧 XSS）+ 最小权限白名单 + 写操作审批；三档模式（off/audit/enforce），默认只检测不改写，保证历史实验可比。纯正则实现，10KB 文本 0.31ms。

**量化结果**（可复现）
- 测试：**233 个**单元测试全绿；离线检查器对 9 个开源框架（OpenAI Agents SDK / Google ADK / AutoGen / smolagents / LangGraph / CrewAI / PydanticAI / Agno 等）做过实测扫描。
- 真实模型对照：270 用例 × 4 模式 = **1080 次运行**（Qwen3 4B / Ollama），单故障 A 0% / B 22.1% / C 82.8% / D 79.9%；复合故障 C 0% / **D 53.3%**。
- 训练与门禁：147 探索用例 → 127 候选 → **11 条严格晋升**（其余 64 拒绝、52 证据不足）。
- **安全与审批**：把「检测建议」与「真实执行后果」拆成六字段契约 + `security_effects`
  执行证据表（修掉「返回 blocked 但正文未改」被当成拦截成功的高估）；可恢复审批状态机
  绑定规范化参数摘要、权限指纹、24h 有效期与一次性消费，5 个验收情形
  （首次写入无审批 / 参数篡改 / 响应丢失 / 拒绝与过期 / 进程重启）全部通过。
- 防御演示：4 类良性攻击，`enforce` 下 **4/4 检测 + 4/4 拦截 + 0 外泄**。
- 性能：安全检查每次运行约 51µs（相对真实模型 627ms/次为 0.008%）。

**技术栈**：Python（标准库为主）、SQLite、OTel GenAI 语义约定、本地 LLM（Ollama/Qwen3）、http.server、单元测试（unittest）

---

## 三、英文版（投外企/实习用）

**EvoGuardLab — Agent Reliability & Security Assurance Platform** (Personal project, Python)

- Built an offline inspector that analyses agent execution traces and definitions **without running the agent or calling any model**: unified a Trace/Step model across OTel GenAI, LangSmith and local SQLite runs, with 31 trace rules + 20 static rules across 6 categories.
- Designed a promotion gate with paired evaluation and sample-sufficiency checks (per-case/seed comparison, negative-transfer rate, ≥3 independent tasks, content & validation-set fingerprints, idempotent promotion); only 11 of 127 candidates passed on a real model.
- Added applicability accounting so "zero findings" is never reported as "pass": the report states which rules could not run and why.
- Implemented a security layer (24 deterministic detectors, least-privilege tool allowlist, write-operation approval, off/audit/enforce modes) with ~51µs overhead per run; a controlled attack simulation shows 4/4 attacks detected and blocked, 0 data leaks in enforce mode.
- Achieved full reproducibility: manifest with artifact/code/library/config fingerprints, content-based skill-library hashing, and cross-baseline mixing refusal.

---

## 四、面试会被追问的 5 个问题（备好答案）

1. **「你这个是玩具吧？」**
   → 承认规模，但给出可验证点：392 个测试、9 个真实框架扫过、真实模型 1080 次运行。
   并主动说局限：数据集是模拟业务环境，不是生产流量。

2. **「53.3% 的复合故障听起来很低。」**
   → 这是**在故障注入下**的成绩，不是线上成功率；对照组 C 是 0%，说明差距来自治理而不是模型。
   同时说明难度组掉到 69.4% 的原因是模型不会多步算术，不是恢复机制。

3. **「你怎么保证不是自己刷分？」**
   → 门禁的判定从成对明细重算，不采信汇总列；候选正文改一个字段旧评估就失效；
   晋升必须绑定评估记录、重复请求幂等。这些都有反例测试。

4. **「安全检查误报怎么办？」**
   → 每条发现都带「可能是误报」说明；默认 audit 只记录不改行为；
   enforce 的代价我实测过：写审批真执行后单故障 -26.5pp、复合 -40pp，所以默认不开。

5. **「这和 LangSmith / Langfuse 有什么区别？」**
   → 它们做观测，我做**判定与门禁**：技能能不能晋升、回滚能不能还原行为、基线能不能复现。
   观测工具给你数据，这个项目给你结论和拦截。

---

## 五、不能写的话（写了会被问穿）

- ❌ 「防止了提示注入」→ 只能写「检测并降低风险」。特征是匹配的，改写/编码可绕过。
- ❌ 「生产级 / 企业级」→ 没有鉴权、限流、多租户、SLA。
- ❌ 「准确率 9x%」→ 没有人工标注集，只有覆盖率，**没有准确率**。
- ❌ 「支持所有 Agent」→ 支持 OTel/LangSmith/本地运行库导入，以及静态扫描常见框架写法。
- ❌ 「沙箱隔离」→ 审批和最小权限是真执行的，**沙箱没有**。

---

## 六、如果要在简历里放一行数字

> 392 个测试全绿 ｜ 31+20 条确定性规则 ｜ 真实模型 1080 次运行 ｜ 严格库 11/127 ｜
> 防御演示按**真实执行后果**计量（enforce 下越权工具、危险命令、参数外带真实阻断；
> 不再用「4/4 拦截」这种把检测建议当拦截成功的说法）

（对应文件：`ai-service/evoguard_inspect/`、`ai-service/evoguard/security.py`、
`experiments/real_model_20261109/`、`experiments/defense_demo/`、`docs/progress.md`）

---

## 七、按简历模板格式（与「Lux」那条对齐）

> 直接可复制。个人项目，所以「团队协作」一栏换成「工程实践」，不虚构协作。

**EvoGuardLab——Agent 可靠性保障与安全防护系统**
· 个人项目（独立开发）
2026-11 至今

内容：

项目概况：独立开发的 Agent 可靠性保障与安全防护系统，面向「Agent 已经跑过、需要判断能不能放心用」的场景，把可靠性检查、技能晋升门禁与攻击面防护整合成一套离线、可复现的评估体系。

技术架构：项目采用 Python（标准库为主）、SQLite、OTel GenAI 语义约定、本地大模型（Ollama / Qwen3 4B）等技术，按「轨迹归一化 → 规则检查（31 条轨迹 + 20 条静态，6 组分类）→ 三维判定（可靠 / 防护 / 可运行）→ 基线冻结」分层设计；安全层独立为确定性检测器 + 工具白名单 + 写操作审批，支持 off / audit / enforce 三档模式。

项目功能：目前已实现离线轨迹检查（不运行 Agent、不调用模型）、静态定义扫描（9 个开源框架实测）、技能晋升门禁（成对验证 + 样本充分性 + 指纹绑定 + 幂等）、完整行为回滚、经验证据溯源、可复现基线（manifest + 内容指纹 + 防混跑），以及攻击面检测与运行时拦截；正在进行真实模型全量对照与误报率验证。

工程实践：全程独立完成需求定义、架构设计、实现与验证；以 233 个单元测试守护行为，先写反例再改实现——「评估后改候选正文」「重复传参静默丢输入」「JSON 参数下漏检 rm -rf」都各自留下回归测试。

业绩：

- 独立实现 Agent 离线检查器，统一 OTel GenAI / LangSmith / 本地运行库三种轨迹格式，沉淀 31 条轨迹规则与 20 条静态规则，并用置信度分级把 9 个开源框架扫描的 298 条噪音压到 2 条真线索。
- 设计并实现技能晋升门禁：逐 case/seed 成对比较、负迁移率、≥3 个独立任务的样本充分性、候选与验证集指纹、24 小时有效期、幂等晋升；真实模型下 127 个候选仅 11 个通过，复合故障场景严格库 53.3%、未验证库 0%。
- 在 Qwen3 4B 上完成 1080 次运行的四组对照（单故障 204 / 复合 30 / 难度组 36），并落地可复现基线：产物哈希 + 代码指纹 + 技能库内容哈希 + 配置哈希，重跑不覆盖历史、续跑拒绝跨基线混跑。
- 实现安全层与防御验证：24 个确定性检测器 + 工具白名单 + 写操作审批，单次运行开销约 51µs；面向间接注入、越权调用、危险命令、输出 XSS 的四类良性攻击演示中，enforce 模式 4/4 拦截、0 数据外泄。
