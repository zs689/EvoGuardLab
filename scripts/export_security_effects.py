# -*- coding: utf-8 -*-
"""把 `security_effects` 导成 jsonl，交给 CI 决策读。

为什么要有这一步：报告层面的「检测到 X 条」和「真的拦住了」是两件事。
副作用表记的是**真实执行点**发生了什么，CI 只认这个。
`decision=deny` 而 `executed=1` 就是「说了拦、其实跑」，必须阻断。

用法：
    python scripts/export_security_effects.py --db data/evoguard.db --out eff.jsonl
    python scripts/export_security_effects.py --db ... --run-id run_xxx --out ...
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import core      # noqa: E402
import security as S  # noqa: E402


def export(db_path, out_path, run_id=None, limit=100000):
    if not os.path.isfile(db_path):
        raise SystemExit("找不到数据库：%s" % db_path)
    store = core.Store(db_path)
    try:
        rows = S.list_effects(store, run_id=run_id, limit=limit)
    finally:
        store.close()
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    denied_exec = sum(1 for r in rows
                      if r.get("executed") and (r.get("decision") or "")
                      in ("deny", "encode", "wrap"))
    return {"rows": len(rows), "denied_but_executed": denied_exec,
            "out": out_path}


def main(argv=None):
    ap = argparse.ArgumentParser(description="导出 security_effects 供 CI 判定")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "evoguard.db"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args(argv)
    r = export(args.db, args.out, args.run_id)
    print("[√] %s：%d 条后果记录，其中「判拒绝但仍执行」%d 条"
          % (r["out"], r["rows"], r["denied_but_executed"]))
    return r


if __name__ == "__main__":
    main()
