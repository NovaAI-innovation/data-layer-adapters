-- agent-zero/seeds/0002_migrate_local_id.sql
-- ONE-SHOT operator-side migration for pre-existing data created
-- before the (framework_id, framework_local_id, deployment) identity
-- contract took effect. Run ONCE per database that has legacy rows.
--
-- Idempotent: only matches rows that still carry the legacy
-- framework_local_id 'agent-zero-sbzm'.

BEGIN;

UPDATE agents
   SET framework_local_id = '__LOCAL_CONTAINER_NAME__',
       deployment         = 'local'
 WHERE framework_local_id = 'agent-zero-sbzm'
   AND deployment         = '';

COMMIT;
