# 独立标注与三种真值（Q01 / Q03）

对应推进计划 §7。代码：`ai-service/evoguard_inspect/independent_labels.py`；
产物：`experiments/external_labeling_v1/{registry.json,protocol.md}`；
验收：`tests/test_permission_and_ci.py`（Q01/Q03 部分）。

## 一、三种真值必须分开存（Q01）

| 维度 | 含义 | 为什么不能合并 |
|---|---|---|
| `execution_state` | clean / recovered / failed（来自注入动作） | 任务失败 ≠ 任意轨迹规则都该能检出 |
| `observable_fault` | 注入的故障类型 | 「故障存在」和「轨迹里看得到」是两件事 |
| `business_outcome` | 最终 success 与否 | 轨迹自洽但业务错时，需要外部业务判据 |

另外两条口径，写进协议避免误判：

- `recovered` 轨迹里**确实存在过的攻击发现不一定是假阳性**，不能仅因最终成功就否定安全规则。
- 内部自洽但结论错的漏检（静默篡改 / 陈旧数据 / 计划顺序）需要外部政策版本或流程定义；
  能用外部判据补的才扩规则，无法观察的记为能力边界，不用更宽泛的正则硬刷召回。

## 二、当前状态：全部待复核（如实）

| 项 | 值 |
|---|---|
| 自生成样本 | 37 |
| 可授权外部轨迹 | **0** |
| 独立人工标注 | **0** |
| 待复核 `pending_independent_review` | 37 |
| Cohen's kappa | `null`（没有第二位标注者，零分母不写 0） |

`registry.json` 里每条都带 `label_basis=execution_ground_truth`、
`independent_annotation=null`、`review_status=pending_independent_review`。
**绝不把自生成标签改称人工标签**。外部轨迹清单必须自带 `source` 与
`authorization`，缺任一项直接被拒，不计入「可授权使用」。

## 三、要变成真准确率还差什么（Q03）

1. 至少 30 条可授权使用的外部轨迹，覆盖正常 / 恢复成功 / 失败 / 攻击尝试。
2. 独立标注者按任务约束标注，**不读取规则实现**。
3. 第二位复核者核争议案例；两人标同一批算 Cohen's kappa。
4. 分歧标 `needs_discussion`，**先讨论再定，不直接取多数**，也不静默丢弃。
5. 开发与测试集按任务家族划分；测试集冻结后不得根据失败题继续调规则。

## 四、报告需要同时展示的内容

按规则、严重级别、入口、任务家族报告 TP / FP / FN、样本数、适用率与不可判定率。
原始样本、发现与标注定位到同一条证据；私密字段导出前脱敏；零分母继续 `null`。

## 五、权限评测的独立性（Q02，另见权限专项）

`eval_labeled.py` 把轨迹**实际用到的工具**填入 `allowed_tools`，只为消除结构评测的
无配置噪声，却会让未经授权但已执行的工具被自动视为允许。所以：

- 权限专项 `scripts/eval_permission.py` 只用事先冻结的
  `tests/fixtures/labeled/policies.json`（与 `security.AGENT_TOOLS` 一致，有测试断言）。
- 没有独立策略的样本标 `unjudgeable_permission`，**不计入分母也不静默通过**。
- 结构评测与权限专项分开报告，不混成一个数。

**实测反例**：`trace_attack.json` 执行了白名单外的 `http.post` / `shell.exec`。
反推白名单检出越权 **0** 条，冻结策略检出 **2** 条 —— 不分离口径，越权永远测不出来。

## 六、不能怎么用

- ❌ 把 `execution_ground_truth` 写成「人工标注」。
- ❌ 把 37 条自生成样本写成外部真实业务轨迹。
- ❌ 把两档 recall/precision 写成真实业务准确率或误报率 0。
- ✅ 可以写：已建立独立标注协议、待复核清单与权限专项口径；外部轨迹与独立标注尚未到位。

## 七、复核命令

```bash
cd E:\agent\EvoGuardLab
python scripts/eval_permission.py
python scripts/build_external_label_registry.py
python -m unittest tests.test_permission_and_ci -q
```
