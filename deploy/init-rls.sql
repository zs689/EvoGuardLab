
-- EvoGuardLab multi-tenant RLS (PostgreSQL 14+)
-- 用法：在每个需要隔离的表上启用；事务开始先 SET LOCAL app.current_tenant。
ALTER TABLE runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs        FORCE  ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE  ROW LEVEL SECURITY;
ALTER TABLE skills      ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills      FORCE  ROW LEVEL SECURITY;

-- 策略：只允许读写 current_tenant 匹配的行。
-- FORCE 保证表 owner 也受约束（否则 owner 连接可绕过隔离）。
CREATE POLICY tenant_isolation_runs ON runs
  USING (tenant_id = current_setting('app.current_tenant', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_audit ON audit_events
  USING (tenant_id = current_setting('app.current_tenant', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_skills ON skills
  USING (tenant_id = current_setting('app.current_tenant', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

-- 备用：无 GUC（未 SET）时 current_setting 返回 NULL，所有行都不可见
-- —— fail-closed，绝不因为漏配就把全部租户数据暴露出去。
