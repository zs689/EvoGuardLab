# -*- coding: utf-8 -*-
"""EG-022：用合成敏感标记验证「日志/指标/导出/详情都不暴露原值」。

不是读代码确认「有白名单」，而是真的把标记塞进请求，再从每个出口找一遍。
"""
import contextlib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import live_client as L                                    # noqa: E402
import service_controls as SC                             # noqa: E402

CANARY = "CANARY_SECRET_9f2c7b1e"


class TestDataGovernance(unittest.TestCase):
    def test_structured_log_whitelist_drops_sensitive_values(self):
        c = SC.ServiceControls.from_env({"AGENTLAB_LOG_QUIET": ""})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            c.log({"request_id": "r1", "method": "POST", "path": "/api/run",
                   "body": CANARY, "token": CANARY,
                   "Authorization": CANARY, "password": CANARY,
                   "status": 200, "actor": "alice", "decision": "allowed"})
        out = buf.getvalue()
        self.assertNotIn(CANARY, out, "结构化日志泄露了请求正文/凭据")
        self.assertIn("request_id", out)
        for banned in ("body", "token", "password", "Authorization"):
            self.assertNotIn('"%s"' % banned, out)

    def test_canary_not_observable_from_any_export(self):
        """真起服务：标记塞进 POST body，再逐个出口检查。"""
        token = "gov-token"
        env = {"AGENTLAB_API_TOKEN": token,
               "AGENTLAB_RATE_LIMIT": "100000",
               "AGENTLAB_LOG_QUIET": ""}
        svc_obj = L.Service(env)
        with svc_obj as svc:
            h = {"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"}
            body = ('{"agent_type":"order","task_id":"A1001",'
                    '"fault_type":"silent_corruption","mode":"D",'
                    '"canary":"%s","note":"%s"}'
                    % (CANARY, CANARY)).encode("utf-8")
            st, _, raw = L.http(svc.port, "POST", "/api/run", h, body=body)
            self.assertEqual(st, 200, raw[:200])

            # 出口 1：指标
            _, _, m = L.http(svc.port, "GET", "/metrics", h)
            self.assertNotIn(CANARY.encode(), m, "Prometheus 指标泄露")

            # 出口 2：诊断/存活响应
            for path in ("/api/health", "/api/live", "/api/ready", "/api/meta"):
                _, _, b = L.http(svc.port, "GET", path, h)
                self.assertNotIn(CANARY.encode(), b, "%s 泄露" % path)

            # 出口 3：列表与详情（可「导出」的两条路径）
            _, _, runs = L.http(svc.port, "GET", "/api/runs?limit=5", h)
            self.assertNotIn(CANARY.encode(), runs, "运行列表泄露")
            import json
            items = json.loads(runs.decode("utf-8")).get("items", [])
            for r in items[:1]:
                _, _, detail = L.http(
                    svc.port, "GET", "/api/runs/" + r["id"], h)
                self.assertNotIn(CANARY.encode(), detail, "运行详情泄露")

        # 出口 4：服务端结构化日志（进程退出后读文件）
        log = svc_obj.tail(200000)
        self.assertNotIn(CANARY, log, "服务端日志泄露")

    def test_governance_document_declares_classification(self):
        p = os.path.join(ROOT, "docs", "data-governance.md")
        self.assertTrue(os.path.isfile(p), "缺数据分类与保留策略文档")
        with io.open(p, encoding="utf-8") as f:
            t = f.read()
        for key in ("审计", "轨迹", "日志", "缓存", "保留期", "删除", "导出",
                    "冻结", "加密", "合成敏感标记"):
            self.assertIn(key, t, "文档缺字段：%s" % key)


if __name__ == "__main__":
    unittest.main()
