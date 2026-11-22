# 自主学习深耕与安全闭环 执行进度（2026-11-22 晚）

对应方案：`EvoGuardLab_自主学习深耕与安全闭环方案_2026-11-22.json`

原则：未达标/未完成的项一律如实标注。

| Workstream | 状态 | 产物 |
|---|---|---|
| EVO-DEEP-01 规约冲突 + 微工具合成 | **完成** | `skill_synthesis._slot_key/active_invariants`、`tool_synthesis.py`、2 个测试文件 |
| EVO-DEEP-02 在线学习闭环 | **完成** | `ReActAgent(auto_learn=)`、`tests/test_online_learning_loop.py` |
| DET-DEEP-01 越狱召回 + L2 限速 | **T1 部分（召回无法在不误报前提下提升）；T2 未完成** | `pub_jailbreak_layered.json`（含泄漏排除与诊断） |
| ARC-DEEP-01 状态封装后拆分 | **T1 完成；T2 未执行** | — |

## EVO-DEEP-01-T1：规约冲突消解（完成）

每条 `env_invariant` 新增 `slot_key`（如 `pagination.start_index` /
`unit.scale` / `timezone.suffix` / `coupling.required_sync`）。`refine_skill`
发现**同槽位但规则值不同**时，把旧规约标记 `superseded_by=<新版本>`，
`active_invariants()` 只返回未退役的规约；旧条目原文仍留在版本快照里可回滚。
基准注入也已改用 `active_invariants`，避免把互相矛盾的旧规约一起喂给模型。

顺带修了一个让冲突检测形同虚设的 bug：pagination 的兜底规则把「从 1 开始」
写死了，1-based → 0-based 两种环境会提取出**同一句话**，同槽位判定永远不触发。
现在从证据里的 `start at (\d+)` 取实际值。

`tests/test_skill_drift.py` 4 条：同槽位稳定、反转使旧规约退役、快照保留旧规则、
不同槽位不误退役。

## EVO-DEEP-01-T2：受控微工具合成（完成）

`tool_synthesis.py` 三道闸：
1. **AST 白名单**：只允许 `re/json/math/datetime/hashlib`，禁 `open/eval/exec/
   compile/__import__/getattr` 等，禁 `.system/.popen/__class__/__subclasses__/
   __globals__` 等属性，禁 class/async/with/global；
2. **沙箱单测**：≥3 条正反例，在受限 builtins 下跑（`__import__` 也替换成白名单版）；
3. **只读上架**：`from_json_schema(scope="read_only", source="synthesized")`
   并登记参数模型。

过程中发现两个会**静默拒绝**新工具的接线缺陷（不修则合成工具永远跑不起来）：
- `registry.register()` 会用简化契约覆盖 `from_json_schema` 生成的结构契约，
  导致 preflight 判「没有登记参数模型」；
- `execution.preflight()` 固定读全局 `tool_rails.load_default()`，动态工具不在
  其中就判「未声明」。已加可选 `rails=` 注入（不传时行为完全不变）。

`tests/test_tool_synthesis.py` 6 条：合法注册并跑通执行通道、8 类恶意源码
（open / os.system / __import__ / subprocess / eval / class 逃逸 / exec / with open）
全部拒绝且不在注册表残留、测试数不足与测试失败均拒绝注册。

## EVO-DEEP-02：在线学习闭环（完成）

`ReActAgent(auto_learn=True, learn_task_type=...)`：
- 开头按 task_type 检索已学候选技能，只注入 `active_invariants`，并用
  `<learned_skill>` 包裹成**不可信经验**（不洗成系统事实）；
- 收尾判断「先失败、后修复成功」的成对轨迹，自动 `synthesize`（首次）或
  `refine_skill`（已有），过信息增益门禁后入库；
- 失败判定覆盖三种形态：门禁拒绝、executor 返回 `ok=False`、非零 `returncode`；
- 默认关闭，`out` 结构与既有行为完全不变（有测试锁定）。

`tests/test_online_learning_loop.py` 6 条：第 1 次自动学到东西并落库、第 2 次
自动注入（断言规约真的进了提示）、无成对轨迹不学习、关闭时不改返回结构、
停用后不再注入、退化判定能识别成功率骤降。

## DET-DEEP-01-T1：越狱召回（部分，如实说未达标）

先发现一个**严重的数据泄漏**：`train_light_classifier.py` 的训练集就包含
`jailbreak_classification/prompts.jsonl`——直接评测等于拿训练集考自己。
已改为复用训练脚本自身的 `stratified_split`，只用其 **holdout**（200 条，
100 攻击 + 100 良性，`train_overlap_excluded: true`）。

结果：

| 口径 | recall | fpr | p95 |
|---|---|---|---|
| L1_only | 0.02 | 0.00 | 0.71ms |
| L1 + light（默认阈值） | **0.94** | **0.46** | 6.32ms |

召回确实从 0.04 提升到 0.94，但 **fpr 0.46 远超 0.05 的验收线**。
进一步诊断：calib 集上 light 的 recall≈0.99 同时 fpr≈0.50，等于「全都拦」；
误报集中在英文新闻/摘要类长文本（p≈0.89）。我验证过用真余弦相似度
（补上权重向量模长）重算，fpr 仍是 0.495 —— 说明**不是长度归一化问题，
而是英文特征/权重的偏差**，后处理救不了。已在产物里写明：要用 light 覆盖
英文直接越狱，必须用平衡的英文良性语料重训并重新校准阈值。

## DET-DEEP-01-T2：令牌桶限速（未完成）

已把 L2 判官调用改为流式落盘 + `as_completed` + 单次 20s 超时
（旧实现用 `ex.map` 按序取结果，一条卡住整批已完成的也落不了盘，
且默认 60s×重试让进程挂十几分钟）。缓存已完整覆盖 InjecAgent 的 62 条唯一文本。

**未做**：令牌桶 `--rps` 与 429 指数退避未实现，全量 `pub_bench_layered.json`
仍未产出（`L2_unavailable` 未清零）。不伪造完整结果。

## ARC-DEEP-01：T1 完成，T2 未执行

T1：三轮 95% 实证与进度文档已入库。T2（先封装 `RuntimeState` 再拆 runtime.py）
本轮未动，`runtime.py` 仍 1320 行。上一轮的教训（隐式全局状态导致
`test_composite_needs_chain` 回归）已经写进方案，但真做需要一次专门的重构窗口。
