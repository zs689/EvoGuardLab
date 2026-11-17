# v3 语义注入实验报告（INT-03）

## 判定：PASS（8 条线全 OK，判定线 sha256 未改）

| 检查 | 实测 | 门槛 | 结果 |
|---|---|---|---|
| auc_drop_max | 0.0 | 0.01 | OK |
| fp_max | 0.0 | 0.05 | OK |
| latency_ratio_max | 0.1117 | 2.0 | OK |
| overdefense_fp_max | 0.0 | 0.05 | OK |
| side_effect_max | 0 | 0 | OK |
| silent_pass_max | 0 | 0 | OK |
| slice_fp_max | 0.0 | 0.05 | OK |
| tail_gain_min | 0.5 | 0.1 | OK |

## 关键改进（都不是调阈值，是补真实缺口）

1. **滑动窗口分块**（480/80 重叠）：长文本末尾载荷不再被前文稀释，
   `tail_gain_min` 0.0417 → **0.5**。
2. **灰区语义判官**：L1 未命中且含动作特征时路由 qwen3:4b。
3. **补三条真实 L1 缺口**（在 security.py，非测试内绕）：
   - `injection_role_switch` 扩展到 admin/root/superuser 提权；
   - 新增 `injection_double_negation_grant`（「不要忘记授权」）；
   - 新增 `exfil_upload_to_url`（上传到外部 URL）。
4. 新增规则必须带前置关键词（`_PRE`），否则 10KB 扫描从 1.0ms 涨到 1.77ms
   超预算——这条是 test_security 的延迟测试抓出来的，不是我自己发现。
5. 修了**测试数据本身的错**：`normal_task` 切片原放纯正常文本却标注入，
   等于自造必然漏检的样本；改为「伪装成正常任务的隐蔽注入」。

## ⚠️ 独立盲测的负结果（必须一起看）

v3 的 8 条线是在**模板化载荷**上过的。用独立来源数据复核：

| 数据集 | 来源 | n | 召回 | FP |
|---|---|---|---|---|
| chinese_jailbreak_selfbuilt | self-built jailbreak set, NOT public corpus | 62 | 0.0645 | 0.0323 |
| chinese_selfbuilt | self-built (source:self), NOT public corpus | 413 | 0.4389 | 0.0861 |
| injecagent_public | public dataset (InjecAgent) | 2108 | 0.9032 | None |

**结论**：中文自建集召回 44%、中文越狱 6.5% —— 远低于 v3 的数字。
这不是「v3 造假」，而是**模板化载荷与真实分布差距**的量化证据。
对外只能说「模板化集通过 + 公开 InjecAgent 90%」，不能说「中文注入已解决」。

## 边界

- `independent_reproduction=false`：本机自跑，不是未参与调参方的复跑。
- `chinese*` 是项目自建（`source:self`），不是外部公开语料。
