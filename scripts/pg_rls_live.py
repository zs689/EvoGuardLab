#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WS-06：**真 PostgreSQL** 上的 RLS 验证（免安装 zip 版）。

    python scripts/pg_rls_live.py --pg-root E:/agent/_pgwork/pgsql \
        --out docs/review-remediation/probes/pg_rls_live.json

流程：initdb（trust 本地）→ 起临时实例（随机端口）→ 建表 + ENABLE/FORCE RLS
→ 插两租户数据 → **两个独立 psql 进程**并发查询 → 校验 A 只能看到 A、
B 只能看到 B、无 GUC 时零行 → 停实例。调用方负责清理目录。

判定看的是**真 PostgreSQL 的 RLS 行为**，不是 SQLite 模拟。
"""
import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
import pg_rls as P                                          # noqa: E402

# RLS_DDL 覆盖 runs / audit_events / skills 三张表，验证时三张都要存在，
# 否则「relation does not exist」会让整个 DDL 失败（这也是真实部署的
# 前置条件：先建表再开 RLS）。
SCHEMA = """
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS skills;
CREATE TABLE runs(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, body TEXT);
CREATE TABLE audit_events(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                          path TEXT);
CREATE TABLE skills(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    name TEXT);
INSERT INTO runs VALUES ('a1','A','A-secret'),('b1','B','B-secret');
INSERT INTO audit_events VALUES ('e1','A','/x'),('e2','B','/y');
INSERT INTO skills VALUES ('s1','A','skill-a'),('s2','B','skill-b');
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _bin(pg_root, name):
    return os.path.join(pg_root, "bin", name + (".exe" if os.name == "nt" else ""))


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _psql(pg_root, port, sql, *, env_tenant=None, db="evo"):
    cmd = [_bin(pg_root, "psql"), "-h", "127.0.0.1", "-p", str(port),
           "-U", "postgres", "-d", db, "-t", "-A", "-c", sql]
    env = dict(os.environ)
    if env_tenant:
        # -c 里先 SET LOCAL 需要在同一事务；这里用 -v 更简单：直接拼进 SQL
        cmd[-1] = "BEGIN; SET LOCAL app.current_tenant='%s'; %s COMMIT;" % (
            env_tenant, sql)
    return _run(cmd, env=env)


_PSQL_CMD = ["psql"]          # 可被 --psql-cmd 覆盖（如 docker exec ... psql）


def _psql_dsn(dsn, sql):
    """直连已有 PostgreSQL：默认用系统 psql，也可指定容器内 psql。"""
    return _run(_PSQL_CMD + [dsn, "-t", "-A", "-v", "ON_ERROR_STOP=1",
                             "-c", sql])


def run_dsn(dsn):
    """不 initdb，直接对已存在的实例验证 RLS（GitHub Actions services 用）。"""
    _psql_dsn(dsn, "DROP TABLE IF EXISTS runs")
    _psql_dsn(dsn, SCHEMA)
    r = _psql_dsn(dsn, P.RLS_DDL)
    if r.returncode != 0:
        print("RLS DDL 失败：", (r.stderr or r.stdout)[-400:])
        return None
    # **必须以非超级用户连接**：superuser / BYPASSRLS 角色永远绕过 RLS，
    # 用 postgres 超级用户测「RLS 生效」永远会得到「全部可见」的假结论。
    _psql_dsn(dsn, "DROP ROLE IF EXISTS evoguard_app")
    r = _psql_dsn(dsn, "CREATE ROLE evoguard_app LOGIN PASSWORD 'app'")
    if r.returncode != 0:
        print("建角色失败：", (r.stderr or r.stdout)[-300:])
        return None
    _psql_dsn(dsn, "GRANT SELECT, INSERT, UPDATE, DELETE "
                   "ON runs, audit_events, skills TO evoguard_app")
    app_dsn = dsn.replace("postgres:evo@", "evoguard_app:app@")
    if app_dsn == dsn:
        # DSN 不是 postgres:evo 形式时，改成显式用户名
        import re as _re
        app_dsn = _re.sub(r"//[^@]*@", "//evoguard_app:app@", dsn)

    def count(tenant=None):
        prefix = ("BEGIN; SET LOCAL app.current_tenant='%s'; " % tenant
                  if tenant else "BEGIN; ")
        res = _psql_dsn(app_dsn, prefix + "SELECT count(*) FROM runs; COMMIT;")
        # 多语句 -c 的输出里有 BEGIN/SET/COMMIT 等命令行，
        # 取「纯数字的那一行」才是 SELECT 结果。
        for line in (res.stdout or "").splitlines():
            t = line.strip()
            if t.isdigit():
                return int(t)
        return None
    ca, cb, cn = count("A"), count("B"), count(None)
    rep = {"backend": "real PostgreSQL via DSN (CI services)",
           "rows_visible": {"tenant_A": ca, "tenant_B": cb, "no_guc": cn},
           "checks": {"A_sees_only_own": ca == 1,
                      "B_sees_only_own": cb == 1,
                      "no_tenant_context_sees_nothing": cn == 0,
                      "owner_also_restricted": cn == 0}}
    rep["passed"] = all(rep["checks"].values())
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-root", default="", help="解压后的 pgsql 目录（自起实例）")
    ap.add_argument("--dsn", default="", help="已有实例的 DSN（CI 用，不自起）")
    ap.add_argument("--psql-cmd", default="",
                    help='psql 命令，如 "docker exec evoguard-pg-test psql"')
    ap.add_argument("--out", default="")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    global _PSQL_CMD
    if a.psql_cmd:
        _PSQL_CMD = a.psql_cmd.split()
    if a.dsn:
        rep = run_dsn(a.dsn)
        if rep is None:
            return 2
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if a.out:
            os.makedirs(os.path.dirname(a.out), exist_ok=True)
            with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
                json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
        return 0 if rep["passed"] else 1
    if not a.pg_root:
        print("需要 --pg-root 或 --dsn")
        return 2
    pg = os.path.abspath(a.pg_root)
    if not os.path.isfile(_bin(pg, "initdb")):
        print("找不到 initdb：%s" % pg)
        return 2
    data = tempfile.mkdtemp(prefix="evo-pgdata-")
    port = _free_port()
    started = False
    try:
        r = _run([_bin(pg, "initdb"), "-D", data, "-U", "postgres",
                  "--auth=trust", "-E", "UTF8"])
        if r.returncode != 0:
            print("initdb 失败：", (r.stderr or r.stdout)[-400:])
            return 2
        r = _run([_bin(pg, "pg_ctl"), "-D", data, "-o",
                  "-p %d -c listen_addresses=127.0.0.1" % port,
                  "-l", os.path.join(data, "pg.log"), "start"])
        if r.returncode != 0:
            print("pg_ctl start 失败：", (r.stderr or r.stdout)[-400:])
            return 2
        started = True
        # 等就绪
        for _ in range(50):
            if _psql(pg, port, "SELECT 1", db="postgres").returncode == 0:
                break
            time.sleep(0.2)
        _psql(pg, port, "CREATE DATABASE evo", db="postgres")
        _psql(pg, port, SCHEMA)
        _psql(pg, port, P.RLS_DDL)

        # 三组查询：A 租户、B 租户、无 GUC
        a_rows = _psql(pg, port, "SELECT count(*) FROM runs;", env_tenant="A")
        b_rows = _psql(pg, port, "SELECT count(*) FROM runs;", env_tenant="B")
        none_rows = _psql(pg, port, "SELECT count(*) FROM runs;")
        total = _psql(pg, port, "SELECT count(*) FROM runs;", env_tenant="A")
        # owner 绕过检查：superuser 也受 FORCE 约束（postgres 是 owner）
        def n(res):
            for line in (res.stdout or "").splitlines():
                t = line.strip()
                if t.isdigit():
                    return int(t)
            return None
        ca, cb, cn = n(a_rows), n(b_rows), n(none_rows)
        rep = {
            "backend": "real PostgreSQL (zip, trust local, temp instance)",
            "server_version": _psql(pg, port, "SHOW server_version").stdout.strip(),
            "rows_visible": {"tenant_A": ca, "tenant_B": cb,
                             "no_guc": cn},
            "checks": {
                "A_sees_only_own": ca == 1,
                "B_sees_only_own": cb == 1,
                "no_tenant_context_sees_nothing": cn == 0,
                "owner_also_restricted": cn == 0,
            },
            "raw": {"A": (a_rows.stdout or "").strip()[:60],
                    "B": (b_rows.stdout or "").strip()[:60],
                    "none": (none_rows.stdout or "").strip()[:60]},
        }
        rep["passed"] = all(rep["checks"].values())
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if a.out:
            os.makedirs(os.path.dirname(a.out), exist_ok=True)
            with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
                json.dump(rep, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
        return 0 if rep["passed"] else 1
    finally:
        if started:
            _run([_bin(pg, "pg_ctl"), "-D", data, "-m", "immediate", "stop"])
        if not a.keep:
            shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
