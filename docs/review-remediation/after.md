# 独立审查整改 · 修后证据

与 `before.md` 用**同一份脚本、同一套验收条件**。机器可读结果在
`after/review_checks.json`，逐项台账在 `README.md`。

## 一、同一检查入口：修前 1/12 → 修后 14/14

```bash
python scripts/review_checks.py --gate --out docs/review-remediation/after
# == 14/14 pass, 0 fail ==   GATE_EXIT=0
```

修后新增了 2 项检查（企业模式端到端、告警指标一致性），所以分母从 12 变 14。
每项都对应某个 finding 的**验收条件**，不是「文件存在吗」：

| 检查 | 怎么验的 |
|---|---|
| EG-003/context | 标记文件模拟构建上下文，真跑 `.dockerignore` 匹配，8 个敏感标记全排除 |
| EG-007/retention | 真写 3 条 → 按保留期删 → 重算哈希链 |
| EG-015/SLI | 用构造状态计数验算 `classify()` / `sli()`，429/404 不计入可用率 |
| EG-005/stable-4xx | 真起服务发畸形 JSON 与 2MiB body，连跑 5 次全是 4xx |
| EG-010/health-ddl | `h_health` 里不再构造 `core.Store` |
| EG-013/metric-consistency | 告警 yaml 里 6 个指标名逐个在 `app.py` 找来源 |
| EG-001+002/enterprise | 真起服务、真 token、真跨租户查询 |
| EG-023/gate | CI 8 项门槛齐全 |

## 二、独立验收脚本

```bash
python scripts/enterprise_smoke.py --out docs/review-remediation/probes   # 9/9
python scripts/check_docker_context.py                                     # 5/5
python scripts/check_wheel_manifest.py dist/*.whl                          # PASS
python scripts/wheel_smoke.py --cli <venv evoguard> --workdir <空目录>      # 10/10
python -m unittest tests.test_api_contract -v                              # 8/8
```

`enterprise_smoke.py` 实测到的关键结果：

```
缺配置时启动被拒绝（AGENTLAB_IDENTITY_MAP / AUDIT_DB 未设）
未绑定 token + 伪造 X-Tenant-Id -> 401
绑定 token 自报 evil，实际 tenant=acme，identity_verified=True
reader 角色 POST /api/run -> 403      executor -> 200
acme runs=1，beta runs=0             紧接 acme 请求后 beta 仍是 beta
acme 审计 4 行，链验证 True
```

`wheel_smoke.py` 在**独立 venv、源码树外的空目录**里跑通
`--help / config / scan / intent / demo / serve`，`serve` 下
`/`, `/api/health`, `/api/meta`, `/api/runs`, `/metrics`, `/api/ready`
全部 200，业务库落在工作目录而非安装目录。

## 三、性能：改前改后并列（这里有个不好听的结论）

| 来源 | 工作负载 | `POST /api/run` P95 | 吞吐 |
|---|---|---|---|
| 独立审查（改前 `793c6d1`） | 8 并发 × 36 次，audit off | **3534.7 ms** | 6.96 rps |
| 本轮（改后） | 8 并发 × 24 次 × 3 轮，audit off | **3500.09 ms**（3 轮中位） | 约 4.3–6 rps |

两组参数不完全一致（36 vs 24 次），**不做百分比对比**。但量级几乎一样：
我这轮把 schema DDL 移出热路径、加了索引，**对写路径瓶颈基本没用**。
瓶颈在「同步执行 + SQLite 写锁 + 每步各自开连接提交」，也就是审查的
EG-009 / EG-010 指的地方 —— 这两项在台账里仍然标 ⬜ / 🟡，没有解决。

读路径的数字也没拿来邀功：审查用 12 并发 × 160 次，本轮用 8 并发 × 24 次，
工作负载不同，不能说「P95 从 X 降到 Y」。要下这个结论，得用同一脚本、
同一参数，在改前的提交上再跑一遍 —— 本轮**没做**，所以不写。

原始样本在 `experiments/loadtest_v1/samples.jsonl`，环境/提交/配置指纹在
`experiments/loadtest_v1/result.json`。压测脚本支持 `--baseline --max-regression`，
但绝对门槛需要业务目标，本轮没有，**不擅自冻结一个数字当 SLA**。

## 四、明确没验的（不能拿静态检查冒充）

- **Docker 镜像层**：本机连不上 Docker Desktop 的 Linux daemon，没有真 build。
  `.dockerignore` 与 COPY 清单只做了规则级模拟验证。
- **CI 是否真的绿**：全部工作是本地的，未创建远程仓库、未推送、未发布制品。
  `.github/workflows/test.yml` 的 Linux/Windows wheel job 与 release-gate
  只是配置好了，**没有在 GitHub 上跑过**。
- **真实浏览器**：前端解耦只验了 `Promise.allSettled` 结构与 `node --check`
  语法；桌面/移动/键盘、慢网络、权限不足的交互没有自动化实测。
- **真实企业环境**：没有 SSO、多节点、真实业务流量、第三方合规验收。
  企业模式的身份源是本地 token 映射表，审批后端是 stub 标记。
- **检测器 v3**：预注册未做，v2 的负结果原样保留，没放宽判定线。

## 五、分数旁边现在写了什么

`evoguard scorecard` 仍是 84.8/100。审查说「仓库自评分数不计入结论」，
所以本轮**没有把分数当进度**，而是在它的报告顶部加了独立审查口径：

> 27 项发现中 ✅ 5 完成 / 🟡 12 部分 / ⬜ 10 未做。不要只看分数。

`docs/enterprise-scorecard.md` 已重生成，stdout 与 `--out` 逐字节一致
（`tests/test_enterprise.py::test_stdout_matches_out_file` 守着）。

## 六、最终复验（提交后，干净 clone）

```
主仓库         : Ran 874 tests ... OK                       / 14/14 gate
干净 clone     : Ran 874 tests ... OK (skipped=41)         / 14/14 gate
干净 clone CLI : evoguard config PASS / scorecard --min 80 PASS
干净 clone 制品 : pip wheel . -> 独立 venv 安装 -> check_wheel_manifest PASS
               -> wheel_smoke 10/10（含 serve 六个路由全 200）
```

`skipped=41` 全部是依赖 `data/external/` 语料的用例（该目录按许可与体积
不入库），不是失败。干净 clone 能独立构建可用制品，说明打包配置不依赖
主工作树里的任何未跟踪文件。

**仍然没有验的**：Docker 镜像层（daemon 不可达）、GitHub CI 真实运行
（本轮全部本地，未创建远程仓库/推送）、Linux 平台的 wheel 冒烟
（本机 Windows；CI 矩阵里配了但没跑）。
