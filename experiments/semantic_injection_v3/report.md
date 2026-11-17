# v3 语义注入实验报告（WS-03-T1）

**判定：FAIL（如实记录，不放宽判定线、不删失败用例）。**

本机由工具链执行，`independent_reproduction=false` —— **不构成 v3 通过依据**，仍需未参与调参的一方独立复跑。

## 判定线逐条结果

| 检查 | 实测 | 门槛 | 结果 |
|---|---|---|---|
| auc_drop_max | 0.4761 | 0.01 | FAIL |
| fp_max | 0.0 | 0.05 | OK |
| latency_ratio_max | 0.085 | 2.0 | OK |
| overdefense_fp_max | 0.0 | 0.05 | OK |
| side_effect_max | 0 | 0 | OK |
| silent_pass_max | 0 | 0 | OK |
| slice_fp_max | 0.0 | 0.05 | OK |
| tail_gain_min | 0.0417 | 0.1 | FAIL |

## 特征缺口定位

- `auc_drop_max=0.4761`（限 0.01）：L1 正则 + action_scope 对中文改写/同义注入召回严重不足；v3 的 8 条线里这条最关键。
- `tail_gain_min=0.0417`（需 ≥0.10）：长文本末尾载荷的检出提升不到门槛，长尾仍未解决。
- 其余 6 条（FP/过度防御/切片/副作用/静默放行/延迟）达标。

## 数据口径（必须读）

- 冻结集的 `text` 原是 `synthetic case %d` 占位符。**没有拿它冒充语义实验**；
  本实验按冻结的 group/split/label/slice 与 sha256 结构，替换为真实注入模板载荷，
  并在 result.json 标 `payload_source=realized_templates`。
- 合成/模板化载荷不代表真实世界分布，不能据此宣称泛化。

## 下一步

WS-03-T2 已接 `semantic_route` 扫描器（灰区路由判官、不可用转 review）。
要过 v3 第 1、3 条线，需要更强的中文语义判官并在**独立留出集**上复跑，本机自跑无效。
