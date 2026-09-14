-- Grant the app's Postgres role permission to create tables.
--
-- RUN IN THE LAKEBASE INSTANCE'S OWN QUERY EDITOR (opened from the database
-- instance page), as a superuser role - NOT in a workspace SQL editor or a
-- notebook %sql cell. Those target Unity Catalog, read `copilot_app` as a
-- Databricks principal, and fail with PRINCIPAL_DOES_NOT_EXIST.
--
-- Skip if the role is already in `databricks_superuser`. Check first:
--     SELECT has_schema_privilege('copilot_app', 'public', 'CREATE');
--
-- Why: since PostgreSQL 15 `public` no longer grants CREATE to every role, so a
-- fresh Lakebase password role gets USAGE only.

GRANT CREATE ON SCHEMA public TO copilot_app;

-- Expect: t, t
SELECT has_schema_privilege('copilot_app', 'public', 'USAGE')  AS has_usage,
       has_schema_privilege('copilot_app', 'public', 'CREATE') AS has_create;
