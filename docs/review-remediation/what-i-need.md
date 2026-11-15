# 还差什么（按「需要你提供什么」分类）

> **照着做的操作手册**（精确到点哪里、看到什么算成功）：
> [`docker-handoff.md`](docker-handoff.md)。推荐方案 A：手动启动
> Docker Desktop，零配置改动、不碰安全设置。

> ## 进展（2026-11-15，你说「自己搞」之后）
>
> **B1 语料 —— 大幅推进，但不是靠「补 commit」**：复核发现「4 源缺 license」
> 是我 `verify_corpus.py` 只读顶层字段的 bug（数据写在嵌套 `source`/`sources`）。
> 修完 + 联网核实后：5 个源 license/license_url 齐全、全部
> `content_pinned`；InjecAgent 的 commit `f19c9f2c…` 由第三方清单的
> `source_file_sha256` 与本地 `files[0].sha256` 完全一致印证。
> 告警 9 → 4，且全部精确为「上游 commit 未核实」。**`--strict` 仍判红**，
> 没放宽：内容哈希锁字节 ≠ 上游版本可追溯。见
> `data/corpus_upstream.json`、`docs/corpus-supply-chain.md` 顶部更正块。
>
> **A1 Docker —— 尝试过，daemon 起不来**：
> - CLI 在 `<LOCALAPPDATA>\Programs\DockerDesktop\resources\bin\docker.exe`
>   （用户目录安装，不在 Program Files），版本 29.7.2 / Desktop 4.87.0；
> - 我启动了 `Docker Desktop.exe`（pid 28528），等 96 秒 daemon 仍不可达，
>   进程随后退出；
> - **真正根因（后来用 `docker desktop start` 查到）**：该命令触发
>   `wsl.exe`，被 `PROGRAM BLOCKED BY SECURITY POLICY` 直接拒绝。
>   Docker Desktop 依赖 WSL2，所以 A1 和 A2 是**同一个阻塞**，不是两件事。
>   早先看 backend 日志停在拉 `desktop.docker.com/modules/4.87/modules.json`
>   只是表象，那个推断已更正。
> - **不需要 daemon 的部分已做**：`scripts/check_build_context.py`
>   静态模拟 `.dockerignore`，PASS（180 文件 / 2.2 MiB，`.git`/`.env`/
>   `*.db`/语料/租户数据/缓存全挡，Dockerfile 的 8 个 COPY 源齐全）。
>   产物 `probes/build_context.json`。**这不等于镜像层实测**。
> - **要继续只需要你做一个动作**：安全中心 → 命令安全 → 程序黑名单，
>   移除 `C://WINDOWS//System32//wsl.exe`（路径已脱敏写作
>   `%WINDIR%\System32\wsl.exe`）。放行后告诉我，我立刻做镜像层实测
>   与 Linux wheel 冒烟。
>
> **A2 WSL —— 与 A1 同一个根因，被安全策略硬拦，我不会绕**：
> - `wsl.exe` 调用直接被 `PROGRAM BLOCKED BY SECURITY POLICY` 拒绝
>   （程序黑名单），规则明确禁止换壳重试；
> - 只读探测：`vmms` 与 `WSLService` 都在 Running，但
>   `%LOCALAPPDATA%\Packages` 下**没有任何 Linux 发行版**；
> - Docker Desktop 4.87 日志显示自带 `Ubuntu-7.0.0-28.28`（docker-desktop
>   发行版），所以**只要 A1 的 daemon 起来，跑 Linux 容器就能覆盖 Linux
>   wheel 验证**，不必单独装 WSL 发行版。
>
> **E 类已兑现两项**：SQL 前缀识别加固（`--` / `/* */` 注释、
> `WITH … INSERT`，测试 11 → 13 项）、构建上下文静态检查。
> 其余四项（v3 可执行化、小时级 soak、更多压测轮次、可访问性自查、
> gate 并发压测）下一轮继续，不用你提供任何东西。


台账现状：**✅6 / 🟡21 / ⬜0**。下面每一项都写清「卡住哪条」「我需要什么」
「拿到能产出什么」「拿不到的替代方案」——不把「我不想做」混成「做不了」。

---

## A. 环境 / 机器

### A1. Docker daemon（卡 EG-003、EG-016）

- **现在**：`docker info` 报 `npipe:////./pipe/dockerDesktopLinuxEngine …
  系统找不到指定的文件`，两次复查都不可达。
- **需要**：启动 Docker Desktop，或给我一台能跑 `docker build` 的机器。
- **拿到能做**：镜像层实测——`.dockerignore` 真的排除了 `.git/*.db/语料`、
  非 root 用户、只读根文件系统、healthcheck、独立卷、容器内 CLI 冒烟、
  镜像体积。EG-003 可以从 🟡 到 ✅。
- **拿不到**：只有 Dockerfile 静态审查 + 本地非容器冒烟，**不能声称镜像可用**。

### A2. Linux 环境（WSL2 也算）（卡 EG-004、EG-015）

- **现在**：所有 wheel 安装冒烟、压测数字都只有 Windows。
- **需要**：任意 Linux（WSL2 一条命令就够）。
- **拿到能做**：Linux wheel 安装 + CLI 冒烟、`loadtest` 在 Linux 的数字、
  文件锁 / 线程行为差异对比（SQLite 在 Linux 上锁语义和 Windows 不同）。
- **拿不到**：跨平台只能写「未实测」。

### A3. 第二台机器或共享目录（卡 EG-010 多主机部分）

- **现在**：单主机 4 进程 × 8 线程已验（14691 写 / 0 错），多主机没验。
- **需要**：两台机器访问同一个 db 文件（NFS / SMB 挂载都行），或者一台机器
  挂一个 SMB 共享目录模拟。
- **拿到能做**：多主机共享存储下写串行化是否仍成立、SQLite 文件锁在
  网络文件系统上的行为。
- **拿不到**：只能写「仅单主机」，不能外推。

### A4. 真实浏览器（卡 EG-020、EG-021）

- **现在**：前端只有 HTTP 层测试 + 静态审查；你机器上我没装浏览器自动化。
- **需要**：允许我在隔离目录装 Playwright（用完删），或你手动点一遍录屏。
- **拿到能做**：令牌输入 → 401/403 提示 → 会话失效 → 降级的端到端验证；
  键盘可达性与 ARIA 粗查。
- **拿不到**：前端交互只能靠代码审查。

---

## B. 上游数据 / 凭据

### B1. 语料的上游 commit 与 license（卡 EG-018，**唯一「补数据就能变绿」的项**）

- **现在**：5 个源里 4 个缺 `license`，**全部** `git_sha` 是 `cache` 占位符。
  `verify_corpus.py --strict` 因此失败。
- **需要**，任选一种：
  1. 每个源的不可变 commit SHA（40 位）+ 许可证名称与链接；
  2. 或者授权我用 Gitee 镜像 / GitHub API 去查（你之前说 GitHub 访问困难，
     Gitee 可以；但 AgentDojo/InjecAgent 在 Gitee 未必有镜像）。
- **拿到能做**：替换占位符、补齐 license，`--strict` 变绿，台账 EG-018
  可从 🟡 到 ✅。
- **拿不到**：锁清单只能证明「生成产物没被改」，**证明不了上游版本**——
  所以 v3 的「跨域泛化」也不能声称。

### B2. 真实业务流量或 SLA 目标（卡 EG-015 绝对门槛）

- **现在**：只有相对 A/B（新默认 1093 ms vs 全关 2816 ms），没有绝对门槛。
- **需要**：一句「写接口 P95 必须 < X ms / 可用率 > Y%」，或脱敏的请求分布
  （QPS、读写比、payload 大小）。
- **拿到能做**：把 `--max-regression` 从「只报告」变成真守门，
  冻结发布门槛。
- **拿不到**：不编数字当 SLA，只报相对值。

### B3. 外部审批系统凭据（卡 EG-026）

- **现在**：`approved_by` 是本地字符串，`mark_effect()` 要调用方自己填回执，
  没有真实外部审批人。
- **需要**：钉钉/飞书/企业微信审批的 app key + 模板 ID；**或者明确「不做外部对接」**。
- **拿到能做**：真实审批人解析、审批结果回调、外部效果回执、跨系统对账。
- **拿不到**：继续是 design gap（台账里也是这么写的）。

---

## C. 人（这条最卡结论）

### C1. v3 检测实验的独立复跑者（卡 EG-017，最根本的一条）

- **现在**：v2 已判「未过自己的预注册线」，v3 只写了预注册
  （判定线 sha256 冻结、8 条门限），**实验没跑**。
- **为什么卡**：预注册里写明「由**未参与调参**的一方复跑」，本地只有我一个人。
  我自己跑 = 自己判自己，结果不算数。
- **需要**：一位同学/同事，按 `docs/semantic-injection-v3-plan.md` 执行
  `v3_gate`，并在结果文件上签字（谁跑的、什么时候、用哪个 commit）。
- **拿到能做**：v3 结果才算科学证据；「检测到底行不行」才有结论。
- **拿不到**：我可以把 v3 做成**可执行**（两个检测组 + 冻结集生成 + 判定线），
  但**不能自证**，台账保持 🟡。

---

## D. 需要你拍板的取舍

### D1. 要不要上多副本（影响 EG-001/005/010/014/019）

- 现状：隔离是按租户分库**不是 RLS**；限流/熔断是进程内；评测进度没有
  跨进程锁。这四条都指向同一个决定：**是否引入 Postgres/Redis**。
- 代价：打破「零依赖、离线可跑」——这是这个项目现在的卖点之一。
- 不做：就永远停在「单副本可信、多副本未验」。

### D2. 目标用户是谁（影响 EG-002/020/024/025 的投入深度）

- 「自己/课程演示」：现在的 demo 模式够了，不用 SSO、不用租户管理 UI。
- 「真企业试用」：需要 OIDC/SSO、审计导出、租户管理、SDK 与示例。
- 这直接决定剩下 21 项 🟡 里哪些值得继续投入。

---

## E. 不用你提供，我自己就能做的（你点头我就开）

| 项 | 产出 | 成本 |
|---|---|---|
| v3 **可执行化** | 实现 `v3_fields_only` / `v3_full` 两组 + 冻结集生成 + 判定线调用，**不跑结果、不自证** | 中 |
| 小时级 soak | 分多次 10 分钟累积到 1 小时，或一次挂 1 小时 | 机器闲置 |
| 更多压测轮次 | 把 HTTP A/B 从各 2 次加到各 5 次，提高置信度 | 约 15 分钟 |
| SQL 前缀识别加固 | 处理 `-- 注释` / `/* */` 前缀、`WITH … INSERT` 等边界，补测试 | 小 |
| 前端静态可访问性自查 | 不装浏览器，查 ARIA/焦点顺序/label 关联 | 小 |
| `_GATES` 并发压测 | 200 个线程抢同一把 gate，验 `waiters` 计数不漂 | 小 |

---

## 一句话总结

**最想要的三样**：① 语料的上游 commit + license（唯一能直接变绿的）；
② 一位不参与调参的人跑 v3；③ Docker daemon 或一台 Linux。

其余的（多副本、SSO、外部审批）都是**取舍**，不是「差东西」——
你不拍板，我就按现在的保守路线走。
