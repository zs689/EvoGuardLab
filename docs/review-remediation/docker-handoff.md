# 你要做什么：Docker 镜像层实测操作手册

> 目标：让 Docker daemon 可用，我就能做 EG-003 镜像层实测 + Linux wheel 冒烟。
> **全程不装任何新东西、不改 Windows 安全设置、不 push 镜像、不建远程仓库。**
>
> 当前状态（2026-11-15 16:00 我实测）：`docker info` 报
> `npipe:////./pipe/dockerDesktopLinuxEngine … 系统找不到指定的文件`，
> 即 Docker Desktop 没在运行。CLI 29.7.2 / Desktop 4.87.0 已装在
> `%LOCALAPPDATA%\Programs\DockerDesktop\`，不用重装。

---

## 方案 A：手动启动（推荐，零配置改动）

### 依据与不确定性（先说清，别让你白做）

我判断「你手动启动后，我这边用 `docker` CLI 就不再需要 `wsl.exe` 放行」，
理由是 `docker build/run` 走 daemon 的 npipe 管道，不直接调 `wsl.exe`；
被黑名单拦的是**启动阶段**（`docker desktop start` 触发了 `wsl.exe`）。

⚠️ **这是推断，我没验证过**。你从 WorkBuddy 外面手动启动，进程不在我的
命令沙箱里，理论上不受黑名单影响；如果实测不成立，就退回方案 C，
不要为了它去改安全设置。

### 第 1 步：启动 Docker Desktop（在 WorkBuddy 窗口外面操作）

1. 按 `Win` 键 → 输入 `Docker Desktop` → 回车。
   或者直接双击：
   `C:\Users\zs\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`
2. **普通启动即可**，不用「以管理员身份运行」。
3. 可能弹出的窗口，按下面处理：
   - UAC 用户账户控制 → 点「是」
   - 服务条款 / 使用协议 → 接受
   - 登录 / Sign in → **可以跳过**，不登录也能本地用
4. 不需要输入任何密码、token、密钥。

### 第 2 步：等它就绪

- 看屏幕**右下角托盘**（点 `^` 展开），找鲸鱼图标。
- 图标在转 / 有动画 = 还在启动，**首次可能 2–5 分钟**。
- 图标静止 = 就绪。

### 第 3 步（可选，能省一轮来回）：自己验一下

打开 PowerShell 或「终端」，输入：

```
docker info
```

| 你看到 | 含义 |
|---|---|
| 输出里有 `Server:` 段，含 `Server Version: 29.7.2` | ✅ 好了，直接第 4 步 |
| 只有 `Client:` 然后 `failed to connect to the docker API at npipe...` | ❌ 还没起来，再等 1–2 分钟 |
| 等了 5 分钟还是不行 | 把窗口里的报错**原样贴给我**，别自己改设置 |

### 第 4 步：回来跟我说一声

就打「好了」或「起来了」。我会自己重新探测，不用你贴一堆输出。

### 第 5 步：接下来我做什么（你不用操作）

| 检查 | 命令/做法 | 通过标准 |
|---|---|---|
| 构建 | `docker build -t evoguardlab:verify .` | 成功出镜像 ID |
| 夹带检查 | 容器里查 `.git` / `.env` / `*.db` / `data/external` / `data/tenants` / `__pycache__` | 全都不存在 |
| 非 root | `docker run --rm … id` | uid = 10001，不是 0 |
| 只读根 | `--read-only --tmpfs /tmp` 起服务 | 能正常响应 |
| healthcheck | `docker inspect … State.Health.Status` | `healthy` |
| 卷 | `-v <宿主目录>:/app/data` | db 落在宿主目录、镜像里没有 |
| Linux wheel | 容器内 `pip install` wheel + `wheel_smoke.py` | 10/10，补上 Linux 数字 |

**不会做**：`docker push`、连外部仓库、把任何文件传出本机、修改你的配置。
做完把每层体积、镜像 ID、各项结果写成 `docs/review-remediation/docker-image-verification.md`。

### 第 6 步：做完之后

**什么都不用关**（因为一行设置都没改）。想省资源就托盘右键 → Quit。

---

## 可能卡住的两处（提前说，免得你白等）

这两处都是**境外网络依赖**，和 `wsl.exe` 黑名单无关，
所以「放行 wsl.exe」也解不了 —— 这是我上轮说「一次做完、EG-003 转绿」
说得太满的原因，已更正。

1. **Docker Desktop 启动阶段**
   backend 日志上次停在
   `fetching manifest from https://desktop.docker.com/modules/4.87/modules.json`
   之后再无输出。如果这个域名访问不了，托盘可能一直转圈。
   → 需要让这台机器能访问该域名（挂代理），或在 Docker Desktop 设置里
   关掉自动更新/模块检查（具体菜单名我没实测，**标待确认**）。

2. **`docker build` 阶段**
   - `FROM python:3.11-slim` → 要拉 Docker Hub
   - `RUN pip install -e .` → build-system 要 `setuptools>=68`，走 PyPI
   → 国内网络可能慢/超时。可在 Docker Desktop → Settings → Docker Engine
   加 registry mirror；PyPI 可换镜像源（那需要改 Dockerfile，我会先问你）。

**任何一处卡住，方案 A 就做不完。这时直接走方案 C，不要去动安全设置。**

---

## 方案 B：如果你更想改那个开关（不推荐）

1. WorkBuddy → 安全中心 → 命令安全 → 程序黑名单
2. 找到 `wsl.exe`（`C:\WINDOWS\System32\wsl.exe`）→ 移除/关闭
3. 告诉我，我用 `docker desktop start` 自己启动并做实测
4. **我做完会立刻提醒你把它加回去** —— 这个设置持续生效，别留着

风险实质（再说一遍）：它改的是 WorkBuddy 命令沙箱的黑名单，
**不是** Windows Defender / 防火墙 / 组策略；放行 = 让我这个 agent
能调用 `wsl.exe`（能进 Linux 环境、默认挂载 `/mnt/c`、权限边界变大）。
不改系统安全、不开端口、不需要凭据。仍然有上面两处外网坎。

---

## 方案 C：什么都不做

EG-003 保持 🟡，文档如实写「镜像层未实测」。
项目没上生产，这完全能接受，零风险零成本。

---

## 我的推荐

**先花 2 分钟试方案 A**。不成直接 C，不必碰 B。
为一个 🟡 放宽 agent 权限不划算，何况放行也未必真能做完。
