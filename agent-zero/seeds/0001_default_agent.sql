-- agent-zero/seeds/0001_default_agent.sql
-- Seeds the canonical Agent Zero agent row. Idempotent. Run AFTER
-- data-layer-postgres schema has been installed.
--
-- Operators set DATA_LAYER_LOCAL_CONTAINER_NAME before applying; the
-- placeholder is substituted by agent-zero/bootstrap seed.

BEGIN;

INSERT INTO agent_frameworks (kind, display_name, version)
VALUES ('agent_zero', 'Agent Zero', '2.11')
ON CONFLICT (kind) DO NOTHING;

INSERT INTO projects (project_key, display_name, description)
VALUES ('default', 'Default Project',
        'Workspace for an Agent Zero instance')
ON CONFLICT (project_key) DO NOTHING;

INSERT INTO agents (project_id, framework_id, framework_local_id,
                    deployment, display_name, profile_key, status)
SELECT p.id, f.id,
       '__LOCAL_CONTAINER_NAME__',
       'local',
       'agent0',
       'a0',
       'active'
  FROM projects p, agent_frameworks f
 WHERE p.project_key = 'default'
   AND f.kind        = 'agent_zero'
ON CONFLICT (framework_id, framework_local_id, deployment) DO NOTHING;

COMMIT;
