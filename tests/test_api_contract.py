# -*- coding: utf-8 -*-
"""EG-025：机器可读契约不能和代码漂移。

用 AST 读 `ROUTES`，**不导入 app.py**（导入会建 Store、起全局控制对象，
测试不该有这种副作用）。逐条比对方法+路径，再核对六字段契约与错误码。
"""
import ast
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ai-service", "api", "app.py")
CONTRACT = os.path.join(ROOT, "docs", "api-contract-v1.json")


def _routes_from_source():
    # FINAL-04-T1：ROUTES 字面量随模块级内容搬到 app_core.py
    routes_src = os.path.join(os.path.dirname(APP), "app_core.py")
    if not os.path.isfile(routes_src):
        routes_src = APP
    with open(routes_src, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "ROUTES"
                   for t in node.targets):
            continue
        return ast.literal_eval(node.value)
    raise AssertionError("app.py 里找不到 ROUTES 字面量")


def _to_path(pattern):
    p = pattern.strip("^$")
    name = "{job_id}" if p.startswith("/api/jobs/") else "{rid}"
    p = re.sub(r"\(\[.*?\]\+\)", name, p)
    return p


class TestAPIContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding="utf-8") as f:
            cls.c = json.load(f)
        # FINAL-04-T1：源码已拆分到多个模块，契约断言读取合并源码。
        src_parts = []
        for name in ("app.py", "app_core.py", "routes_agent.py",
                     "routes_enterprise.py"):
            with open(os.path.join(os.path.dirname(APP), name),
                      encoding="utf-8") as f:
                src_parts.append(f.read())
        cls.src = "\n".join(src_parts)
        cls.code_routes = {(m, _to_path(p)) for m, p, _ in _routes_from_source()}
        cls.doc_routes = {(r["method"], r["path"])
                          for r in cls.c["routes"]}

    def test_every_code_route_is_documented(self):
        missing = self.code_routes - self.doc_routes
        self.assertEqual(missing, set(),
                         "代码里有路由但契约没写：%s" % sorted(missing))

    def test_every_documented_route_exists(self):
        extra = self.doc_routes - self.code_routes
        self.assertEqual(extra, set(),
                         "契约声称有但代码没有：%s" % sorted(extra))

    def test_six_field_contract_is_frozen(self):
        fields = [x["field"] for x in self.c["six_field_contract"]]
        self.assertEqual(fields, ["detected", "decision", "attempted",
                                  "executed", "effect", "reason"])
        self.assertEqual(self.c["compatibility"]["frozen_fields"], fields)

    def test_error_codes_appear_in_source(self):
        missing = [e["code"] for e in self.c["errors"]["codes"]
                   if e["code"] not in self.src]
        self.assertEqual(missing, [],
                         "契约列了代码里不存在的错误码：%s" % missing)

    def test_body_limit_matches_source(self):
        self.assertIn("AGENTLAB_MAX_BODY_BYTES", self.src)
        self.assertEqual(self.c["limits"]["max_body_bytes"], 65536)
        self.assertIn("65536", self.src)

    def test_page_limit_matches_source(self):
        self.assertIn("AGENTLAB_MAX_PAGE_SIZE", self.src)
        self.assertEqual(self.c["limits"]["max_page_size"], 200)

    def test_pagination_contract_matches_handler(self):
        self.assertIn("next_cursor", self.src)
        self.assertIn("ORDER BY r.started_at DESC, r.id DESC", self.src)
        self.assertTrue(self.c["pagination"]["endpoint"] == "/api/runs")

    def test_probe_routes_are_marked(self):
        probes = {r["path"] for r in self.c["routes"]
                  if r["kind"] == "probe"}
        self.assertIn("/api/live", probes)
        self.assertIn("/api/ready", probes)


if __name__ == "__main__":
    unittest.main()
