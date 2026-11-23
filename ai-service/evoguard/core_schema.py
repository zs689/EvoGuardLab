# -*- coding: utf-8 -*-
"""SQLite schema 的单一真相源。

从 core.py 拆出（纯数据搬迁，零逻辑改动）：让表结构演进有明确落点，
core.py 不必为一串 DDL 占位；也让"表/索引有没有漏"可以被单独测试。

改动这里必须同步检查：
  - scripts/check_wheel_manifest.py 的打包清单
  - 旧库的 CREATE TABLE IF NOT EXISTS 升级路径（不要改成 DROP）
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments(
  id TEXT PRIMARY KEY, name TEXT, agent_type TEXT, task_id TEXT,
  fault_type TEXT, fault_target INTEGER, seed INTEGER, recovery_mode TEXT,
  created_at REAL, status TEXT);

CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, experiment_id TEXT, mode TEXT, status TEXT,
  seed INTEGER, started_at REAL, ended_at REAL, success INTEGER,
  tokens INTEGER, latency_ms INTEGER, answer TEXT, expect TEXT,
  note TEXT, skill_id TEXT);

CREATE TABLE IF NOT EXISTS steps(
  id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, logical INTEGER,
  kind TEXT, name TEXT,
  input TEXT, output TEXT, status TEXT, error_type TEXT,
  duration_ms INTEGER, model_id TEXT, tokens INTEGER, checkpoint_id TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, state TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS faults(
  id TEXT PRIMARY KEY, run_id TEXT, fault_type TEXT, target_seq INTEGER,
  payload TEXT, injected INTEGER, detected INTEGER);

CREATE TABLE IF NOT EXISTS diagnoses(
  id TEXT PRIMARY KEY, run_id TEXT, first_bad_step INTEGER, root_cause TEXT,
  evidence TEXT, confidence REAL, created_at REAL);

CREATE TABLE IF NOT EXISTS recoveries(
  id TEXT PRIMARY KEY, run_id TEXT, diagnosis_id TEXT, strategy TEXT,
  params TEXT, approval TEXT, ok INTEGER, detail TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, run_id TEXT, from_state TEXT, to_state TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS evals(
  id TEXT PRIMARY KEY, experiment_id TEXT, metrics TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS agent_sessions(
  session_id TEXT PRIMARY KEY, tenant_id TEXT, agent_type TEXT,
  turns TEXT, summary TEXT, untrusted_boundary TEXT,
  created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON agent_sessions(tenant_id);

CREATE TABLE IF NOT EXISTS agent_plans(
  session_id TEXT PRIMARY KEY, tenant_id TEXT, plan TEXT,
  next_step INTEGER, status TEXT, results TEXT,
  created_at REAL, updated_at REAL);

-- HARD-01-T3：合成微工具持久化（重启后 AST 复验 + sha256 才能重载）
CREATE TABLE IF NOT EXISTS synthesized_tools(
  name TEXT PRIMARY KEY, tenant_id TEXT, source TEXT,
  source_sha256 TEXT, schema_json TEXT, tests_json TEXT,
  func_name TEXT, version INTEGER, status TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_synth_tenant ON synthesized_tools(tenant_id);

CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_faults_run ON faults(run_id);
"""
