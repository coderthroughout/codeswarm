-- ============================================================================
-- PROPOSED multi-node codeswarm corpus seed (Lane 3A — de-hardcode the decisive
-- node). THIS FILE LIVES IN THE codeswarm REPO AS A PROPOSAL ONLY. It is the
-- platform-side change the founder must apply IN Omium-platform to match the
-- codeswarm change in codeswarm/workflow/omium_executor.py. It replaces:
--   * Omium-platform/scripts/seed_codeswarm_corpus_workflow.sql       (RED anchor)
--   * Omium-platform/scripts/seed_codeswarm_corpus_green_pool.sql     (GREEN pool)
-- DO NOT apply this from the codeswarm repo; hand it to the platform side.
--
-- WHY (the gT5/gT6 root cause)
-- ----------------------------
-- EE's failure_signature_hash (execution-engine/app/services/signature.py) is
--   sha256( error_type | NORMALIZE_v1(message) | FAILING_NODE )
-- and the recovery-orchestrator records that SAME failing node as `decisive_step`
-- (recovery-orchestrator/app/loop/phases/diagnosing.py). The old seed pinned EVERY
-- codeswarm row to ONE node ("cs_oracle"), so although codeswarm produced 16-21
-- distinct MESSAGE signatures, the WHERE axis — and therefore `decisive_step` —
-- collapsed to a single token across all clusters. gT5/gT6 (which score decisive-
-- step diversity / attribution) stayed UNDECIDED.
--
-- codeswarm now picks the failing node DETERMINISTICALLY from a SET of N names
-- (CORPUS_NODE_NAMES in omium_executor.py):
--   index 0 cs_oracle, 1 cs_parser, 2 cs_planner, 3 cs_synth, 4 cs_validator
-- The choice is a pure function of codeswarm's stable signature identity, so the
-- SAME task always localizes to the SAME node -> replay reproduces at the right
-- node. RED and GREEN use the SAME chooser, so a task's red+green rows share a node
-- (one signature cluster; the per-signature training floor is preserved).
--
-- BINDING CONTRACT with codeswarm (must stay in lock-step):
--   * RED: codeswarm stamps workflow_version = corpus_node_version(node) = index+1.
--     So RED needs ONE pinned version per node, version v carrying node
--     CORPUS_NODE_NAMES[v-1].
--   * GREEN: codeswarm draws a pool version from the chosen node's sub-pool via
--     GreenKeyAllocator.version_node(v) = CORPUS_NODE_NAMES[(v-1) % N]. So GREEN
--     version v MUST carry node CORPUS_NODE_NAMES[(v-1) % N] (a round-robin
--     interleave) and key 'cs-green-<v>' exactly as before.
--   * If CORPUS_NODE_NAMES changes on the codeswarm side, re-generate this seed.
--
-- Idempotent DELETE-then-INSERT + ON CONFLICT, founder/test tenant only. Same psql
-- apply pattern as the existing seeds.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- RED anchor: one workflow, N pinned versions (version v -> node index v-1).
-- Workflow id unchanged: uuid5(NAMESPACE_DNS,"codeswarm.omium.corpus.workflow")
--   = 8b628198-fd99-53ce-898d-2b53c647374d
-- (codeswarm/workflow/omium_executor.py::_default_workflow_id).
-- ---------------------------------------------------------------------------
DELETE FROM workflow_versions WHERE workflow_id = '8b628198-fd99-53ce-898d-2b53c647374d';
DELETE FROM workflows        WHERE id          = '8b628198-fd99-53ce-898d-2b53c647374d';

-- Head row (informational; RERUNNING pins the workflow_versions rows below). Its
-- definition shows version 1 (cs_oracle) for continuity with the legacy anchor.
INSERT INTO workflows (
  id, tenant_id, created_by, name, description, workflow_type,
  definition, version, config, status, published_at, tags, created_at, updated_at, deleted_at
) VALUES (
  '8b628198-fd99-53ce-898d-2b53c647374d',
  'c9997cff-5895-4263-a409-3518a5522ec3', NULL,
  'codeswarm-corpus-multinode',
  'codeswarm Mode-2 corpus anchor (multi-node): one pinned version per failing node '
  '(cs_oracle/cs_parser/cs_planner/cs_synth/cs_validator, version = node index + 1). '
  'The chosen node permanently fails with the code_test_failure class marker; the '
  'authoritative re-run reproduces at THAT node -> verified_failure (reward=0). '
  'De-hardcodes the decisive_step axis (gT5/gT6).',
  'langgraph',
  '{
     "name": "codeswarm-corpus-cs_oracle",
     "nodes": [
       {"name": "ingest", "function": "ingest_node"},
       {"name": "cs_oracle", "function": "process_node",
        "force_error": "codeswarm re-run: task failed its pytest oracle (pinned reproduce)",
        "emit_side_effect": "orders"},
       {"name": "summarize", "function": "summarize_node"}
     ],
     "edges": [
       {"from": "START", "to": "ingest"},
       {"from": "ingest", "to": "cs_oracle"},
       {"from": "cs_oracle", "to": "summarize"},
       {"from": "summarize", "to": "END"}
     ],
     "postconditions": [
       {"step_id": "cs_oracle", "effect_kind": "db_row",
        "assertion": {"store": "orders",
                      "match": {"execution_id": "$execution_id", "status": "succeeded"},
                      "expect": "exactly_one"},
        "live_probe": "db:orders?execution_id=$execution_id"}
     ]
   }'::jsonb,
  1, '{"backoff_ms": 0, "rate_limit_retries": 0}'::jsonb,
  'published', NOW(), '["codeswarm","corpus","code_test_failure","multinode"]'::jsonb,
  NOW(), NOW(), NULL
)
ON CONFLICT (id) DO NOTHING;

-- One pinned RED version per node. node(v) = names[v-1]; the def's failing node,
-- its name, its force_error class marker, and the orders postcondition all track it.
INSERT INTO workflow_versions (
  id, workflow_id, tenant_id, version, definition, config, created_by, created_at
)
SELECT
  md5('codeswarm-corpus-red-v' || v)::uuid,
  '8b628198-fd99-53ce-898d-2b53c647374d',
  'c9997cff-5895-4263-a409-3518a5522ec3',
  v,
  jsonb_build_object(
    'name', 'codeswarm-corpus-' || node,
    'nodes', jsonb_build_array(
      jsonb_build_object('name','ingest','function','ingest_node'),
      jsonb_build_object(
        'name', node, 'function', 'process_node',
        'force_error', 'codeswarm re-run: task failed its pytest oracle (pinned reproduce)',
        'emit_side_effect', 'orders'),
      jsonb_build_object('name','summarize','function','summarize_node')
    ),
    'edges', jsonb_build_array(
      jsonb_build_object('from','START','to','ingest'),
      jsonb_build_object('from','ingest','to',node),
      jsonb_build_object('from',node,'to','summarize'),
      jsonb_build_object('from','summarize','to','END')
    ),
    'postconditions', jsonb_build_array(
      jsonb_build_object(
        'step_id', node, 'effect_kind', 'db_row',
        'assertion', jsonb_build_object(
          'store','orders',
          'match', jsonb_build_object('execution_id','$execution_id','status','succeeded'),
          'expect','exactly_one'),
        'live_probe', 'db:orders?execution_id=$execution_id')
    )
  ),
  '{"backoff_ms": 0, "rate_limit_retries": 0}'::jsonb, NULL, NOW()
FROM (
  SELECT v, (ARRAY['cs_oracle','cs_parser','cs_planner','cs_synth','cs_validator'])[v] AS node
  FROM generate_series(1, 5) AS v            -- keep 5 in sync with len(CORPUS_NODE_NAMES)
) AS red
ON CONFLICT (workflow_id, version) DO NOTHING;

-- ---------------------------------------------------------------------------
-- GREEN pool: one workflow, N=40 pinned versions. Version v carries
--   node  = names[(v-1) % 5]          (round-robin interleave; matches
--                                       GreenKeyAllocator.version_node)
--   key   = 'cs-green-<v>'            (per-version fail-once Redis counter)
-- Workflow id unchanged:
--   uuid5(NAMESPACE_DNS,"codeswarm.omium.corpus.workflow.green")
--   = 82a67367-4d6d-5abd-97d2-00d33a7ef863
-- (codeswarm/workflow/omium_executor.py::_default_green_workflow_id).
-- Pool size 40 — keep in sync with DEFAULT_GREEN_POOL_SIZE and the clear helper's
-- --pool-size. Prereq: EE migration 007 (the `orders` table).
-- ---------------------------------------------------------------------------
DELETE FROM workflow_versions WHERE workflow_id = '82a67367-4d6d-5abd-97d2-00d33a7ef863';
DELETE FROM workflows        WHERE id          = '82a67367-4d6d-5abd-97d2-00d33a7ef863';

INSERT INTO workflows (
  id, tenant_id, created_by, name, description, workflow_type,
  definition, version, config, status, published_at, tags, created_at, updated_at, deleted_at
) VALUES (
  '82a67367-4d6d-5abd-97d2-00d33a7ef863',
  'c9997cff-5895-4263-a409-3518a5522ec3', NULL,
  'codeswarm-corpus-multinode-green',
  'codeswarm Mode-2 GREEN pool (multi-node): version v fails ONCE at node '
  'names[(v-1) % 5] via key cs-green-<v>, then heals on the authoritative re-run '
  '(writes the orders ground-truth row) -> verified_success. Desirable arm; nodes '
  'interleaved so a task keeps ONE node across red+green (one signature cluster).',
  'langgraph',
  '{
     "name": "codeswarm-corpus-cs_oracle-green",
     "nodes": [
       {"name": "ingest", "function": "ingest_node"},
       {"name": "cs_oracle", "function": "process_node",
        "force_error_once": "codeswarm re-run: recoverable pytest-oracle failure (heals on pinned re-run)",
        "force_error_once_key": "cs-green-1",
        "emit_side_effect": "orders"},
       {"name": "summarize", "function": "summarize_node"}
     ],
     "edges": [
       {"from": "START", "to": "ingest"},
       {"from": "ingest", "to": "cs_oracle"},
       {"from": "cs_oracle", "to": "summarize"},
       {"from": "summarize", "to": "END"}
     ],
     "postconditions": [
       {"step_id": "cs_oracle", "effect_kind": "db_row",
        "assertion": {"store": "orders",
                      "match": {"execution_id": "$execution_id", "status": "succeeded"},
                      "expect": "exactly_one"},
        "live_probe": "db:orders?execution_id=$execution_id"}
     ]
   }'::jsonb,
  1, '{"backoff_ms": 0, "rate_limit_retries": 0}'::jsonb,
  'published', NOW(), '["codeswarm","corpus","code_test_failure","green","multinode"]'::jsonb,
  NOW(), NOW(), NULL
)
ON CONFLICT (id) DO NOTHING;

-- 40 pinned versions; node and key BOTH derive from v (node = round-robin, key = v).
INSERT INTO workflow_versions (
  id, workflow_id, tenant_id, version, definition, config, created_by, created_at
)
SELECT
  md5('codeswarm-corpus-green-v' || v)::uuid,
  '82a67367-4d6d-5abd-97d2-00d33a7ef863',
  'c9997cff-5895-4263-a409-3518a5522ec3',
  v,
  jsonb_build_object(
    'name', 'codeswarm-corpus-' || node || '-green',
    'nodes', jsonb_build_array(
      jsonb_build_object('name','ingest','function','ingest_node'),
      jsonb_build_object(
        'name', node, 'function', 'process_node',
        'force_error_once', 'codeswarm re-run: recoverable pytest-oracle failure (heals on pinned re-run)',
        'force_error_once_key', 'cs-green-' || v,
        'emit_side_effect', 'orders'),
      jsonb_build_object('name','summarize','function','summarize_node')
    ),
    'edges', jsonb_build_array(
      jsonb_build_object('from','START','to','ingest'),
      jsonb_build_object('from','ingest','to',node),
      jsonb_build_object('from',node,'to','summarize'),
      jsonb_build_object('from','summarize','to','END')
    ),
    'postconditions', jsonb_build_array(
      jsonb_build_object(
        'step_id', node, 'effect_kind', 'db_row',
        'assertion', jsonb_build_object(
          'store','orders',
          'match', jsonb_build_object('execution_id','$execution_id','status','succeeded'),
          'expect','exactly_one'),
        'live_probe', 'db:orders?execution_id=$execution_id')
    )
  ),
  '{"backoff_ms": 0, "rate_limit_retries": 0}'::jsonb, NULL, NOW()
FROM (
  SELECT v, (ARRAY['cs_oracle','cs_parser','cs_planner','cs_synth','cs_validator'])
              [((v - 1) % 5) + 1] AS node    -- keep 5 in sync with len(CORPUS_NODE_NAMES)
  FROM generate_series(1, 40) AS v            -- keep 40 in sync with DEFAULT_GREEN_POOL_SIZE
) AS green
ON CONFLICT (workflow_id, version) DO NOTHING;

COMMIT;
