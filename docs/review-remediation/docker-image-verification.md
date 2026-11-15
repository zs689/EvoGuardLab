# Docker 镜像层实测（2026-11-15）

**结论：6/6 通过，EG-003 从 🟡 转 ✅。** 过程中挖出一个真 bug：
**Dockerfile 漏拷 `evoguard_inspect`，镜像此前根本构建不起来**——这是
静态审查查不出、只有真 `docker build` 才会暴露的问题。

## 一、环境与前提

- Docker Desktop 4.87.0 / Engine 29.7.2 / `OSType=linux` / `amd64`
  （张硕手动启动，未改任何安全设置、未放行 `wsl.exe`）。
- 基础镜像 `python:3.11-slim` 从 Docker Hub 拉 590 秒只下了
  7.34MB/29.86MB；改用国内源 `docker.1ms.run/library/python:3.11-slim`
  拉取后 `docker tag` 成本地名。**没有改 Dockerfile、没有改 Docker 配置**，
  只是换下载源。
- 构建产物：`evoguardlab:verify`，ID `6e0dba1f50a0`，**191MB**。

## 二、实测挖出的真 bug（已修）

`docker build` 第 11 层报：

```
error: package directory 'ai-service/evoguard_inspect' does not exist
```

`pyproject.toml` 的 `packages` 声明了 `evoguard_inspect`、
`package-dir` 映射到 `ai-service/evoguard_inspect`，但 Dockerfile 的
显式 COPY 清单里**没有这一行**，于是容器内 `pip install -e .` 必失败。

- 修法（最小改动）：Dockerfile 加
  `COPY ai-service/evoguard_inspect ./ai-service/evoguard_inspect`。
- 防回归：`scripts/check_build_context.py` 增加「pyproject 声明的
  包目录 / data-files 源文件是否都被 COPY 覆盖」校验。
  修前若加了这项会直接判红；修后 2 个目录 + 7 个文件、漏拷 0。

**这也说明之前「Dockerfile 有显式 COPY 清单所以安全」的说法不完整**：
清单本身漏项时，两层静态防线都挡不住，只有真构建能发现。

## 三、验证脚本自身的假绿（已修）

第一版 `verify_docker_image.py` 用字符串拼 shell 命令。Windows 的
`cmd.exe` 不认单引号，导致 `find` 那条**执行失败但 stdout 为空**，
而判定条件是「输出为空即通过」→ **假绿**。已全部改成参数列表
（`shell=False`），并加上 `rc == 0` 约束。重跑后 `no-leaked-files`
才成为可信的 PASS。

另外两处脚本问题也修了：wheel 复制成 `/tmp/pkg.whl` 被 pip 判为
「不是合法 wheel 文件名」；`/api/live` 按设计不碰 DB，需要再打一次
`/api/health` 才会在卷里建库。

## 四、六项实测结果

`probes/docker_image_verification.json`（脚本
`scripts/verify_docker_image.py` 可复算）：

| # | 检查 | 结果 |
|---|---|---|
| 1 | 镜像层无 `.git`/`.env`/`*.db`/语料/`data/tenants` | PASS（`find` rc=0 且输出为空） |
| 2 | 历史层无 `COPY . /app` | PASS（`docker history`，无整仓 COPY 层） |
| 3 | 非 root | PASS，`uid=10001(evoguard)` |
| 4 | `--read-only` + `--tmpfs /tmp` + 卷 | PASS，`State.Health.Status=healthy`，`/api/live` 200 |
| 5 | 卷与镜像层分离 | PASS，触发 `/api/health` 后卷里有 `evoguard.db`，镜像层 `/app/data` 为空 |
| 6 | **Linux wheel** | PASS，全新 `python:3.11-slim` 容器里 `pip install` wheel、`import evoguard / evoguard.enterprise / evoguard_inspect`、`evoguard --help` 全通 |

**全程本机**：不 push 镜像、不建外部仓库、不上传任何文件。
临时容器与卷在脚本 `finally` 里已清理。

## 五、对台账的影响

- **EG-003 容器数据暴露面：🟡 → ✅**（唯一缺口「镜像层未实测」已补，
  并修了漏拷 bug）。
- **EG-004**：Linux wheel 已在本机 Linux 容器实测（此前只有 Windows
  独立 venv 10/10）；边界「GitHub CI 未实跑」仍在，不写成通过。
- **EG-016**：容器运行层（非 root / 只读根 / 卷 / healthcheck）已实测；
  反代 TLS、备份演练、RPO/RTO 等仍未做，保持 🟡。

## 六、仍未验的 / 边界

- GitHub Actions CI 真实运行（含 Linux/Windows 双平台 job）仍未实跑。
- 多主机共享存储、真实业务流量、反代 TLS 拓扑、备份恢复演练未做。
- 本次构建用了国内镜像源绕过境外下载慢的问题；CI 里的拉取路径
  与这里不同，不能把「本机构建成功」外推成「CI 一定成功」。
- `python:3.11-slim` 是本次拉的基础镜像，未做镜像签名/漏洞扫描。

## 七、复现

```bash
docker build -t evoguardlab:verify .
python scripts/check_build_context.py --json <path>
python scripts/verify_docker_image.py --image evoguardlab:verify --json <path>
```
