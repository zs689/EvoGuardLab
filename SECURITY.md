# Security policy (local, pre-release)

**状态：本地研究原型，未发布、未部署。** 这份文件说明发现问题时怎么处理，
以及哪些是**已知且刻意未修**的边界 —— 不要把已知边界当成新漏洞重复报。

## 不要做的事

- 不要在公开 issue、群聊或截图里贴可直接利用的载荷、token、凭据。
- 不要把本机 `.env`、`data/*.db`、`data/external/` 语料或审计库发给任何人。
  这些都不在版本控制里，但一旦贴出去就收不回来。
- 不要为了「验证」在别人的机器上跑写操作或起对外监听的服务。

## 报告方式（当前）

本地项目，没有公开仓库与对外安全邮箱。发现问题请在本地改：
1. 写一个能稳定复现的最小测试，放在 `tests/`。
2. 在 `docs/review-remediation/README.md` 台账里加一条，标注状态与证据。
3. 修完后跑 `python scripts/review_checks.py --gate` 与全量回归。
4. 提交信息里写清「修前怎么复现、修后怎么验」。

未来若发布，启用私有披露通道后再补 `security@` 地址与响应时限。

## 已知边界（不是漏洞，别当新发现）

- **demo 模式默认无鉴权**，只绑 `127.0.0.1`。任何对外暴露都必须先开
  `AGENTLAB_MODE=enterprise` 并置于 TLS 反代之后。
- **企业模式的身份源是本地 token→身份映射表**，不是真实 IdP/SSO。
- **租户隔离靠按租户分库**（业务库、审计库、作业库各自独立），不是数据库 RLS；
  sqlite 没有行级安全。
- **限流与指标是进程内内存**，多副本各算各的，没有共享限流层。
- **持久作业是单进程单 worker**：`runtime.FORCED_SKILL` 仍是进程级全局，
  靠执行锁串行化；多副本需要共享队列与分布式租约。崩溃恢复把过期租约
  标为 `unknown` 并**不自动重放**，需人工对账后显式 retry。
- **前端令牌只存 `sessionStorage`**：已在顶栏实现 Bearer 传递与 401/403
  反馈，但真实反代认证、失效/登出流程与 CSRF 保护未在部署拓扑实测。
- **熔断与并发上限是进程内的**：`ResilientProvider` 接进了真实模型调用路径，
  但熔断状态、信号量、限流都是单进程语义，多副本各算各的。
- **审批没有真实外部审批人**：`approved_by` 是本地字符串，外部效果要调用方
  自己 `mark_effect()` 回填；`reconcile()` 只列出待对账项，不自动重放。
- **语料上游版本未锁定**：`git_sha: cache` 是占位符，4 个源缺 license；
  锁清单只能证明生成产物没被改（见 `docs/corpus-supply-chain.md`）。
- **归档锚点表 `audit_anchors` 与审计库同库**：能改库的人理论上也能插锚点
  伪造删除。生产部署应把锚点备份到独立权限域（见 `docs/data-governance.md`）。
- **业务库 `steps.answer` / `expect` 可能含模型输出正文**：接口不透传，
  但没有字段级脱敏，详见 `docs/data-governance.md` §五。
- **同步 `/api/run` 仍在兼容期**，8 并发 P95 约 3.5 s；持久作业改善的是
  提交响应，不是实验本身变快。
- 检测器 v2 **未过预注册判定线**（AUC 下降 0.032 > 允许 0.01），负结果照记；
  v3 只完成预注册、实验未跑，见 `docs/semantic-injection-v3-plan.md`。

## 六字段契约

任何安全判定都必须保留 `detected / decision / attempted / executed / effect / reason`。
把「检测到」写成「已拦截」在这里算数据造假，不是措辞问题。

- **不要启用 WAL**（`AGENTLAB_STORE_WAL=1` 仅作实验）。稳定性验收实测：
  即使连接池常开连接、加了跨进程初始化锁，**单进程 24 线程并发写仍有
  约 1/8 概率报 `attempt to write a readonly database`**，开池+WAL 跑
  935 项全量回归会偶发挂 1 项。顺利时的性能数字不代表可靠。
  `journal_mode` 是**数据库文件头的持久设置**，撤掉环境变量不会还原
  已跑过的库，需要显式 `PRAGMA journal_mode=DELETE`。
  WAL 模式下备份必须连 `-wal` 一起做：实测只复制 `.db` 恢复 **0/200 行**，
  连 `-wal` 恢复 200/200。另有约 70 倍磁盘写放大
  （8610 B/写 vs 只开池 122.9 B/写）。
  见 `docs/review-remediation/store-pool-stability.md` §四。
  `AGENTLAB_STORE_POOL` 与 `AGENTLAB_STORE_SERIALIZE_WRITES` 已按 A/B
  证据**默认开启**（见 `single-writer-innovation.md`）；WAL 仍默认关。

- **并发写 `database is locked`：已解决（默认开启写串行化）**。
  以下保留问题描述；解法见 `docs/review-remediation/single-writer-innovation.md`。：本机 Windows、多连接并发写
  delete 日志时，SQLite 在 `INSERT` 阶段绕过 busy handler 直接返回
  SQLITE_BUSY。实测默认路径与连接池路径都有（0.1–0.4‰），10 分钟 soak
  98314 次写出现 1 次。表现是低概率 500、事务 rollback，不是数据损坏。
  COMMIT 重试与 `BEGIN IMMEDIATE` 两种解法都试过：前者无效（已撤回），
  后者只把错误率减半（未接入）。详见
  `docs/review-remediation/lock-contention-known-issue.md`。
