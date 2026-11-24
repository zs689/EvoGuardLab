# 按维度覆盖与 CI 发布策略（Q04 / Q05）

对应推进计划 §8。代码：`ai-service/evoguard_inspect/assurance.py`（维度覆盖）、
`ai-service/evoguard_inspect/ci_policy.py`（发布策略）、
`ai-service/evoguard_inspect/cli.py`（`--policy` 分支）。
默认策略：`tests/fixtures/ci_policy.json`。
验收：`tests/test_permission_and_ci.py`（Q04/Q05 部分）+ 原有 `tests/test_assurance.py` 8 项。

## 一、Q04：不再拿一个 `cov_ok` 压所有维度

旧实现：`cov_ok = runnable_rules == total_rules`，一个布尔同时喂给可靠、防护两维。
任一条规则因缺字段没跑，所有维度一起变成「覆盖有限」；反过来，安全缺白名单
也可能被别的维度掩盖。

现在每个维度自己算：

| 维度 | 适用规则 / 观测 | 必需配置 |
|---|---|---|
| reliability | `answer/tool/flow/resource/structure` 组的规则 | 无（各规则自己的条件表） |
| security | `security` 组规则 + **白名单配置事实** | `config:allowed_tools` |
| operability | 模型步 / 工具步 / 延迟 / 报错率四类观测 | 无（只看本批轨迹） |

每维报告 `applicable_rules` / `checked_rules` / `skipped_rules` / `skip_reasons` /
`observations` / `coverage_ok`，并在 `assurance.md` 里逐维展示。

**关键行为**：安全缺白名单只让 security 维度「覆盖有限」，不牵连正常的可靠性判定；
补上白名单只修好 security（测试里对比两种配置守着）。可运行性明确标注
「只是本批轨迹观测，不代表线上 SLA / 并发 / 限流」。

总判定仍取三维最差：通过 / 有风险 / 不通过 / 覆盖有限 / 无法判定，
不合成没有校准依据的单一分数。

## 二、Q05：报告判定与流水线阻断分开保存

旧实现：保障报告「有风险 / 覆盖不足」不一定导致非零退出，
使用者容易把退出码 0 当成全面安全。

现在加 `--policy <json>`：

```bash
cd ai-service
python -m evoguard_inspect.cli \
  --trace ../tests/fixtures/trace_attack.json \
  --assurance --policy ../tests/fixtures/ci_policy.json \
  --out ../experiments/assurance_ci_v1
```

产物分文件：

- `assurance.json` / `assurance.md`：**报告判定**（`verdict`），不因策略改变。
- `ci_decision.json`：`report_verdict` / `ci_action` / `block_pipeline` /
  `exit_code` / `reasons` / `policy_fingerprint`。

`ci_action` 三档：`pass` / `review` / `block`。`review` 退出码为 0，
但**明确记为需要人工处置，不等同通过**；`block` 退出码 3。

### 默认发布策略（对齐计划 §8 表格）

| 输入或状态 | 默认动作 |
|---|---|
| 报告不通过 / 存在 error 级发现 | 阻断 |
| 关键安全规则不可评 | 阻断（要求补配置或人工复核），不静默通过 |
| 覆盖有限 / 无法判定 | 阻断（策略默认不允许） |
| 只有低风险提示 | 复核，保留处置原因 |
| 输入解析失败或被截断 | 阻断，列出未检查范围 |
| 三维证据齐全且无问题 | 只对**本次配置和样本**给通过结论 |

策略字段：`allow_verdicts` / `allow_limited` / `allow_unknown` / `review_on_risk` /
`block_on_input_load_failure` / `require_security_coverage` /
`require_all_dimension_coverage` / `allow_missing_config`。
策略改动会改变 `policy_fingerprint`，便于审计。

**策略文件本身不可信时先阻断**：字段类型不对（`allow_verdicts: null`、
`allow_limited: "false"`、`require_all_dimension_coverage: "yes"`、`version: "1"`）
按更严格的一侧取值并直接 `block`，问题逐条写进 `policy_problems`；
`ci_decision.json` 里新增该字段便于定位。理由：策略写错不该变成「悄悄放行」，
旧实现在 `allow_verdicts: null` 上会抛 `TypeError`。
同理，报告里**没有** `dimension_coverage`（或格式非法）时也阻断——
连覆盖信息都没有，就无从确认关键安全规则是否可评，不能当成没问题。

**一个刻意补的漏洞**：`coverage_ok=False` 却列不出具体缺失项时，
旧逻辑会因为「空列表全都在允许清单里」而放行复核；现在只有
「缺失项明确、逐项在允许清单、且没有规则被跳过」才允许复核，否则阻断。

### 阈值管理

模型报错率 10% / 20% 等阈值仍是工程经验值（`assurance._operability`），
**尚未登记版本与适用场景**。应先记录开发集分布与误报案例再冻结阈值；
不在最终测试集上反复调阈值追求通过。这一条仍列为未完成。

## 三、复核命令

```bash
cd <repo-root>
python -m unittest tests.test_assurance -q
python -m unittest tests.test_permission_and_ci -q
python -m evoguard_inspect.cli --help | grep policy   # 在 ai-service 目录下
```

## 四、仍未完成

- 阈值版本登记与开发集分布记录（§8「阈值管理」）。
- 「存在未授权执行或未批准写入 → 关联调用前证据与副作用记录」：
  `ci_policy` 目前只按报告判定与覆盖决策，还没直接读 `security_effects` 的
  执行后果证据；这属于阶段 B 审批闭环与阶段 C 的衔接，未接通。
- 外部轨迹与独立标注（Q03）仍未到位，见 `docs/external-labeling-protocol.md`。
