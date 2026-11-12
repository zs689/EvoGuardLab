# -*- coding: utf-8 -*-
"""service_controls.py 的独立单元测试（纯标准库，不依赖网络 / 不启 HTTP 服务）。

每个用例都用受控的 environ dict 构造 ServiceControls，隔离真实 os.environ，
并把阈值文件路径指向临时文件，使 validate_config 的行为完全可预测。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ai-service", "evoguard"))

import service_controls as sc  # noqa: E402


class TestServiceControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc_")
        self.thresholds = os.path.join(self.tmp, "thresholds_v1.json")
        with open(self.thresholds, "w", encoding="utf-8") as f:
            json.dump({"version": "thresholds-v1"}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def _controls(self, **env):
        # 不传 environ 时 from_env 会读真实环境；这里一律传受控 dict，保证 hermetic。
        env.setdefault("AGENTLAB_THRESHOLDS", self.thresholds)
        return sc.ServiceControls.from_env(env)

    # 1. 默认关闭鉴权，且显式给出 warning
    def test_default_auth_disabled(self):
        c = self._controls()
        d = c.describe()
        self.assertFalse(d["auth_enabled"])
        self.assertTrue(d["warning"])
        ok, reason, actor = c.authorize({})
        self.assertTrue(ok)
        self.assertEqual(reason, "auth_disabled")
        self.assertEqual(actor, "anonymous")

    # 2. 启用鉴权后，缺失 / 格式错误的 token 被拒
    def test_missing_and_malformed_token_rejected(self):
        c = self._controls(AGENTLAB_API_TOKEN="s3cr3t")
        ok, reason, _ = c.authorize({})
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_or_malformed_token")
        ok, reason, _ = c.authorize({"Authorization": "Basic abc"})
        self.assertFalse(ok)
        ok, reason, _ = c.authorize({"Authorization": "Bearer"})
        self.assertFalse(ok)

    # 3. 错误 token 被拒
    def test_wrong_token_rejected(self):
        c = self._controls(AGENTLAB_API_TOKEN="s3cr3t")
        ok, reason, _ = c.authorize({"Authorization": "Bearer wrong"})
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_token")

    # 4. 正确 token 放行，X-Actor 作为审计主体
    def test_correct_token_allowed_with_actor(self):
        c = self._controls(AGENTLAB_API_TOKEN="s3cr3t")
        ok, reason, actor = c.authorize(
            {"Authorization": "Bearer s3cr3t", "X-Actor": "alice"})
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(actor, "alice")

    # 5. 比较确实走 hmac.compare_digest，而不是 ==
    def test_compare_digest_is_the_gate(self):
        c = self._controls(AGENTLAB_API_TOKEN="s3cr3t")
        # token 用 == 会相等；把 compare_digest 强制成 False 后仍必须被拒，
        # 证明实现依赖 compare_digest 的结果，而非 == 的短路。
        with mock.patch.object(sc.hmac, "compare_digest", return_value=False) as m:
            ok, reason, _ = c.authorize({"Authorization": "Bearer s3cr3t"})
            self.assertFalse(ok)
            m.assert_called_once()

    # 6. 限流触发，返回 retry_after
    def test_rate_limit_triggers(self):
        c = self._controls(AGENTLAB_RATE_LIMIT="2", AGENTLAB_RATE_WINDOW="10")
        self.assertTrue(c.check_rate("bob", now=0.0)[0])
        self.assertTrue(c.check_rate("bob", now=1.0)[0])
        ok, retry = c.check_rate("bob", now=2.0)
        self.assertFalse(ok)
        self.assertGreater(retry, 0)

    # 7. 滑动窗口过期后恢复，不会永久拉黑
    def test_rate_window_expires(self):
        c = self._controls(AGENTLAB_RATE_LIMIT="2", AGENTLAB_RATE_WINDOW="10")
        c.check_rate("bob", now=0.0)
        c.check_rate("bob", now=1.0)
        self.assertFalse(c.check_rate("bob", now=2.0)[0])
        self.assertTrue(c.check_rate("bob", now=12.0)[0])

    # 8. kill switch 只挡写操作，GET 仍可读
    def test_kill_switch_blocks_only_writes(self):
        for val in ("1", "true", "ON", "yes"):
            c = self._controls(AGENTLAB_KILL_SWITCH=val)
            self.assertTrue(c.kill_switch("POST"), val)
            self.assertTrue(c.kill_switch("PUT"), val)
            self.assertTrue(c.kill_switch("DELETE"), val)
            self.assertFalse(c.kill_switch("GET"), val)
            self.assertTrue(c.kill_switch())  # 无 method 时返回开关状态
        off = self._controls()
        self.assertFalse(off.kill_switch("POST"))

    # 9. 日志是单行 JSON，且绝不含 token / 请求正文
    def test_log_single_line_json_no_token(self):
        c = self._controls(AGENTLAB_API_TOKEN="SUPER_SECRET_TOKEN")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            c.log({
                "request_id": "abc123", "method": "POST", "path": "/api/run",
                "status": 200, "actor": "alice", "decision": "allowed",
                "token": "SUPER_SECRET_TOKEN", "body": "sensitive request body",
            })
        raw = buf.getvalue()
        lines = raw.strip().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        for field in ("ts", "request_id", "method", "path", "status",
                      "actor", "decision"):
            self.assertIn(field, rec)
        self.assertEqual(list(rec.keys()), sorted(rec.keys()))  # sort_keys 确定性
        self.assertEqual(rec["request_id"], "abc123")
        self.assertNotIn("SUPER_SECRET_TOKEN", raw)
        self.assertNotIn("token", rec)
        self.assertNotIn("body", rec)

    # 13. AGENTLAB_LOG_QUIET 抑制结构化日志输出（与 docs 描述一致）
    def test_log_quiet_suppresses_output(self):
        c = self._controls(AGENTLAB_LOG_QUIET="1")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            c.log({"request_id": "x", "method": "GET", "path": "/",
                   "status": 200, "actor": "a", "decision": "allowed"})
        self.assertEqual(buf.getvalue(), "")
        self.assertTrue(c.describe()["log_quiet"])

    # 10. describe 脱敏：token 只显示是否已设置
    def test_describe_redacts_token(self):
        c = self._controls(AGENTLAB_API_TOKEN="SUPER_SECRET_TOKEN")
        d = c.describe()
        self.assertTrue(d["api_token_set"])
        self.assertNotIn("SUPER_SECRET_TOKEN", json.dumps(d, ensure_ascii=False))
        self.assertNotIn("api_token", d)

    # 11. validate_config 对坏值报错
    def test_validate_config_reports_bad_numeric(self):
        c = self._controls(AGENTLAB_RATE_LIMIT="abc")
        errs = c.validate_config()
        self.assertTrue(any("AGENTLAB_RATE_LIMIT" in e for e in errs))

    # 12. 合法配置返回空错误列表；阈值文件缺失 / 坏 JSON 会报错
    def test_validate_config_ok_missing_and_bad_thresholds(self):
        self.assertEqual(self._controls().validate_config(), [])
        missing = self._controls(
            AGENTLAB_THRESHOLDS=os.path.join(self.tmp, "nope.json"))
        self.assertTrue(any("阈值文件" in e for e in missing.validate_config()))
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        bad_cfg = self._controls(AGENTLAB_THRESHOLDS=bad)
        self.assertTrue(any("JSON" in e for e in bad_cfg.validate_config()))


if __name__ == "__main__":
    unittest.main()
