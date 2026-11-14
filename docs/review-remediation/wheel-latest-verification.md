# 最新提交的 wheel 制品复验（2026-11-14）

第四~六轮改了 `core.py`（连接池、跨进程初始化锁、`__del__` 修复）、
`gateway.py`、`approval.py`、`real_model_bench.py` 和 `app.py`。
wheel 是隔了几轮没重建过的，这轮对着**本轮最终工作树（含所有撤回与修正）**
重新构建并验证，确认新代码真的进了包、且安装包能跑。
（构建过两轮：中途那版含后来撤回的 COMMIT 重试，最终版已不含。）

## 一、命令与结果

```bash
python -m pip wheel . --no-deps -w <dir>
python scripts/check_wheel_manifest.py <wheel>          # PASS，59 entries
python -m venv <dir>/venv && <venv>/python -m pip install --no-deps <wheel>
python scripts/wheel_smoke.py --cli <venv>/Scripts/evoguard.exe \
       --workdir <dir>/work                             # 10/10
AGENTLAB_STORE_POOL=1 python scripts/wheel_smoke.py ...  # 10/10
```

| 检查 | 结果 |
|---|---|
| `check_wheel_manifest.py` | **PASS**；缺模块/缺资产/禁用条目均为「无」 |
| 关键模块在制品 | `core.py` / `enterprise/jobs.py` / `v3_gate.py` / `gateway.py` 全在 |
| 制品内源码符号 | `_ConnPool`、`_interprocess_lock`、`只关空闲队列`、`pool_flag`、`ResilientProvider`、`E_CIRCUIT_OPEN`、`MAX_DETAIL_ITEMS`、`real_model_state` 全在 |
| 独立 venv 内开池 | `from evoguard import core` 成功；`pool_stats()` = `{size:8, created:1, discarded:0, idle:1}`，写入 1 行 |
| wheel serve（默认） | 10/10，`/`、`/api/health`、`/api/meta`、`/api/runs`、`/metrics`、`/api/ready` 全 200 |
| wheel serve（开池） | 10/10，同上 |

## 二、`check_wheel_manifest.py` 本轮扩展

原脚本只查「文件在不在清单里」。加了 `SOURCE_SYMBOLS`：直接读 wheel 内的
源码内容，断言第四~六轮修的真 bug 对应的符号都在。
理由：清单检查挡不住「用旧源码构建但文件齐全」的制品。

**可证伪性验证**（不是摆设）：把制品里的 `evoguard/core.py` 换成不含
连接池的旧内容，重新跑检查：

```
源码符号缺失: ['evoguard/core.py: 缺 _ConnPool',
              'evoguard/core.py: 缺 _interprocess_lock',
              'evoguard/core.py: 缺 只关空闲队列',
              'evoguard/core.py: 缺 pool_flag']
== FAIL ==   (exit 1)
```

`REQUIRED` 同时补了 `enterprise/jobs.py` 与 `v3_gate.py`（第三、四轮新增的
模块，之前只在打包结果里、没进验收清单）。CI 的 wheel job 会自动用新版检查。

## 三、边界

- 本机 Windows；Linux wheel 与 GitHub CI 真实运行仍未实测。
- Docker daemon 不可达，镜像层未实测。
- 这里验的是「制品包含且能跑新代码」，不是多副本/长时稳定性。
