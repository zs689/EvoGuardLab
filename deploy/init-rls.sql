
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['runs', 'audit_events', 'skills'] LOOP
    IF to_regclass(t) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_%s ON %I',
                     t, t);
      EXECUTE format(
        'CREATE POLICY tenant_isolation_%s ON %I '
        'USING (tenant_id = current_setting(''app.current_tenant'', true)) '
        'WITH CHECK (tenant_id = current_setting(''app.current_tenant'', true))',
        t, t);
    END IF;
  END LOOP;
END $$;
