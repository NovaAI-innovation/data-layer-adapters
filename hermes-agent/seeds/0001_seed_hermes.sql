-- hermes-agent/seeds/0001_seed_hermes.sql
-- Seeds the Hermes agent row for the Nous Research Hermes framework
-- family. Idempotent. Run AFTER data-layer-postgres schema has been
-- installed.
--
-- The metadata blob previously carried runtime-addressing fields
-- (host/address/mcp_service) for a separate az-retrieval-mcp sidecar
-- that has been retired; the data-layer-adapters universal MCP server
-- is now the only sanctioned MCP endpoint for the data-layer stack
-- (see data-layer-adapters/docs/decisions/0001-dual-write-and-redis-publish-hook.md).
-- Only framework-agnostic descriptive metadata belongs here.

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
           'kanban', true)
  FROM projects p, agent_frameworks f
 WHERE p.project_key = 'default'
   AND f.kind        = 'hermes'
ON CONFLICT (framework_id, framework_local_id, deployment) DO NOTHING;

COMMIT;