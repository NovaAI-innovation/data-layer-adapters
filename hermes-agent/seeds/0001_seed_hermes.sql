-- hermes-agent/seeds/0001_seed_hermes.sql
-- Seeds the Hermes agent row for the Nous Research Hermes framework
-- family. Idempotent. Run AFTER data-layer-postgres schema has been
-- installed.

BEGIN;

INSERT INTO agent_frameworks (kind, display_name, version)
VALUES ('hermes', 'Hermes (Nous Research)', NULL)
ON CONFLICT (kind) DO NOTHING;

INSERT INTO agents (project_id, framework_id, framework_local_id,
                    deployment, display_name, profile_key, status, metadata)
SELECT p.id, f.id,
       '__HERMES_CONTAINER_NAME__',
       'hermes',
       'Hermes root',
       'hermes',
       'active',
       jsonb_build_object(
           'host',        'hermes.tailbcc871.ts.net',
           'address',     '100.64.49.70:8091',
           'mcp_service', 'az-retrieval-mcp',
           'kanban',      true)
  FROM projects p, agent_frameworks f
 WHERE p.project_key = 'default'
   AND f.kind        = 'hermes'
ON CONFLICT (framework_id, framework_local_id, deployment) DO NOTHING;

COMMIT;
