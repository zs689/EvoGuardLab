# 独立审查整改 · 修前证据

来源：`E:/agent/EvoGuardLab_企业与高性能客观审查_2026-11-12.json`
（revision `793c6d1`，2026-11-12，独立克隆 + 独立 venv + 本机 HTTP 压测）

本文件只记**我自己复现过**的差距。`scripts/review_checks.py` 是同一检查入口，
修前跑出的机器可读结果在 `before/review_checks.json`。

## 一条先认下来的话

审查写了「**仓库自评分数不计入结论**」。上一轮我做的 `evoguard scorecard`
84.8/100 是自评，八个维度里有七个由「我写的探针检查我写的模块」判定，
确实不构成企业就绪的证据。这一轮不拿那个分数当进度。

## 复现结果（2026-11-12，本机 Windows / Python 3.13）

```
python scripts/review_checks.py --out docs/review-remediation/before
== 1/12 pass, 11 fail ==
```

| 检查 | 修前 | 证据 |
|---|---|---|
| EG-003 `.dockerignore` | FAIL | 仓库根无该文件；`Dockerfile` 是 `COPY . /app` |
| EG-003 COPY 清单 | FAIL | 同上。Git 忽略规则不限制 Docker 上下文 |
| EG-007 保留期后验链 | FAIL | 写 3 条 → `purge_before(2.0)` 删 1 条 → `verify()` 返回 `prev_mismatch / first_bad_id=2` |
| EG-015 SLI 口径 | FAIL | `scripts/loadtest.py:79` 是 `ok = sum(1 for c in codes if 200 <= c < 500)`，429/404 算进可用率 |
| EG-005 请求体上限 | FAIL | 入口无 `MAX_BODY_BYTES`，按 Content-Length 全量读 |
| EG-005 稳定 4xx | **PASS** | 真 HTTP：畸形 JSON → 400，2MiB body → 400。**没复现审查提到的 500**，如实记为通过。但 2MiB 被完整读入，说明「无上限」成立 |
| EG-010 热路径 DDL | FAIL | `h_health` 每次 `core.Store(DB)`，而 `Store.__init__` 会 `init()` 跑 schema |
| EG-011 业务库路径 | FAIL | `DB` 固定为仓库内 `data/evoguard.db` |
| EG-012 列表上限 | FAIL | `h_runs` 里 `limit = int(m.group(1))`，无 clamp |
| EG-013 探针拆分 | FAIL | 只有 `/api/health`，且它做三次 COUNT |
| EG-021 前端可选面板 | FAIL | 初始化用裸 `await Promise.all(...)` |
| EG-023 CI 硬门槛 | FAIL | CI 只有 `scorecard --min 80` 与 unittest |

## EG-004：独立 wheel 的 CLI（单独复现）

复用审查留下的独立 venv，未在你机器上装任何东西：

```
cd E:/agent/_enterprise_review_20261112/tmp
../wheel_env_793c6d1/Scripts/evoguard.exe <cmd>
```

| 命令 | 退出码 | 现象 |
|---|---|---|
| `--help` | 0 | 正常 |
| `config` | **1** | `ModuleNotFoundError`（`import baseline` 找不到） |
| `scan hello` | **1** | `ModuleNotFoundError`（`import security` 找不到）—— 审查没测这条，实测同样坏 |
| `demo` | **2** | `can't open file '.../site-packages/scripts/defense_demo.py'` |
| `serve` | **2** | `can't open file '.../site-packages/ai-service/api/app.py'` |

根因：`evoguard_cli.py` 用 `__file__` 拼 `ROOT/ai-service/evoguard` 再塞进
`sys.path`，安装到 site-packages 后那个目录不存在；`_run()` 又按 checkout
相对路径找 `scripts/`。`pyproject.toml` 只打了 `evoguard` 包，没打
`enterprise/` 子包、`evoguard_inspect`、前端与脚本资产。

## 审查给的独立性能数据（不是我测的，原样引用）

- `POST /api/run` 串行 12 次：P95 168.95 ms，8.22 rps
- `POST /api/run` 8 并发 36 次：P95 **3534.7 ms**，6.96 rps（并发反而更慢）
- 开审计后 8 并发：P95 3744.35 ms，6.93 rps
- 业务库 `journal_mode=delete`，`runs` 列表 `SCAN r` + `USE TEMP B-TREE FOR ORDER BY`

我这一轮不重新报一套「更好看」的数字来盖掉它；修完会用**同一脚本、
同一工作负载**再跑一次，改前改后并列。

## 复现命令

```bash
python scripts/review_checks.py --out docs/review-remediation/before
python scripts/review_checks.py --out docs/review-remediation/after
```
