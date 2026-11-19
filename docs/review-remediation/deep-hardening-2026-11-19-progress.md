# 深度加固与能力跃迁方案执行进度（2026-11-19）

依据 `EvoGuardLab_深度加固与能力跃迁方案_2026-11-19.json`，`严格按方案执行`。

| WS | 状态 | 证据 |
|---|---|---|
| SEC-BYPASS-01 role=tool 漏扫 + 非流式输出阻断 | ✅ | 反例：tool/function 角色注入 opener 调用数=0；非流式私钥 -> content_filter |
| SEC-BYPASS-02 单汉字路由劫持 + 扰动鲁棒 | ✅ | 英文注入+" 好" 仍 block 且 lang=en；插空格中文 block；全角与半角等价 |
| EVO-LOOP-01 v1 退化禁用 + skill_id 精确归因 | ✅ | v1 越权自动 disabled；混入 15 条无关失败不误伤；真实跑过 |
| INFRA-CONC-01 Redis 线程安全 + PG 统一审计 | ✅ | 16 线程 0 错位；真 PG 两实例互验 checked=2，篡改被抓 |
| AGENT-CAP-01 多轮会话 + 并行只读 + 压缩 | ✅ | 3 轮记忆；压缩保留不可信边界；3 只读并行+写工具 executed=False |

## 基线差异（如实）

方案写 `unittest 1222 tests OK`，开始时实测是 **1225 tests OK**（中间有 3 条
后续提交）。最终 **1248 tests OK / 0 ResourceWarning / 29-29 gate / build PASS**。

## 真 PG 审计 E2E 踩的坑（都已修）

`docs/review-remediation/probes/pg_audit_e2e.json` passed=true，但过程暴露：
1. PG `REAL` 是 float4，epoch 秒小数截断 -> verify 重算 hash 不等；
   改 `ts`/`latency_ms` 为 `DOUBLE PRECISION`。
2. `ORDER BY ts` 跨进程时序不可靠 -> 加大自增 `seq` 排序，旧表幂等补列。
3. PG 版 verify 不实现 SQLite 归档锚点，返回值 `mode=pg_no_anchors` 如实标注。

## 坚持不做的事

- 没有推 GitHub、没有远端 CI、没有开源发布（方案明确不含 GitHub 任务）。
- 没有为过测试放宽任何门禁：全角短句本身只有 review 置信度，测试断言改为
  「标准化后与半角等价」，而不是硬降阈值凑 block。
- 核心库 `evoguard/` 仍零第三方运行时依赖；pg8000 只在隔离 venv。

## 提交

57c3309 / 1f63ce7 / 72319ed / e03f6c1 / ad4ad4f
