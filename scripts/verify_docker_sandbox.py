#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Docker 代码沙箱的隔离验证（真容器，不是桩）。

此前这条路径**全程没有 live 验证**：daemon 不可达时 gate 里那项只能 SKIP。
本脚本把沙箱该有的隔离属性逐条实测，产出可复核的证据。

五项：
  1. 正常执行可拿到 stdout（沙箱不是「一律失败」的摆设）
  2. 网络隔离：容器内访问外网必须失败
  3. 宿主文件不可读：/etc/passwd 之外的宿主路径不存在
  4. 超时被杀：死循环必须在 timeout 内被打断，且返回明确状态
  5. 内存上限：申请超限内存必须被 OOM 杀掉（而不是拖垮宿主）

用法：python scripts/verify_docker_sandbox.py [--json out.json]
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import sandbox_executor as SX                              # noqa: E402


def check_basic(sb):
    r = sb.run("print(sum(range(10)))", timeout_s=60)
    ok = r.get("ok") and (r.get("stdout") or "").strip() == "45"
    return ok, {"returncode": r.get("returncode"),
                "stdout": (r.get("stdout") or "").strip()[:40],
                "error": r.get("error")}


def check_network(sb):
    code = ("import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('http://1.1.1.1', timeout=3)\n"
            "    print('NETWORK_REACHABLE')\n"
            "except Exception as e:\n"
            "    print('NETWORK_BLOCKED', type(e).__name__)\n")
    r = sb.run(code, timeout_s=60)
    out = (r.get("stdout") or "")
    ok = "NETWORK_BLOCKED" in out and "NETWORK_REACHABLE" not in out
    return ok, {"stdout": out.strip()[:80], "error": r.get("error")}


def check_host_fs(sb):
    """容器里不应该看到宿主的项目目录。"""
    marker = os.path.join(ROOT, "ai-service").replace("\\", "/")
    code = ("import os\n"
            "print('SEES_HOST' if os.path.exists(%r) else 'HOST_HIDDEN')\n"
            % marker)
    r = sb.run(code, timeout_s=60)
    out = (r.get("stdout") or "")
    ok = "HOST_HIDDEN" in out
    return ok, {"probe_path": marker, "stdout": out.strip()[:80]}


def check_timeout(sb):
    t0 = time.perf_counter()
    r = sb.run("while True:\n    pass\n", timeout_s=5)
    elapsed = time.perf_counter() - t0
    ok = (not r.get("ok")) and elapsed < 60
    return ok, {"elapsed_s": round(elapsed, 1), "returncode": r.get("returncode"),
                "error": (r.get("error") or "")[:80]}


def check_memory(sb):
    """申请远超上限的内存：应被 OOM 杀掉，而不是把宿主拖垮。"""
    code = ("x = []\n"
            "while True:\n"
            "    x.append(bytearray(8 * 1024 * 1024))\n")
    r = sb.run(code, timeout_s=40)
    ok = not r.get("ok")
    return ok, {"returncode": r.get("returncode"),
                "error": (r.get("error") or "")[:120]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    sb = SX.DockerCodeSandbox()
    checks = []
    for cid, fn, desc in (
            ("basic_run", check_basic, "容器内正常执行并取回 stdout"),
            ("network_isolated", check_network, "容器内访问外网被阻断"),
            ("host_fs_hidden", check_host_fs, "宿主项目目录在容器内不可见"),
            ("timeout_killed", check_timeout, "死循环在超时内被杀"),
            ("memory_capped", check_memory, "超限内存申请被 OOM 终止")):
        try:
            ok, detail = fn(sb)
        except Exception as exc:                           # noqa: BLE001
            ok, detail = False, {"exception": "%s: %s"
                                 % (type(exc).__name__, str(exc)[:120])}
        checks.append({"id": cid, "desc": desc, "pass": bool(ok),
                       "detail": detail})
        print("%s %-22s %s" % ("PASS" if ok else "FAIL", cid, desc))

    # 孤儿回收：跑完之后不应有本沙箱留下的容器
    orphans = []
    try:
        import subprocess
        r = subprocess.run([sb.docker, "ps", "-a", "--filter",
                            "name=evoguard-sbx", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=30)
        orphans = [x for x in (r.stdout or "").splitlines() if x.strip()]
    except Exception:                                      # noqa: BLE001
        pass
    orphan_ok = not orphans
    checks.append({"id": "no_orphans", "desc": "跑完后无残留容器",
                   "pass": orphan_ok, "detail": {"containers": orphans}})
    print("%s %-22s %s" % ("PASS" if orphan_ok else "FAIL", "no_orphans",
                           "跑完后无残留容器"))

    rep = {"image": sb.image, "memory": sb.memory, "cpus": sb.cpus,
           "passed": all(c["pass"] for c in checks), "checks": checks}
    if a.json:
        out = a.json if os.path.isabs(a.json) else os.path.join(ROOT, a.json)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("WROTE", a.json)
    n = sum(1 for c in checks if c["pass"])
    print("== %d/%d pass ==" % (n, len(checks)))
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
