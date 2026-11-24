# 条件决策技能协议（阶段 D / E01–E03）

代码：`ai-service/evoguard/conditional_skill.py`；验收：`tests/test_conditional_skill.py`（21 项）。

## 一、E01 受限声明式结构

从「遇到某故障采用某恢复策略」推进到「按条件选择证据 / 按声明顺序组合规则」。
继续加 `retry` / `replay` 别名解决不了规则表之外的能力缺口。

技能**只能**包含这八项：

| 字段 | 作用 |
|---|---|
| `id` / `name` | 标识 |
| `applies_when` | 适用条件 |
| `required_evidence` | 必需证据（缺了就走失败回退） |
| `allowed_tools` | 允许工具（必须是冻结策略的子集） |
| `decision_params` | 确定性参数，只允许 `evidence_selector` / `order_by` / `max_steps` / `max_evidence` |
| `stop_when` | 停止条件 |
| `fallback` | 失败回退 |
| `schema_version` | 可选，必须为 `conditional-skill-v1` |

`evidence_selector` 只认 `max_version`（按文档版本号选证据）、
`declared_order`（按声明的业务顺序组合）、`first`。

**边界由外部执行器强制，不是靠模型自觉**：

- 禁止字段：`code` / `python` / `exec` / `eval` / `lambda` / `script` /
  `source_code` / `evaluation_criteria` / `approval_status` / `system_policy` /
  `set_allowed_tools` / `override_policy` / `subprocess` / `imports`（嵌套也查）。
- 禁止代码特征字符串：`import ` / `def ` / `exec(` / `eval(` / `__` / `os.` /
  `sys.` / `subprocess` / `lambda ` / `compile(`。**先不生成任意 Python 代码**。
- 权限：`allowed_tools ⊆ 冻结策略.allowed_tools`，且不得声明写工具
  （写操作必须走审批门禁）。执行前会**再校验一次**，策略收紧后旧技能不能绕过。
  写工具的判定不只靠策略的 `write_tools`：再核一遍 `tool_contract.DEFAULT_CONTRACTS`
  的副作用声明（`write` / `external` / `arbitrary`），否则策略漏配一个字段
  就等于给技能开了写或外发的口子。
- 候选不是对象（`None` / 列表 / 字符串 / 数字）时直接抛 `SkillRejected`，
  不抛 `AttributeError` —— 技能来自模型，形态不可控，校验层必须干净地拒绝。
- 技能不得修改评测标准、审批状态、系统策略或允许工具集合。

模型提出候选 → `accept(skill, policy)` 校验 → `execute(skill, context, policy)`
只做确定性证据选择，**不调用工具、不执行代码**；调用方拿 `tools_allowed`
自己决定后续动作。

刻意不复用旧的 `skills.py`：旧技能是字符串策略名，两者并存，330 项旧测试不受影响。

## 二、E02 独立家族上的增量（本轮只做最小骨架）

`paired_check(before, after)`：同 task/seed 前后配对，逐 case 比较。

- 计算 `gain`（业务成功率差）与 `negative_transfer_rate`（原本成功变失败的比例）。
- **业务收益不能抵消未授权操作**：只要 after 出现 `unauthorized` / `unsafe`，
  直接 `REJECT`，哪怕 gain=1.0。
- 负迁移超阈值、没有稳定收益 → `REJECT`，**允许发布负结果**，不要求每轮百分比上涨。
- 没有可配对用例 → `NEEDS_REVIEW`，`gain` / 负迁移率给 `None`（零分母不写 0）。

同时检查正常 guard 与安全 guard：当前骨架只接逐 case 的 `unauthorized` / `unsafe`
标记，还没接真实 guard 家族数据集。

## 三、E03 经验与检索的适用范围

现有经验已绑定来源，但检索仍主要依赖结构条件。计划要求先记录经验适用的
工具契约、数据版本、权限范围、有效期和撤销状态，再做结构过滤 + BM25；
只有语义改写确实造成召回不足时才加 embedding，相似度不能替代可信性。
**本轮未做**，列为下一阶段。

## 四、公平消融（§10）与指标冻结

C 与 D 不只差一层验证：技能数量、匹配规则、状态过滤、链覆盖都不同，
所以 C/D 差距只能作为系统级对照，不能单独证明验证门禁的因果收益。

| 实验 | 唯一变化 |
|---|---|
| 验证门禁消融 | 候选池 / 匹配器 / 任务 / seed / 容量固定，只改候选是否通过独立验证 |
| 匹配消融 | 技能内容 / 状态 / 数量固定，只改宽松或严格匹配 |
| 容量对照 | 来源与任务分布固定，只改固定数量候选子集 |
| 条件技能消融 | 工具 / 模型 / 故障 / 匹配固定，只改是否启用声明式决策字段 |

manifest 需登记威胁模型版本、攻击集与正常集内容指纹、独立授权策略、
安全模式、包裹、自动审批、有效工具契约与输出落点。
**历史真实模型 metrics 未完整登记这些安全开关时，只能说明旧记录中的模型与技能条件，
不能事后补猜成某种安全模式。**

同时报告业务与防护：计划任务数、框架失败数、可评数、业务成功数、安全完成数、
待审批数、攻击目标达成数、正常任务误阻断数；关键差异保留逐 case 与分母；
框架失败和不可判定既不伪装成成功，也不从完成率里消失。

## 五、已做 / 未做

已做（21 项测试）：声明式 schema + 禁止字段/代码特征 + 权限子集与写工具禁止 +
执行前二次校验 + 三种确定性选择器 + 失败回退 + 配对验证骨架。

后续补做（同一轮，协议不变）：

- **E02 落地**：`scripts/conditional_skill_eval.py` 建 train / dev / validation / test
  四组家族（task_id 与 seed 全程不重叠），同 task/seed 前后配对，
  同时检查正常 guard 与安全 guard；业务收益不能抵消未授权操作。
  validation gain 0.167、test gain 0.25；`first` 选择器被 REJECT 并发布负结果；
  越权候选 REJECT。产物 `experiments/e02_v1/`，17 项测试。
- **E03 落地**：`ai-service/evoguard/experience_scope.py` 记录工具契约版本、
  数据版本、权限范围、有效期、撤销状态，结构过滤先于 BM25（纯标准库）。
  反例：文本最像查询但已撤销的记录，若忽略撤销会排第一，结构过滤后不进候选。
  产物 `experiments/e03_v1/`，同批 17 项测试。
- **§10 公平消融**：`scripts/ablation_v1.py` 六组消融 / 13 配置 / 八项指标，
  见 [docs/ablation-protocol.md](docs/ablation-protocol.md)。

未做（如实）：
- E02 在**真实模型 / 线上流量**上的四组家族验证与不确定性范围；当前是确定性构造集，
  不给置信区间，不把同一道题多次调用当扩大样本。
- E03 的 embedding 评估：只有语义改写确实造成召回不足时才加，本轮未加。
- 公平消融中的回滚证据、1080 run 历史评测重跑、2×2 消融、
  30 攻击 + 30 正常扩样。
- 云端 LoRA、完整多 Agent、向量数据库、双后端迁移 —— 按计划**暂缓**。
- 没有重跑 1080 run 真实模型评测；没有重训模型；冻结 manifest 未改。

## 六、复核命令

```bash
cd <repo-root>
python -m unittest tests.test_conditional_skill -q
```
