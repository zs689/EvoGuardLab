# 自主学习深耕与安全闭环 执行进度（2026-11-22 晚 ~ 11-23）

对应方案：`EvoGuardLab_自主学习深耕与安全闭环方案_2026-11-22.json`

| Workstream | 状态 | 产物 |
|---|---|---|
| EVO-DEEP-01 规约冲突 + 微工具合成 | **完成** | `_slot_key`/`active_invariants`、`tool_synthesis.py` |
| EVO-DEEP-02 在线学习闭环 | **完成** | `ReActAgent(auto_learn=)` |
| DET-DEEP-01 越狱召回 + L2 限速 | **完成（两项验收均达成）** | `pub_jailbreak_layered.json`、`pub_bench_layered.json` |
| ARC-DEEP-01 状态封装后拆分 | **部分（拆了诊断，Ctx 回滚）** | `legacy_diagnose.py`；`runtime.py` 1320→1013 |
| 模块级提分（11-23） | **进行中** | 见下表 |

## 一、能力指标（全部实测）

| 项 | 数值 | 证据 |
|---|---|---|
| 越狱拦截（英文 direct） | recall 0.93 / fpr 0.02 | `pub_jailbreak_layered.json` |
| 越狱拦截（中文） | 0.935/0.000、0.935/0.000、0.900/0.100 | 同上，三套中文集 |
| InjecAgent 检测器口径 ASR | 0.0645（去重后 62 条） | `pub_bench_layered.json` |
| L2 服务可用性 | `L2_unavailable=0`，全量 1 分钟跑完 | 同上 |
| 技能自进化（离线） | 三轮各 95%（38/40），p<0.01 | `env_bench_final_r1..r3.json` |
| 跨模型（判官链路） | deepseek 1.00/0.00；qwen2.5:7b 本地 0.90/0.00 | `cross_model/judge_probe*.json` |
| 工具合成（模型产码） | 3/3 走完 白名单→沙箱→注册 | `tool_synthesis_e2e.json` |
| Docker 沙箱隔离 | 6/6（网络/文件/超时/内存/孤儿） | `docker_sandbox_verify.json` |
| 真 PG 16 | 跨副本一致 + RLS 隔离 | `probes/pg_store_e2e.json` |

## 二、这一轮修掉的真缺陷（都是"会静默出错"那类）

1. **流式输出门禁漏检**：`SCAN_EVERY=300` 且流结束不补检 → <300 字符的
   恶意输出整段不检查就转发；逐字符拆分同样绕过。改逐帧扫尾部窗口。
   回退验证：3 条新用例变红，恢复后全绿。
2. **自进化归因误杀**：线上 runs 无 `skill_id` 归属时按 task_type 近似汇总，
   拿全量成功率（0.75）比 100% 基线 → 生产库 **7 个 active 全被判退化**。
   现在无归因拒绝自动回滚。
3. **回滚假止血**：v1→v2 只改晋升标记时正文 hash 相同，回滚到 v1 等于没换药
   却报 `rolled_back=True`。现在内容相同一律 disable。
4. **注入被证伪规约**：70 个 REJECT 技能仍是 candidate，而注入侧查 candidate。
   现在 audit 回写状态 + 注入侧按评估结论二次过滤。
5. **监控被宿主打死**：`agent_watch._calls` 对畸形返回（None/字符串/列表混
   None）直接崩 —— 被监测对象一返坏数据，监控自己先挂。
6. **环境变量大小写**：`TRUE` / `" On "` 被当成**关闭**。
7. **评测分桶恒等式写错**：`L2_undecided`/`L2_unavailable` 漏在恒等式外，
   136 条不确定样本时是靠 `tool_gate` 数字凑巧成立的。

## 三、模块级测试补齐（1615 条，12→5 skipped）

| 模块 | 新增 | 覆盖重点 |
|---|---|---|
| text_hygiene | 11 | 零宽/tag/bidi、正常文本零改动、性能预算 |
| semantic_detectors | 12 | 三个开关真值表、模型坏路径不崩、错误不折算成未命中 |
| fs_sandbox | 10 | 绝对路径/../非法名/写入开关/符号链接逃逸 |
| gateway_protocol | 10 | preset 结构、厂商名→后端映射、400/404/5xx 错误码 |
| openai_proxy_stream | 9 | 短攻击/跨帧拆分/逐字符拆分/非法帧/tool_call 零转发 |
| pg_store | 16 | DSN、方言翻译（字面量/注释里的 ? 不换）、驱动缺失 |
| agent_watch | 9 | 异常/None/字符串/畸形 tool_calls 不崩 |
| legacy_diagnose | 14 | seq 与 logical 对齐、退化定位不丢抑制点 |
| plan_runner | 13 | 伪造审批键、跨租户、重复审批、票据一次性 |

## 四、仍未达标（如实记录）

| 项 | 现状 | 卡在哪 |
|---|---|---|
| `runtime.py` 1013 行 | 未达 550 | Ctx 拆分两次回归：`_last_attempt` 的"只取最后一轮"语义跨模块失效 |
| 自进化晋升率 9% | 7 PROMOTE / 70 REJECT | 3 题验证 p=0.125，**统计上不显著**；改门禁会让 7 个 active 全失效，属协议变更，未擅自做（已输出 `chance_p_value`/`stat_note` 披露） |
| 跨模型 4/6 | qwen/openai/glm/kimi NOT_RUN | 缺对应厂商 key，不伪造数字 |
| `core.py` 902 行 | 未拆 | 存储/审计/状态机耦合，拆分收益低于回归风险 |
| gate 1 项 | 现已 33/33 | Docker 起来后 `DEEP-01/no-orphans` 已 PASS |

## 五、注意

- `eval_env_invariant_bench` 用本地 7B 跑是 0 通过（训练阶段就挂），那是
  **模型能力**问题，不能拿来当"协议不行"的证据。
- 真 PG 部署需先 `GRANT ALL ON SCHEMA public`（PG 15+），且**不能给
  SUPERUSER**——超级用户绕过 RLS 会得到假的"隔离通过"。

## 六、第三轮补强后的自评（2026-11-23 晚）

按"实测锚点 / 测试充分性 / 已知缺陷 / 规模"四维自评，从低到高逐个补：

| 域 | 补前 | 补后 | 关键动作 |
|---|---|---|---|
| runtime + ctx + diagnose | 84 | **92** | 删掉三份逐字重复的共享状态；`side_effects_total` 此前**永远是 0**（ctx 递增自己的 dict、evaluate 读 runtime 的）；ToolError 两份导致 except 漏接；顺带解开 `import ctx` 的循环 |
| 评测脚本 | 85 | **90** | 11 条缓存/检测回归（L2_unavailable 不进缓存、坏行不毒化、检测无状态）+ self_checks 自证 + gate EG-025 |
| plan_runner | 86 | **90** | 并发 resume：两副本同时恢复，写操作只执行一次 |
| agent_watch | 86 | **90** | 13 条规则表覆盖（危险工具正则/三类密钥/8 类 checked 项） |
| gateway | 86 | **91** | Anthropic Messages 协议 10 条回环；至此两种协议 + 两家真模型都验过 |
| core | 87 | **91** | DDL 59 行 + 状态机 37 行拆出，core 953→857；20 条 DDL/状态机完整性测试 |
| autonomous_agent | 87 | **90** | re-export 门面 7 条 + 拆件决策 21 条 |
| execution 等 | 88 | **91** | 三表一致性 13 条（scanner 名拼错会静默跳过检查） |
| openai_proxy | 88 | **90** | 流式门禁 12 条 + 3 处冷路径 NameError |
| security 等 | 88 | **91** | pinyin_guard 14 条 + 修一个真误报（引号内被翻译词被当攻击） |
| approval | 89 | **90** | 110 条 + 票据一次性/跨租户一致性 |
| 自进化 | 89 | **91** | 模型产码→合成→注册全链路 3/3、回滚演练、归因门禁 |
| sandbox | 90 | **93** | Docker 隔离 6/6 真容器 |
| pg_store | 90 | **92** | 真 PG 16 跨副本一致 + RLS 隔离 |

**1783 条测试全绿（5 skipped），gate 35/35。**

仍未达成（如实保留，不用"分数"掩盖）：
- 跨模型只有 2 家真跑（deepseek + 本地 qwen），4 家因缺 key 标 NOT_RUN；
- 技能晋升 3 题验证 p=0.125 不显著 —— 已输出 `chance_p_value`/`stat_note`
  披露，**没有**擅自把门禁改硬（会让 7 个 active 全失效，属协议变更）；
- Docker daemon 关着时 gate 的 no-orphans 会 SKIP，绝不伪造通过。
