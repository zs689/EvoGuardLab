# -*- coding: utf-8 -*-
"""企业形态模块的守门测试：多租户、可观测、审计迁移、韧性、TLS、自评。

守的是「真的能工作」与「边界没被夸大」：
  1. 租户从请求头解析、配额按租户独立、SQL 缺租户条件必须被拦；
  2. 指标是合法 Prometheus 文本、日志带 tenant_id、采样不采正文；
  3. 审计写入强制 tenant_id、迁移幂等、保留清理可算；
  4. 熔断三态可复现、超时预算可扣减、SLO 分母为 0 时返回 None；
  5. 不可信对端的 X-Forwarded-* 必须被忽略；
  6. scorecard 的每一项都有证据字段，缺证据记 0 并把缺口写出来。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

from enterprise import audit as A            # noqa: E402
from enterprise import observability as O    # noqa: E402
from enterprise import resilience as R       # noqa: E402
from enterprise import scorecard as SC       # noqa: E402
from enterprise import tenancy as T          # noqa: E402
from enterprise import tls_proxy as P        # noqa: E402


class TestTenancy(unittest.TestCase):
    def test_header_resolver_reads_tenant_and_actor(self):
        ctx = T.HeaderResolver().resolve({"X-Tenant-Id": "acme",
                                          "X-Actor": "alice",
                                          "X-Scopes": "read,write"})
        self.assertEqual(ctx.tenant_id, "acme")
        self.assertEqual(ctx.actor, "alice")
        self.assertTrue(ctx.has_scope("read"))

    def test_header_resolver_rejects_bad_tenant(self):
        self.assertIsNone(T.HeaderResolver().resolve({"X-Tenant-Id": "../etc"}))
        self.assertIsNone(T.HeaderResolver(allowed_tenants=["acme"])
                          .resolve({"X-Tenant-Id": "other"}))

    def test_missing_header_falls_back_to_default(self):
        self.assertEqual(T.HeaderResolver().resolve({}).tenant_id, "default")

    def test_quota_is_per_tenant(self):
        reg = T.TenantRegistry([T.TenantPolicy("a", request_quota=2)])
        q = T.TenantQuota(reg)
        self.assertEqual([q.check("a")[0] for _ in range(3)],
                         [True, True, False])
        self.assertTrue(q.check("b")[0], "b 的配额不该被 a 用掉")

    def test_unknown_tenant_gets_minimal_policy(self):
        reg = T.TenantRegistry()
        self.assertLessEqual(reg.policy("ghost").request_quota, 10)
        self.assertFalse(reg.allows_tool("ghost", "shell.exec"))

    def test_require_tenant_filter_rejects_unfiltered_sql(self):
        with self.assertRaises(ValueError):
            T.require_tenant_filter("SELECT * FROM audit_events")
        with self.assertRaises(ValueError):
            T.require_tenant_filter("SELECT * FROM audit_events WHERE tenant_id = ?")
        self.assertTrue(T.require_tenant_filter(
            "SELECT * FROM audit_events WHERE tenant_id = ?", ("acme",)))

    def test_scope_query_appends_predicate(self):
        sql, params = T.scope_query("SELECT id FROM t", "acme")
        self.assertIn("tenant_id = ?", sql)
        self.assertEqual(params, ("acme",))
        with self.assertRaises(ValueError):
            T.scope_query("DELETE FROM t", "acme")


class TestObservability(unittest.TestCase):
    def test_metrics_render_prometheus(self):
        m = O.Metrics()
        m.inc("evoguard_requests_total", {"route": "health"})
        m.set_gauge("evoguard_up", 1)
        m.observe("evoguard_latency_seconds", 0.02)
        text = m.render_prometheus()
        self.assertIn("# TYPE evoguard_requests_total counter", text)
        self.assertIn('route="health"', text)
        self.assertIn("evoguard_up 1", text)
        self.assertIn("evoguard_latency_seconds_bucket", text)
        self.assertIn('le="+Inf"', text)

    def test_log_pipeline_injects_tenant_and_timestamp(self):
        sink = O.MemorySink()
        pipe = O.LogPipeline([sink])
        pipe.emit({"request_id": "r1"}, tenant_id="acme")
        self.assertEqual(sink.records[0]["tenant_id"], "acme")
        self.assertIn("ts", sink.records[0])

    def test_log_pipeline_survives_broken_sink(self):
        class Boom(O.LogSink):
            def emit(self, record):
                raise RuntimeError("disk full")
        sink = O.MemorySink()
        pipe = O.LogPipeline([Boom(), sink])
        pipe.emit({"a": 1})
        self.assertEqual(len(sink.records), 1)

    def test_jsonl_sink_writes_single_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "audit.jsonl")
            sink = O.JsonlFileSink(p)
            sink.emit({"a": 1})
            sink.emit({"b": 2})
            with open(p, encoding="utf-8") as f:
                lines = f.read().strip().split("\n")
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].startswith("{") and lines[0].endswith("}"))

    def test_tracer_records_duration(self):
        tr = O.Tracer()
        with tr.span("probe", x=1):
            pass
        spans = tr.export()
        self.assertEqual(spans[0]["name"], "probe")
        self.assertIsNotNone(spans[0]["duration_ms"])
        self.assertEqual(spans[0]["attrs"]["x"], 1)

    def test_tracer_records_error(self):
        tr = O.Tracer()
        with self.assertRaises(ValueError):
            with tr.span("boom"):
                raise ValueError("x")
        self.assertEqual(tr.export()[0]["attrs"]["error"], "ValueError")

    def test_sampler_drops_body(self):
        s = O.TrafficSampler(rate=1.0)
        out = s.maybe_sample({"path": "/x", "body": "SECRET", "status": 200})
        self.assertEqual(out, {"path": "/x", "status": 200})


class TestAudit(unittest.TestCase):
    def test_migration_reaches_schema_version(self):
        s = A.AuditStore(":memory:")
        self.assertEqual(s.version, A.SCHEMA_VERSION)
        s.close()

    def test_migration_is_idempotent(self):
        s = A.AuditStore(":memory:")
        self.assertEqual(s.migrate(), A.SCHEMA_VERSION)
        self.assertEqual(s.migrate(), A.SCHEMA_VERSION)
        s.close()

    def test_write_and_query_by_tenant(self):
        s = A.AuditStore(":memory:")
        s.emit({"tenant_id": "a", "request_id": "r1", "path": "/x"})
        s.emit({"tenant_id": "b", "request_id": "r2", "path": "/y"})
        self.assertEqual(s.count("a"), 1)
        self.assertEqual([r["request_id"] for r in s.query("a")], ["r1"])
        s.close()

    def test_missing_tenant_rejected(self):
        s = A.AuditStore(":memory:")
        with self.assertRaises(ValueError):
            s.emit({"request_id": "r"})
        s.close()

    def test_purge_before(self):
        s = A.AuditStore(":memory:")
        s.emit({"tenant_id": "a", "request_id": "old", "ts": 1.0})
        s.emit({"tenant_id": "a", "request_id": "new", "ts": 100.0})
        self.assertEqual(s.purge_before(50.0), 1)
        self.assertEqual(s.count("a"), 1)
        s.close()

    def test_memory_sink_enforces_tenant(self):
        m = A.MemoryAuditSink()
        m.emit({"tenant_id": "a", "request_id": "r"})
        self.assertEqual(len(m.events), 1)
        with self.assertRaises(ValueError):
            m.emit({"request_id": "r2"})


class TestAuditTamperEvidence(unittest.TestCase):
    """哈希链的意义全在「被改了能不能发现」。检测不到就等于没有。"""

    def _store(self):
        s = A.AuditStore(":memory:")
        for i in range(5):
            s.emit({"tenant_id": "t", "request_id": "r%d" % i,
                    "path": "/p", "status": 200, "decision": "ok", "ts": 100.0 + i})
        return s

    def test_clean_chain_verifies(self):
        s = self._store()
        ok, info = s.verify()
        self.assertTrue(ok, info)
        self.assertEqual(info["checked"], 5)
        self.assertEqual(info["unhashed"], 0)
        self.assertIsNone(info["first_bad_id"])
        s.close()

    def test_modified_row_is_detected(self):
        s = self._store()
        # 把被拒的请求改成通过 —— 这正是掩盖事故最省事的做法
        s._conn.execute("UPDATE audit_events SET status=500 WHERE id=3")
        s._conn.commit()
        ok, info = s.verify()
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "hash_mismatch")
        self.assertEqual(info["first_bad_id"], 3)
        s.close()

    def test_deleted_row_breaks_the_chain(self):
        s = self._store()
        s._conn.execute("DELETE FROM audit_events WHERE id=2")
        s._conn.commit()
        ok, info = s.verify()
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "prev_mismatch")
        self.assertEqual(info["first_bad_id"], 3)
        s.close()

    def test_unchanged_rows_still_verify_after_new_writes(self):
        s = self._store()
        s.emit({"tenant_id": "t", "request_id": "r9", "ts": 200.0})
        ok, info = s.verify()
        self.assertTrue(ok, info)
        self.assertEqual(info["checked"], 6)
        s.close()

    def test_legacy_rows_are_reported_not_assumed_ok(self):
        """迁移前的老行没有哈希：如实计入 unhashed，不装作校验过。"""
        s = A.AuditStore(":memory:")
        s._conn.execute(
            "INSERT INTO audit_events(ts,tenant_id,request_id,actor,method,"
            "path,status,decision,detail,latency_ms) "
            "VALUES (1.0,'t','legacy','anon','GET','/x',200,'ok',NULL,NULL)")
        s._conn.commit()
        ok, info = s.verify()
        self.assertTrue(ok)
        self.assertEqual(info["checked"], 0)
        self.assertEqual(info["unhashed"], 1)
        s.close()


class TestAuditRetention(unittest.TestCase):
    """EG-007 验收组合：合法清理与篡改必须给出不同结果。"""

    def _store(self, rows):
        s = A.AuditStore(":memory:")
        for i, (tenant, ts) in enumerate(rows):
            s.emit({"tenant_id": tenant, "request_id": "r%d" % i,
                    "ts": ts, "status": 200})
        return s

    def test_retention_leaves_chain_verifiable_with_anchor(self):
        s = self._store([("t", 1.0), ("t", 2.0), ("t", 3.0)])
        self.assertEqual(s.purge_before(2.0), 1)
        ok, info = s.verify()
        self.assertTrue(ok, info)
        self.assertEqual(info["archived_gaps"], 1)
        self.assertEqual(len(s.anchors()), 1)
        self.assertEqual(s.anchors()[0]["deleted_rows"], 1)
        s.close()

    def test_retention_is_per_tenant(self):
        rows = [("a", 1.0), ("b", 1.0), ("a", 3.0), ("b", 3.0)]
        s = self._store(rows)
        self.assertEqual(s.purge_before(2.0, tenant_id="a"), 1)
        self.assertEqual(s.count("a"), 1)
        self.assertEqual(s.count("b"), 2)
        ok, info = s.verify()
        self.assertTrue(ok, info)
        self.assertEqual(info["archived_gaps"], 1)
        s.close()

    def test_out_of_order_timestamps_produce_multiple_segments(self):
        # 时间乱序会让「按 ts 删」变成不连续的 id 区间
        rows = [("t", 1.0), ("t", 9.0), ("t", 1.5), ("t", 9.5), ("t", 2.0)]
        s = self._store(rows)
        self.assertEqual(s.purge_before(3.0), 3)
        ok, info = s.verify()
        self.assertTrue(ok, info)
        self.assertEqual(info["archived_gaps"], 2)
        s.close()

    def test_forged_anchor_does_not_cover_a_gap(self):
        """随便插一条锚点不能把手工删除洗白：哈希必须对得上。"""
        s = self._store([("t", 10.0), ("t", 20.0), ("t", 30.0)])
        s._conn.execute("DELETE FROM audit_events WHERE id=2")
        s._conn.execute(
            "INSERT INTO audit_anchors(after_id,anchor_hash,deleted_rows,"
            "created_at,tenant_id,note) VALUES (3,'forged',1,1.0,'t','fake')")
        s._conn.commit()
        ok, info = s.verify()
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "prev_mismatch")
        s.close()

    def test_retention_boundary_is_exclusive(self):
        s = self._store([("t", 1.0), ("t", 2.0), ("t", 3.0)])
        self.assertEqual(s.purge_before(2.0), 1)
        self.assertEqual(s.count(), 2)
        s.close()


class TestResilience(unittest.TestCase):
    def test_breaker_opens_and_recovers(self):
        now = [1000.0]
        b = R.CircuitBreaker(failure_threshold=2, cooldown_s=10,
                             clock=lambda: now[0])
        b.record(False)
        self.assertEqual(b.state, "closed")
        b.record(False)
        self.assertEqual(b.state, "open")
        self.assertFalse(b.allow())
        now[0] += 11
        self.assertTrue(b.allow())
        self.assertEqual(b.state, "half_open")
        b.record(True)
        self.assertEqual(b.state, "closed")

    def test_half_open_failure_reopens(self):
        now = [0.0]
        b = R.CircuitBreaker(failure_threshold=1, cooldown_s=5,
                             clock=lambda: now[0])
        b.record(False)
        now[0] += 6
        b.allow()
        b.record(False)
        self.assertEqual(b.state, "open")

    def test_timeout_budget(self):
        now = [0.0]
        tb = R.TimeoutBudget(10, clock=lambda: now[0])
        now[0] += 9
        self.assertTrue(tb.spend(0.5))
        self.assertFalse(tb.spend(2))

    def test_slo_math_and_empty_denominator(self):
        slo = R.SLO("availability", 0.99, 3600)
        r = slo.evaluate(1000, 995)
        self.assertTrue(r["met"])
        self.assertEqual(r["budget_remaining"], 5.0)
        self.assertIsNone(slo.evaluate(0, 0)["sli"])

    def test_drill_runner_reports_open_and_recovery(self):
        now = [0.0]
        b = R.CircuitBreaker(failure_threshold=2, cooldown_s=5,
                             clock=lambda: now[0])
        drill = R.DrillRunner(b)
        report = drill.run_with_recovery(2, 1, advance=lambda s: now.__setitem__(
            0, now[0] + s), cooldown_s=6)
        self.assertTrue(report["opened"])
        self.assertEqual(report["final_state"], "closed")
        self.assertEqual(report["steps"], 3)


class TestTLSProxy(unittest.TestCase):
    def test_trust_loopback_by_default(self):
        self.assertTrue(P.is_trusted("127.0.0.1"))
        self.assertTrue(P.is_trusted("::1"))
        self.assertFalse(P.is_trusted("8.8.8.8"))

    def test_trust_cidr(self):
        self.assertTrue(P.is_trusted("10.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(P.is_trusted("11.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(P.is_trusted("not-an-ip"))

    def test_untrusted_peer_headers_ignored(self):
        info = P.resolve_proxy({"X-Forwarded-Proto": "https",
                                "X-Forwarded-For": "1.2.3.4",
                                "Host": "a"}, "8.8.8.8")
        self.assertFalse(info.trusted)
        self.assertEqual(info.proto, "http")
        self.assertEqual(info.client_ip, "8.8.8.8")

    def test_trusted_peer_headers_used(self):
        info = P.resolve_proxy({"X-Forwarded-Proto": "https",
                                "X-Forwarded-For": "1.2.3.4, 10.0.0.1"},
                               "127.0.0.1")
        self.assertTrue(info.trusted)
        self.assertEqual(info.proto, "https")
        self.assertEqual(info.client_ip, "1.2.3.4")

    def test_missing_cert_raises(self):
        with self.assertRaises(FileNotFoundError):
            P.build_context(P.TLSConfig("nope.pem", "nope.key"))


class TestLiveService(unittest.TestCase):
    """进程级验收探针本身也要能被证伪。

    探针永远返回 5/5 的「验收」是最糟的一类测试：它看起来最硬，实际什么都没验。
    这里专测「给一个没有 app.py 的目录，它必须报 0」。
    """

    def test_probe_scores_zero_without_service(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="evo-noapp-")
        SC._LIVE.clear()
        try:
            score, detail, gaps = SC.probe_live(tmp)
            self.assertEqual(score, 0.0)
            self.assertTrue(gaps)
            self.assertFalse(detail[0]["ok"])
        finally:
            SC._LIVE.clear()

    def test_probe_reports_nine_observations_on_real_repo(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isfile(os.path.join(root, "ai-service", "api", "app.py")):
            self.skipTest("不是完整仓库")
        SC._LIVE.clear()
        try:
            score, detail, gaps = SC.probe_live(root)
        finally:
            SC._LIVE.clear()
        self.assertEqual(len(detail), 9, "进程级验收项数变了，同步文档")
        self.assertEqual(score, 5.0, gaps)


class TestScorecard(unittest.TestCase):
    def test_stdout_matches_out_file(self):
        """`--out` 写出来的文件必须和 stdout 一模一样。

        之前 `render_markdown` 自带结尾换行，又用 `print()` 输出，
        stdout 会多一个空行 —— 于是「文档」和「屏幕上看到的」不是同一份。
        """
        import subprocess
        import tempfile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = os.path.join(tempfile.mkdtemp(prefix="evo-sc-"), "sc.md")
        p = subprocess.run([sys.executable, os.path.join(root, "evoguard_cli.py"),
                            "scorecard", "--out", out],
                           capture_output=True, cwd=root)
        self.assertEqual(p.returncode, 0, p.stderr)
        stdout = subprocess.run([sys.executable,
                                 os.path.join(root, "evoguard_cli.py"),
                                 "scorecard"], capture_output=True,
                                cwd=root).stdout.replace(b"\r\n", b"\n")
        with open(out, "rb") as f:
            written = f.read()
        self.assertEqual(stdout, written)

    def test_report_shape_and_evidence(self):
        r = SC.evaluate(ROOT)
        self.assertIn("total", r)
        self.assertEqual(len(r["dimensions"]), len(SC.DIMENSIONS))
        for d in r["dimensions"]:
            self.assertEqual(d["max"], 5)
            self.assertTrue(d["checks"], d["name"])
            for c in d["checks"]:
                self.assertIn("evidence", c)
        self.assertIn("不是认证", r["disclaimer"])

    def test_every_gap_points_to_a_dimension(self):
        r = SC.evaluate(ROOT)
        names = {d["name"] for d in r["dimensions"]}
        for g in r["gaps"]:
            self.assertIn(g["dimension"], names)

    def test_detection_dimension_reflects_real_results(self):
        r = SC.evaluate(ROOT)
        det = [d for d in r["dimensions"] if d["key"] == "detection"][0]
        # v2 未通过，分数必须 < 5，且缺口里要点名
        self.assertLess(det["score"], 5)
        self.assertTrue(any("v2" in g for g in det["gaps"]))

    def test_render_markdown_has_sections(self):
        md = SC.render_markdown(SC.evaluate(ROOT))
        self.assertIn("企业就绪度自评", md)
        self.assertIn("缺口清单", md)


if __name__ == "__main__":
    unittest.main()
