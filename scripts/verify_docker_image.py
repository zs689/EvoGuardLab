#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EG-003/EG-004：Docker 镜像层 + Linux wheel 实测（需要可用 daemon）。

    python scripts/verify_docker_image.py --image evoguardlab:verify --json <path>

全部在**本机**完成：不 push 镜像、不建仓库、不上传文件。
所有 docker 调用都用参数列表（shell=False）—— Windows 的 cmd.exe 不认
单引号，用字符串拼命令会让 `find` 失败却输出空，从而产生**假绿**。
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTAINER = "evo-verify-runtime"
WHEEL_CT = "evo-verify-wheel"
VOLUME = "evo-verify-data"
PORT = 18770


def run(cmd, timeout=180):
    """cmd 是参数列表，避开所有 shell 引号问题。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout


def cleanup():
    for name in (CONTAINER, WHEEL_CT):
        run(["docker", "rm", "-f", name], timeout=30)
    run(["docker", "volume", "rm", VOLUME], timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="evoguardlab:verify")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    img = a.image
    checks = []
    cleanup()

    try:
        # 1. 镜像层里没有本地敏感数据（rc 必须为 0 且 stdout 为空）
        rc, out, err = run([
            "docker", "run", "--rm", "--entrypoint", "find", img,
            "/app", "-xdev", "(",
            "-name", ".git", "-o", "-name", ".env", "-o",
            "-name", "*.db", "-o", "-name", "*.db-wal", "-o",
            "-path", "*/data/external*", "-o",
            "-path", "*/data/tenants*", ")", "-print"], timeout=120)
        checks.append({
            "id": "no-leaked-files", "pass": rc == 0 and out == "",
            "desc": "镜像层无 .git/.env/*.db/语料/租户数据",
            "rc": rc, "found": out[:400], "stderr": err[-200:],
        })

        # 1b. 历史层里也不能有 `COPY . /app`（.dockerignore 的第一道防线）
        hrc, hout, herr = run([
            "docker", "history", "--no-trunc", "--format",
            "{{.CreatedBy}}", img], timeout=60)
        broad = [ln for ln in hout.splitlines()
                 if re.search(r"(COPY|ADD)\s+\.\s+/", ln)]
        layers = [ln for ln in hout.splitlines() if ln.strip()]
        checks.append({
            "id": "no-broad-copy",
            "pass": hrc == 0 and not broad and len(layers) > 0,
            "desc": "历史层无 `COPY . /app`（Dockerfile 用显式 COPY 清单）",
            "layer_count": len(layers), "broad_copy_layers": broad[:3],
            "stderr": herr[-200:],
        })

        # 2. 非 root
        rc, out, err = run(["docker", "run", "--rm", "--entrypoint",
                            "id", img], timeout=60)
        checks.append({
            "id": "non-root", "pass": rc == 0 and out.startswith("uid=10001("),
            "desc": "运行用户非 root（uid 10001）",
            "id_output": out, "stderr": err[-200:],
        })

        # 3. 只读根 + tmpfs + 卷 + 服务 + healthcheck
        rc, out, err = run([
            "docker", "run", "-d", "--name", CONTAINER,
            "--read-only", "--tmpfs", "/tmp",
            "-p", "%d:8770" % PORT, "-v", "%s:/app/data" % VOLUME, img],
            timeout=120)
        started = rc == 0
        health, live_http = "not_started", "not_started"
        data_in_volume, image_layer_data = "n/a", "n/a"
        if started:
            for _ in range(30):
                hc, ho, _ = run([
                    "docker", "inspect", "--format",
                    "{{.State.Health.Status}}", CONTAINER], timeout=30)
                health = ho if hc == 0 else "unknown"
                if health in ("healthy", "unhealthy"):
                    break
                time.sleep(2)
            lc, lo, le = run([
                "docker", "exec", CONTAINER, "python", "-c",
                "import urllib.request as u;"
                "print(u.urlopen('http://127.0.0.1:8770/api/live',"
                "timeout=2).status)"], timeout=60)
            live_http = lo if lc == 0 else "error: " + le[-200:]
            # /api/live 按设计不碰 DB，必须再打一次会建库的请求
            dc, do, de = run([
                "docker", "exec", CONTAINER, "python", "-c",
                "import urllib.request as u;"
                "print(u.urlopen('http://127.0.0.1:8770/api/health',"
                "timeout=5).status)"], timeout=60)
            db_trigger = do if dc == 0 else "error: " + de[-200:]
            vc, vo, ve = run(["docker", "exec", CONTAINER,
                              "ls", "-1", "/app/data"], timeout=60)
            data_in_volume = vo if vc == 0 else "error: " + ve[-200:]
            ec, eo, ee = run([
                "docker", "run", "--rm", "--entrypoint", "ls", img,
                "-1", "/app/data"], timeout=60)
            image_layer_data = eo if ec == 0 else "error: " + ee[-200:]
        checks.append({
            "id": "readonly-root-service",
            "pass": bool(started and health == "healthy"
                         and live_http == "200"),
            "desc": "--read-only + tmpfs + 卷下服务健康、/api/live 返回 200",
            "health": health, "live_status": live_http,
            "db_trigger_status": db_trigger,
            "start_error": err[-300:] if not started else "",
        })
        checks.append({
            "id": "volume-separated",
            "pass": bool(started and "evoguard.db" in data_in_volume
                         and image_layer_data == ""),
            "desc": "业务库落在卷、镜像层 /app/data 为空",
            "data_in_volume": data_in_volume,
            "image_layer_data": image_layer_data,
        })

        # 4. Linux wheel：全新 python:3.11-slim 容器里安装并 import
        # 用系统临时目录，别在仓库里留 dist_*
        wheel_dir = tempfile.mkdtemp(prefix="evo-docker-wheel-")
        wrc, wo, we = run([sys.executable, "-m", "pip", "wheel", ".",
                           "--no-deps", "-w", wheel_dir], timeout=300)
        whls = [f for f in os.listdir(wheel_dir) if f.endswith(".whl")]
        if wrc != 0 or not whls:
            checks.append({"id": "linux-wheel", "pass": False,
                           "desc": "wheel 构建失败", "stderr": we[-300:]})
        else:
            whl = os.path.join(wheel_dir, whls[0])
            r1 = run(["docker", "create", "--name", WHEEL_CT,
                      "python:3.11-slim", "sleep", "3600"], timeout=60)
            r2 = run(["docker", "cp", whl,
                      "%s:/tmp/%s" % (WHEEL_CT, whls[0])], timeout=60)
            r3 = run(["docker", "start", WHEEL_CT], timeout=60)
            r4 = run(["docker", "exec", WHEEL_CT, "pip", "install",
                      "--no-cache-dir", "/tmp/%s" % whls[0]], timeout=300)
            r5 = run(["docker", "exec", WHEEL_CT, "python", "-c",
                      "import evoguard, evoguard.enterprise, "
                      "evoguard_inspect; print('linux-import-ok')"],
                     timeout=60)
            r6 = run(["docker", "exec", WHEEL_CT, "evoguard", "--help"],
                     timeout=60)
            ok = all(r[0] == 0 for r in (r1, r2, r3, r4, r5, r6)) \
                and "linux-import-ok" in r5[1] and len(r6[1]) > 20
            checks.append({
                "id": "linux-wheel", "pass": bool(ok),
                "desc": "Linux 容器内安装 wheel、import 三个包、CLI 可用",
                "steps_rc": [r[0] for r in (r1, r2, r3, r4, r5, r6)],
                "pip_stdout": r4[1][-300:], "pip_stderr": r4[2][-300:],
                "import": r5[1][-200:], "import_err": r5[2][-300:],
                "cli_head": r6[1][:200], "cli_err": r6[2][-200:],
                "wheel": whls[0],
            })
    finally:
        cleanup()
        if "wheel_dir" in locals():
            shutil.rmtree(wheel_dir, ignore_errors=True)

    passed = sum(1 for c in checks if c["pass"])
    report = {
        "version": 2,
        "image": img,
        "method": "本机 Docker Desktop（Linux 容器），参数列表调用，不 push 不上传",
        "passed": passed == len(checks),
        "summary": {"total": len(checks), "pass": passed,
                    "fail": len(checks) - passed},
        "checks": checks,
    }
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
            f.write("\n")
    for c in checks:
        print("%s %-22s %s" % ("PASS" if c["pass"] else "FAIL",
                               c["id"], c["desc"]))
    print("== %d/%d pass ==" % (passed, len(checks)))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
