-- =====================================================================
-- ITSM Agent Platform — PostgreSQL 16 + pgvector schema
-- Single organisation, self-hosted. Executed and tested by
-- schema/seed_and_tests.sql (see docs/13-validation-report.md).
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector (embeddings)
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- fuzzy matching for dedupe/conflict checks

CREATE SCHEMA IF NOT EXISTS itsm;
SET search_path = itsm, public;

-- ---------------------------------------------------------------------
-- 0. Shared helpers
-- ---------------------------------------------------------------------

-- Blocks UPDATE/DELETE on append-only tables unless a retention job has
-- explicitly opted in for the current transaction:
--   SET LOCAL itsm.retention_purge = 'on';
CREATE OR REPLACE FUNCTION itsm.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND coalesce(current_setting('itsm.retention_purge', true), 'off') = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '% on %.% is not allowed: table is append-only', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE OR REPLACE FUNCTION itsm.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------
-- 1. Identity & RBAC
-- ---------------------------------------------------------------------
CREATE TABLE orgs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL REFERENCES orgs(id),
  external_subject text NOT NULL UNIQUE,              -- OIDC 'sub' from the IdP
  email            text NOT NULL,
  display_name     text NOT NULL,
  time_zone        text NOT NULL DEFAULT 'UTC',       -- IANA zone, used to resolve relative time
  status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_uq ON users (lower(email));

CREATE TABLE roles (
  name        text PRIMARY KEY,                       -- e.g. requester, approver, admin, security_reviewer
  description text NOT NULL
);

CREATE TABLE user_roles (
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       text NOT NULL REFERENCES roles(name),
  granted_by uuid REFERENCES users(id),
  granted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, role)
);

-- ---------------------------------------------------------------------
-- 2. Sessions, append-only event log, jobs, task ledger
-- ---------------------------------------------------------------------
CREATE TABLE sessions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id),
  title       text,
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER sessions_touch BEFORE UPDATE ON sessions
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Source of truth for everything that happened; context is rebuilt from it.
CREATE TABLE session_events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id  uuid NOT NULL REFERENCES sessions(id),
  seq         integer NOT NULL CHECK (seq >= 1),
  event_type  text NOT NULL CHECK (event_type IN (
                'user_message', 'model_request', 'model_response', 'tool_call', 'tool_result',
                'approval_requested', 'approval_decided', 'action_executed', 'ledger_reset',
                'system_note', 'error')),
  job_id      uuid,                                   -- FK added after jobs exists
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, seq)
);
CREATE TRIGGER session_events_append_only BEFORE UPDATE OR DELETE ON session_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE jobs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id     uuid NOT NULL REFERENCES sessions(id),
  user_id        uuid NOT NULL REFERENCES users(id),
  request_event  bigint NOT NULL REFERENCES session_events(id),   -- the user turn that started it
  status         text NOT NULL DEFAULT 'queued' CHECK (status IN (
                   'queued', 'running', 'paused', 'awaiting_approval', 'completed', 'failed', 'cancelled')),
  ledger         jsonb NOT NULL DEFAULT '{}'::jsonb,               -- doc 03 §6
  banner         text CHECK (banner IN ('verified', 'verified_with_caveats', 'partially_verified')),
  token_budget   integer NOT NULL DEFAULT 400000 CHECK (token_budget > 0),
  tokens_used    integer NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
  cost_usd       numeric(12, 4) NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz
);
CREATE INDEX jobs_session_idx ON jobs (session_id, created_at);
CREATE TRIGGER jobs_touch BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

ALTER TABLE session_events
  ADD CONSTRAINT session_events_job_fk FOREIGN KEY (job_id) REFERENCES jobs(id);

-- Durable work queue + task ledger steps. Workers claim with FOR UPDATE SKIP LOCKED.
CREATE TABLE job_steps (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id      uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step_key    text NOT NULL,                          -- e.g. s1, s2 (stable within job)
  kind        text NOT NULL CHECK (kind IN (
                'plan', 'fetch', 'enrich', 'map', 'reduce', 'draft', 'verify', 'act', 'post_check')),
  status      text NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'ready', 'running', 'done', 'failed', 'blocked', 'skipped')),
  depends_on  text[] NOT NULL DEFAULT '{}',
  attempt     integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  locked_by   text,
  locked_at   timestamptz,
  input       jsonb NOT NULL DEFAULT '{}'::jsonb,
  output      jsonb,
  notes       text,                                   -- the only free-text field the model may edit
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, step_key),
  CHECK (attempt <= max_attempts)
);
CREATE INDEX job_steps_ready_idx ON job_steps (status, created_at) WHERE status = 'ready';
CREATE TRIGGER job_steps_touch BEFORE UPDATE ON job_steps
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------
-- 3. Data plane: query specs, result sets, records, annotations, facts
-- ---------------------------------------------------------------------
CREATE TABLE query_specs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id       uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  spec         jsonb NOT NULL,
  window_start timestamptz NOT NULL,
  window_end   timestamptz NOT NULL,
  window_field text NOT NULL,                          -- which timestamp defines membership
  user_tz      text NOT NULL,
  frozen_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (window_end > window_start),
  CHECK (window_end <= frozen_at + interval '1 minute')  -- window may not extend into the future
);

CREATE TABLE result_sets (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  handle                text NOT NULL UNIQUE,          -- e.g. rs_01HZX3K9 (what the model sees)
  job_id                uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  query_spec_id         uuid REFERENCES query_specs(id),
  kind                  text NOT NULL CHECK (kind IN ('fetch', 'derived')),
  source_system         text NOT NULL,                 -- jira, zendesk, ... or 'analyzer' for derived
  derived_from          uuid[] NOT NULL DEFAULT '{}',
  derivation            jsonb,                         -- filter/union/join spec for derived sets
  status                text NOT NULL DEFAULT 'fetching' CHECK (status IN (
                          'fetching', 'complete', 'over_limit', 'failed')),
  completeness_status   text NOT NULL DEFAULT 'unknown' CHECK (completeness_status IN (
                          'unknown', 'proven', 'bounded', 'incomplete')),
  completeness          jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_reported_total integer,
  approximate_count     integer,
  fetched_rows          integer NOT NULL DEFAULT 0,
  unique_rows           integer NOT NULL DEFAULT 0,
  max_records           integer NOT NULL DEFAULT 2000 CHECK (max_records BETWEEN 1 AND 2000),
  created_at            timestamptz NOT NULL DEFAULT now(),
  completed_at          timestamptz,
  expires_at            timestamptz NOT NULL DEFAULT now() + interval '30 days',
  CHECK (kind = 'derived' OR query_spec_id IS NOT NULL),
  CHECK (kind = 'fetch' OR cardinality(derived_from) >= 1),
  -- 'proven' requires the exact-count rule when the source reported a total
  CHECK (completeness_status <> 'proven' OR source_reported_total IS NULL OR source_reported_total = unique_rows),
  CHECK (unique_rows <= fetched_rows OR kind = 'derived')
);
CREATE INDEX result_sets_job_idx ON result_sets (job_id);

-- Per-page checkpoints so fetches resume after a crash.
CREATE TABLE result_set_pages (
  result_set_id uuid NOT NULL REFERENCES result_sets(id) ON DELETE CASCADE,
  page_no       integer NOT NULL CHECK (page_no >= 1),
  cursor_in     text,
  cursor_out    text,                                  -- NULL on the last page
  row_count     integer NOT NULL CHECK (row_count >= 0),
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (result_set_id, page_no)
);
-- Loop guard: a cursor may never repeat within one result set.
CREATE UNIQUE INDEX result_set_pages_cursor_uq ON result_set_pages (result_set_id, cursor_out)
  WHERE cursor_out IS NOT NULL;

CREATE TABLE records (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  result_set_id   uuid NOT NULL REFERENCES result_sets(id) ON DELETE CASCADE,  -- owning fetch set
  source_system   text NOT NULL,
  source_id       text NOT NULL,                       -- e.g. NET-4411, CHG0031122, 123456
  source_url      text NOT NULL,
  title           text,
  body_text       text,                                -- sanitised, <= 8000 chars
  status          text,
  created_at_src  timestamptz,
  updated_at_src  timestamptz,
  event_at        timestamptz,                         -- value of the window field
  fields          jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_hash    text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  injection_flag  boolean NOT NULL DEFAULT false,
  fetched_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (result_set_id, source_system, source_id),    -- dedupe key for pagination overlap
  CHECK (body_text IS NULL OR length(body_text) <= 8000)
);
CREATE INDEX records_source_idx ON records (source_system, source_id);
CREATE INDEX records_fields_gin ON records USING gin (fields jsonb_path_ops);

-- Membership: every result set (fetch or derived) lists its records here,
-- so the Analyzer always queries one shape.
CREATE TABLE result_set_members (
  result_set_id uuid NOT NULL REFERENCES result_sets(id) ON DELETE CASCADE,
  record_id     bigint NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  PRIMARY KEY (result_set_id, record_id)
);
CREATE INDEX result_set_members_record_idx ON result_set_members (record_id);

CREATE TABLE rubrics (
  id          text PRIMARY KEY,                        -- e.g. network_change_v3
  name        text NOT NULL,
  version     integer NOT NULL CHECK (version >= 1),
  labels      text[] NOT NULL CHECK (cardinality(labels) >= 2),
  definition  jsonb NOT NULL,                          -- rules + few-shot examples
  status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
  approved_by uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version),
  CHECK (status <> 'active' OR approved_by IS NOT NULL)
);

CREATE TABLE map_batches (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  result_set_id uuid NOT NULL REFERENCES result_sets(id) ON DELETE CASCADE,
  rubric_id     text NOT NULL REFERENCES rubrics(id),
  batch_no      integer NOT NULL CHECK (batch_no >= 1),
  attempt       integer NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 3),
  pass          text NOT NULL DEFAULT 'primary' CHECK (pass IN ('primary', 'second_opinion', 'adjudication')),
  record_ids    bigint[] NOT NULL CHECK (cardinality(record_ids) BETWEEN 1 AND 50),
  model         text NOT NULL,
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'valid', 'invalid', 'failed')),
  validation    jsonb,                                 -- V1..V5 results (doc 05 §6.2)
  api_request_id text,
  usage         jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, rubric_id, pass, batch_no, attempt)
);

CREATE TABLE record_annotations (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  record_id      bigint NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  job_id         uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  rubric_id      text NOT NULL REFERENCES rubrics(id),
  batch_id       uuid REFERENCES map_batches(id),     -- NULL for deterministic / human decisions
  source         text NOT NULL CHECK (source IN (
                   'deterministic_rule', 'primary', 'second_opinion', 'adjudication', 'human')),
  label          text NOT NULL,
  confidence     text CHECK (confidence IN ('high', 'medium', 'low')),
  evidence_quote text,
  reason_code    text,
  grounded       boolean,                              -- V2 result; NULL for deterministic/human
  record_hash    text NOT NULL,                        -- content_hash of the record version judged
  is_final       boolean NOT NULL DEFAULT false,
  needs_human_review boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (source IN ('deterministic_rule', 'human') OR batch_id IS NOT NULL),
  -- a model label claiming a positive class must be grounded
  CHECK (NOT is_final OR source IN ('deterministic_rule', 'human') OR grounded IS TRUE
         OR label = 'insufficient_information')
);
-- exactly one final label per record, job and rubric
CREATE UNIQUE INDEX record_annotations_final_uq
  ON record_annotations (record_id, job_id, rubric_id) WHERE is_final;
CREATE INDEX record_annotations_job_idx ON record_annotations (job_id, rubric_id, label) WHERE is_final;

-- Label must be one of the rubric's labels, and record_hash must match the record.
CREATE OR REPLACE FUNCTION itsm.check_annotation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  allowed text[];
  current_hash text;
BEGIN
  SELECT labels INTO allowed FROM rubrics WHERE id = NEW.rubric_id;
  IF NOT (NEW.label = ANY (allowed)) THEN
    RAISE EXCEPTION 'label "%" is not defined in rubric %', NEW.label, NEW.rubric_id
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT content_hash INTO current_hash FROM records WHERE id = NEW.record_id;
  IF NEW.record_hash <> current_hash THEN
    RAISE EXCEPTION 'annotation refers to a different record version (hash mismatch)'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER record_annotations_check BEFORE INSERT OR UPDATE ON record_annotations
  FOR EACH ROW EXECUTE FUNCTION check_annotation();

-- Facts: the only source of numbers in answers. Reproducible by re-running sql_text.
CREATE TABLE facts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id          uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  fact_key        text NOT NULL CHECK (fact_key ~ '^F-[0-9]+$'),
  description     text NOT NULL,
  sql_text        text NOT NULL,
  params          jsonb NOT NULL DEFAULT '[]'::jsonb,
  input_result_set_ids uuid[] NOT NULL CHECK (cardinality(input_result_set_ids) >= 1),
  input_digest    text NOT NULL,                       -- sha256 over member record hashes
  value           jsonb NOT NULL,
  computed_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, fact_key)
);
CREATE TRIGGER facts_append_only BEFORE UPDATE OR DELETE ON facts
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Code-rendered lists (placeholders L-n in answers).
CREATE TABLE rendered_lists (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  list_key      text NOT NULL CHECK (list_key ~ '^L-[0-9]+$'),
  result_set_id uuid NOT NULL REFERENCES result_sets(id),
  spec          jsonb NOT NULL,                        -- filters, columns, sort
  row_count     integer NOT NULL CHECK (row_count >= 0),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, list_key)
);

CREATE TABLE exports (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id      uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  source_ref  text NOT NULL,                           -- handle or list key
  format      text NOT NULL CHECK (format IN ('csv', 'xlsx')),
  storage_uri text NOT NULL,
  row_count   integer NOT NULL CHECK (row_count >= 0),
  expires_at  timestamptz NOT NULL DEFAULT now() + interval '24 hours',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 4. Answers, claims, verification (triple check)
-- ---------------------------------------------------------------------
CREATE TABLE answers (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id       uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  revision     integer NOT NULL CHECK (revision BETWEEN 0 AND 2),   -- max 2 revisions (doc 06 §2.5)
  draft        jsonb NOT NULL,                         -- narrative_md + claims + caveats
  rendered_md  text,
  status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'verified', 'failed', 'delivered', 'downgraded')),
  banner       text CHECK (banner IN ('verified', 'verified_with_caveats', 'partially_verified')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, revision),
  CHECK (status NOT IN ('verified', 'delivered') OR coalesce(banner IN ('verified', 'verified_with_caveats'), false)),
  CHECK (status <> 'downgraded' OR coalesce(banner = 'partially_verified', false))
);

CREATE TABLE claims (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  answer_id   uuid NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  claim_key   text NOT NULL,                           -- c1, c2 ...
  claim_type  text NOT NULL CHECK (claim_type IN ('numeric', 'attribute', 'existence', 'absence', 'causal', 'recommendation')),
  text        text NOT NULL,
  value       numeric,
  UNIQUE (answer_id, claim_key),
  CHECK (claim_type <> 'numeric' OR value IS NOT NULL)
);

CREATE TABLE claim_evidence (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  claim_id   uuid NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  fact_id    uuid REFERENCES facts(id),
  record_id  bigint REFERENCES records(id),
  CHECK (num_nonnulls(fact_id, record_id) = 1)
);
CREATE INDEX claim_evidence_claim_idx ON claim_evidence (claim_id);

CREATE TABLE verifications (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  answer_id   uuid NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  layer       text NOT NULL CHECK (layer IN ('deterministic', 'verifier', 'reconciliation')),
  check_id    text NOT NULL,                           -- D1..D8, A1..A5, R1..R5
  claim_id    uuid REFERENCES claims(id) ON DELETE CASCADE,
  verdict     text NOT NULL CHECK (verdict IN ('pass', 'fail', 'supported', 'contradicted', 'not_enough_info', 'warning')),
  detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX verifications_answer_idx ON verifications (answer_id, layer);

-- An answer may only be marked 'verified' if (a) every numeric claim cites a fact,
-- (b) no verification row failed, and (c) every claim has at least one evidence row.
CREATE OR REPLACE FUNCTION itsm.check_answer_verified() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IN ('verified', 'delivered') AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status) THEN
    IF EXISTS (SELECT 1 FROM verifications v
               WHERE v.answer_id = NEW.id AND v.verdict IN ('fail', 'contradicted', 'not_enough_info')) THEN
      RAISE EXCEPTION 'answer % has failed verifications', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM claims c WHERE c.answer_id = NEW.id
               AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.claim_id = c.id)) THEN
      RAISE EXCEPTION 'answer % has claims without evidence', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM claims c WHERE c.answer_id = NEW.id AND c.claim_type = 'numeric'
               AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.claim_id = c.id AND e.fact_id IS NOT NULL)) THEN
      RAISE EXCEPTION 'answer % has numeric claims not backed by a fact', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM verifications v WHERE v.answer_id = NEW.id AND v.layer = 'verifier') THEN
      RAISE EXCEPTION 'answer % has not been through the independent verifier', NEW.id USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER answers_verified_check BEFORE INSERT OR UPDATE ON answers
  FOR EACH ROW EXECUTE FUNCTION check_answer_verified();

-- ---------------------------------------------------------------------
-- 5. Actions, approvals (human-in-the-loop writes)
-- ---------------------------------------------------------------------
CREATE TABLE actions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id             uuid NOT NULL REFERENCES jobs(id),
  user_turn_event_id bigint NOT NULL REFERENCES session_events(id),  -- intent binding (doc 06 §5)
  tool_name          text NOT NULL,
  arguments          jsonb NOT NULL,
  risk_tier          text NOT NULL CHECK (risk_tier IN ('low', 'medium', 'high', 'critical')),
  required_approvals integer NOT NULL CHECK (required_approvals BETWEEN 0 AND 2),
  preview            jsonb NOT NULL,                   -- human-readable diff shown to approvers
  idempotency_key    text NOT NULL UNIQUE,
  status             text NOT NULL DEFAULT 'pending_approval' CHECK (status IN (
                       'pending_approval', 'approved', 'rejected', 'expired',
                       'executing', 'succeeded', 'failed', 'cancelled')),
  expires_at         timestamptz NOT NULL DEFAULT now() + interval '24 hours',
  created_at         timestamptz NOT NULL DEFAULT now(),
  executed_at        timestamptz,
  result             jsonb,
  -- policy floor per tier (policy may require more, never less)
  CHECK (required_approvals >= CASE risk_tier WHEN 'low' THEN 0 WHEN 'medium' THEN 1
                                              WHEN 'high' THEN 1 ELSE 2 END)
);
CREATE INDEX actions_job_idx ON actions (job_id, status);

CREATE TABLE approvals (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_id   uuid NOT NULL REFERENCES actions(id),
  approver_id uuid NOT NULL REFERENCES users(id),
  decision    text NOT NULL CHECK (decision IN ('approve', 'reject')),
  comment     text,
  decided_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (action_id, approver_id)
);
CREATE TRIGGER approvals_append_only BEFORE UPDATE OR DELETE ON approvals
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Separation of duties: high/critical actions cannot be approved by the requester,
-- and the approver must hold the approver role. Decisions only on pending actions.
CREATE OR REPLACE FUNCTION itsm.check_approval() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  a actions%ROWTYPE;
  requester uuid;
BEGIN
  SELECT * INTO a FROM actions WHERE id = NEW.action_id FOR UPDATE;
  IF a.status <> 'pending_approval' OR a.expires_at < now() THEN
    RAISE EXCEPTION 'action % is not awaiting approval', a.id USING ERRCODE = 'check_violation';
  END IF;
  SELECT user_id INTO requester FROM jobs WHERE id = a.job_id;
  IF a.risk_tier IN ('high', 'critical') THEN
    IF NEW.approver_id = requester THEN
      RAISE EXCEPTION 'requester cannot approve a % action', a.risk_tier USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM user_roles WHERE user_id = NEW.approver_id AND role = 'approver') THEN
      RAISE EXCEPTION 'approver role required for % actions', a.risk_tier USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approvals_check BEFORE INSERT ON approvals
  FOR EACH ROW EXECUTE FUNCTION check_approval();

-- After each decision: a reject rejects the action; enough approvals approve it.
CREATE OR REPLACE FUNCTION itsm.apply_approval() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  needed integer;
  got integer;
BEGIN
  IF NEW.decision = 'reject' THEN
    UPDATE actions SET status = 'rejected' WHERE id = NEW.action_id;
    RETURN NEW;
  END IF;
  SELECT required_approvals INTO needed FROM actions WHERE id = NEW.action_id;
  SELECT count(*) INTO got FROM approvals WHERE action_id = NEW.action_id AND decision = 'approve';
  IF got >= needed THEN
    UPDATE actions SET status = 'approved' WHERE id = NEW.action_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approvals_apply AFTER INSERT ON approvals
  FOR EACH ROW EXECUTE FUNCTION apply_approval();

-- Legal status transitions; executing requires enough approvals (or tier 'low').
CREATE OR REPLACE FUNCTION itsm.check_action_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  got integer;
BEGIN
  IF OLD.status = NEW.status THEN
    RETURN NEW;
  END IF;
  IF NOT (
       (OLD.status = 'pending_approval' AND NEW.status IN ('approved', 'rejected', 'expired', 'cancelled'))
    OR (OLD.status = 'approved'         AND NEW.status IN ('executing', 'cancelled', 'expired'))
    OR (OLD.status = 'pending_approval' AND NEW.status = 'executing' AND OLD.required_approvals = 0)
    OR (OLD.status = 'executing'        AND NEW.status IN ('succeeded', 'failed'))
  ) THEN
    RAISE EXCEPTION 'illegal action transition % -> %', OLD.status, NEW.status USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status = 'executing' THEN
    SELECT count(*) INTO got FROM approvals WHERE action_id = NEW.id AND decision = 'approve';
    IF got < NEW.required_approvals THEN
      RAISE EXCEPTION 'action % has % of % approvals', NEW.id, got, NEW.required_approvals
        USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM approvals WHERE action_id = NEW.id AND decision = 'reject') THEN
      RAISE EXCEPTION 'action % was rejected', NEW.id USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER actions_transition BEFORE UPDATE OF status ON actions
  FOR EACH ROW EXECUTE FUNCTION check_action_transition();

-- What was approved is what runs: arguments, tool and tier are immutable after creation.
CREATE OR REPLACE FUNCTION itsm.freeze_action_payload() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.arguments IS DISTINCT FROM OLD.arguments OR NEW.tool_name IS DISTINCT FROM OLD.tool_name
     OR NEW.risk_tier IS DISTINCT FROM OLD.risk_tier OR NEW.required_approvals IS DISTINCT FROM OLD.required_approvals
     OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
    RAISE EXCEPTION 'action payload is immutable once proposed' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER actions_freeze BEFORE UPDATE ON actions
  FOR EACH ROW EXECUTE FUNCTION freeze_action_payload();

-- ---------------------------------------------------------------------
-- 6. MCP registry, onboarding, drift detection, tool policy
-- ---------------------------------------------------------------------
CREATE TABLE mcp_servers (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name            text NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9_]{1,40}$'),  -- also the tool namespace
  source          text NOT NULL CHECK (source IN ('builtin', 'registry', 'admin_url', 'gitops', 'capability_gap')),
  registry_id     text,
  version         text,
  transport       text NOT NULL CHECK (transport IN ('stdio', 'streamable-http', 'in-process')),
  url             text,
  package_ref     text,                                -- npm/pypi/oci reference
  package_digest  text,                                -- integrity hash / image digest
  auth            jsonb NOT NULL DEFAULT '{}'::jsonb,  -- vault paths + auth type; never secrets
  config          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- rate limits, offload threshold, aliases
  status          text NOT NULL DEFAULT 'discovered' CHECK (status IN (
                    'discovered', 'intake_failed', 'probing', 'probe_failed', 'pending_review',
                    'rejected', 'canary', 'enabled', 'suspended', 'retired')),
  risk_score      integer CHECK (risk_score BETWEEN 0 AND 100),
  approved_by     uuid REFERENCES users(id),
  approved_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (transport <> 'stdio' OR package_digest IS NOT NULL OR source = 'builtin'),  -- local servers must be pinned
  CHECK (transport <> 'streamable-http' OR url LIKE 'https://%'),
  CHECK (status NOT IN ('canary', 'enabled') OR approved_by IS NOT NULL),
  CHECK (NOT (auth::text ~* '"(secret|password|token|api_key)"\s*:\s*"[^"]'))      -- no inline secrets
);
CREATE TRIGGER mcp_servers_touch BEFORE UPDATE ON mcp_servers
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE mcp_server_events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  server_id   uuid NOT NULL REFERENCES mcp_servers(id),
  from_status text,
  to_status   text NOT NULL,
  actor       text NOT NULL,                           -- user id or 'system'
  detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION itsm.log_server_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status THEN
    INSERT INTO mcp_server_events (server_id, from_status, to_status, actor)
    VALUES (NEW.id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status,
            coalesce(current_setting('itsm.actor', true), 'system'));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER mcp_servers_status_log AFTER INSERT OR UPDATE ON mcp_servers
  FOR EACH ROW EXECUTE FUNCTION log_server_status();

CREATE TABLE mcp_probe_reports (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  server_id   uuid NOT NULL REFERENCES mcp_servers(id),
  version     text,
  protocol_version text,
  capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
  report      jsonb NOT NULL,                          -- static + behavioural probe results
  flags       text[] NOT NULL DEFAULT '{}',
  risk_score  integer NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE mcp_tools (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  server_id            uuid NOT NULL REFERENCES mcp_servers(id),
  native_name          text NOT NULL,                  -- name as the server reports it
  name                 text NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9_]{2,63}$'),  -- namespaced
  description          text NOT NULL,
  input_schema         jsonb NOT NULL,
  output_schema        jsonb,
  annotations          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- untrusted hints
  schema_hash          text NOT NULL CHECK (schema_hash ~ '^[0-9a-f]{64}$'),
  approved_schema_hash text,
  risk_tier            text NOT NULL CHECK (risk_tier IN ('read', 'low', 'medium', 'high', 'critical')),
  allowed_roles        text[] NOT NULL DEFAULT '{orchestrator}',
  defer_loading        boolean NOT NULL DEFAULT true,
  status               text NOT NULL DEFAULT 'pending_review' CHECK (status IN (
                         'pending_review', 'enabled', 'disabled', 'suspended')),
  approved_by          uuid REFERENCES users(id),
  first_seen_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (server_id, native_name),
  -- NB: written with IS NOT NULL / IS NOT DISTINCT FROM because a CHECK whose expression is NULL passes
  CHECK (status <> 'enabled' OR (approved_by IS NOT NULL AND approved_schema_hash IS NOT NULL
                                 AND approved_schema_hash IS NOT DISTINCT FROM schema_hash)),
  CHECK (name LIKE '%\_%')                               -- must carry a namespace prefix
);

-- Drift protection: if a re-list changes the schema/description hash of an enabled
-- tool, the tool is suspended automatically until re-reviewed.
CREATE OR REPLACE FUNCTION itsm.detect_tool_drift() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.schema_hash IS DISTINCT FROM OLD.schema_hash AND OLD.status = 'enabled' THEN
    NEW.status := 'suspended';
  END IF;
  -- risk tier may only be lowered by an approver action (approved_by changes in the same update)
  IF NEW.risk_tier IS DISTINCT FROM OLD.risk_tier
     AND array_position(ARRAY['read','low','medium','high','critical'], NEW.risk_tier)
       < array_position(ARRAY['read','low','medium','high','critical'], OLD.risk_tier)
     AND NEW.approved_by IS NOT DISTINCT FROM OLD.approved_by THEN
    RAISE EXCEPTION 'lowering a tool risk tier requires a new approval' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER mcp_tools_drift BEFORE UPDATE ON mcp_tools
  FOR EACH ROW EXECUTE FUNCTION detect_tool_drift();

-- Per tool x role decision (doc 10). Most specific row wins in the policy engine.
CREATE TABLE tool_policies (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tool_name   text NOT NULL,                           -- exact name or '*' default
  user_role   text NOT NULL REFERENCES roles(name),
  decision    text NOT NULL CHECK (decision IN ('deny', 'allow', 'approve_self', 'approve_role', 'approve_two')),
  conditions  jsonb NOT NULL DEFAULT '{}'::jsonb,      -- e.g. {"max_batch": 10, "public": true}
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tool_name, user_role, conditions)
);

-- ---------------------------------------------------------------------
-- 7. Memory (semantic, episodic, preference), playbooks, org profile
-- ---------------------------------------------------------------------
CREATE TABLE memory_items (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          text NOT NULL CHECK (kind IN ('semantic', 'episodic', 'preference')),
  scope         text NOT NULL CHECK (scope IN ('org', 'user')),
  owner_user_id uuid REFERENCES users(id),
  subject_key   text NOT NULL,                         -- e.g. 'jira.project.network'
  content       text NOT NULL CHECK (length(content) <= 2000),
  version       integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  supersedes_id uuid REFERENCES memory_items(id),
  status        text NOT NULL DEFAULT 'pending_review' CHECK (status IN (
                  'pending_review', 'active', 'deprecated', 'rejected', 'expired')),
  confidence    numeric(3, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  sensitivity   text NOT NULL DEFAULT 'internal' CHECK (sensitivity IN ('public', 'internal', 'restricted')),
  source_refs   jsonb NOT NULL CHECK (jsonb_typeof(source_refs) = 'array' AND jsonb_array_length(source_refs) >= 1),
  proposed_by_session uuid REFERENCES sessions(id),
  reviewed_by   uuid REFERENCES users(id),
  reviewed_at   timestamptz,
  pinned        boolean NOT NULL DEFAULT false,        -- part of the cached org profile
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_to      timestamptz,
  expires_at    timestamptz,
  use_count     integer NOT NULL DEFAULT 0,
  last_used_at  timestamptz,
  embedding     vector(1024),
  tsv           tsvector GENERATED ALWAYS AS
                  (to_tsvector('english', subject_key || ' ' || content)) STORED,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((scope = 'user') = (owner_user_id IS NOT NULL)),
  -- org facts become visible only after human review (doc 04 §2 rule 3)
  CHECK (NOT (kind = 'semantic' AND status = 'active') OR reviewed_by IS NOT NULL),
  CHECK (NOT pinned OR (kind = 'semantic' AND status = 'active')),
  CHECK (valid_to IS NULL OR valid_to > valid_from)
);
-- one current active version per subject (per user for user scope)
CREATE UNIQUE INDEX memory_items_active_subject_uq
  ON memory_items (scope, coalesce(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid), subject_key)
  WHERE status = 'active' AND valid_to IS NULL;
CREATE INDEX memory_items_tsv_idx ON memory_items USING gin (tsv);
CREATE INDEX memory_items_embedding_idx ON memory_items USING hnsw (embedding vector_cosine_ops);
CREATE INDEX memory_items_subject_trgm ON memory_items USING gin (subject_key gin_trgm_ops);

CREATE TABLE memory_reads (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  memory_id  uuid NOT NULL REFERENCES memory_items(id),
  job_id     uuid NOT NULL REFERENCES jobs(id),
  rank       integer NOT NULL CHECK (rank >= 1),
  score      real NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE answer_memory_refs (
  answer_id      uuid NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  memory_id      uuid NOT NULL REFERENCES memory_items(id),
  memory_version integer NOT NULL,
  PRIMARY KEY (answer_id, memory_id)
);

CREATE TABLE playbooks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,                           -- e.g. cab_compliance_check
  version     integer NOT NULL CHECK (version >= 1),
  description text NOT NULL,                           -- short; used for selection
  body_md     text NOT NULL,
  rubric_ids  text[] NOT NULL DEFAULT '{}',
  required_servers text[] NOT NULL DEFAULT '{}',
  status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
  approved_by uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version),
  CHECK (status <> 'active' OR approved_by IS NOT NULL)
);
CREATE UNIQUE INDEX playbooks_one_active_uq ON playbooks (name) WHERE status = 'active';

CREATE TABLE org_profiles (
  version     integer PRIMARY KEY,
  content     text NOT NULL,                           -- rendered L2 block, <= ~3k tokens
  memory_ids  uuid[] NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 8. Telemetry: model calls, tool calls
-- ---------------------------------------------------------------------
CREATE TABLE llm_calls (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id             uuid REFERENCES jobs(id),
  step_id            uuid REFERENCES job_steps(id),
  agent_role         text NOT NULL CHECK (agent_role IN ('orchestrator', 'map_worker', 'verifier', 'explorer', 'adjudicator', 'judge', 'summariser')),
  model              text NOT NULL,                    -- model actually served (may differ after fallback)
  api_request_id     text,
  input_tokens       integer NOT NULL DEFAULT 0,
  output_tokens      integer NOT NULL DEFAULT 0,
  cache_read_tokens  integer NOT NULL DEFAULT 0,
  cache_write_tokens integer NOT NULL DEFAULT 0,
  stop_reason        text,
  latency_ms         integer,
  cost_usd           numeric(12, 6),
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_job_idx ON llm_calls (job_id);

CREATE TABLE tool_calls (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id        uuid REFERENCES jobs(id),
  step_id       uuid REFERENCES job_steps(id),
  tool_name     text NOT NULL,
  arguments     jsonb NOT NULL,
  policy_decision text NOT NULL CHECK (policy_decision IN ('allow', 'deny', 'pending_approval')),
  is_error      boolean NOT NULL DEFAULT false,
  result_handle text,                                  -- when offloaded to the record store
  result_tokens integer,
  latency_ms    integer,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tool_calls_job_idx ON tool_calls (job_id);

-- ---------------------------------------------------------------------
-- 9. Tamper-evident audit log (hash chain)
-- ---------------------------------------------------------------------
CREATE TABLE audit_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor_type  text NOT NULL CHECK (actor_type IN ('user', 'agent', 'system')),
  actor_id    text NOT NULL,
  action      text NOT NULL,                           -- e.g. action.executed, memory.approved, mcp.enabled
  target      text NOT NULL,
  detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
  prev_hash   text NOT NULL,
  hash        text NOT NULL UNIQUE
);

CREATE OR REPLACE FUNCTION itsm.audit_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  last text;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('itsm.audit_log'));   -- serialise chain appends
  SELECT hash INTO last FROM audit_log ORDER BY id DESC LIMIT 1;
  NEW.prev_hash := coalesce(last, repeat('0', 64));
  NEW.hash := encode(sha256(convert_to(
                NEW.prev_hash || '|' || NEW.at::text || '|' || NEW.actor_type || '|' || NEW.actor_id || '|' ||
                NEW.action || '|' || NEW.target || '|' || NEW.detail::text, 'UTF8')), 'hex');
  RETURN NEW;
END $$;
CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_chain();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Verifies the whole chain; returns the first broken id or NULL.
CREATE OR REPLACE FUNCTION itsm.audit_verify() RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE
  r record;
  expected_prev text := repeat('0', 64);
BEGIN
  FOR r IN SELECT * FROM audit_log ORDER BY id LOOP
    IF r.prev_hash <> expected_prev OR r.hash <> encode(sha256(convert_to(
         r.prev_hash || '|' || r.at::text || '|' || r.actor_type || '|' || r.actor_id || '|' ||
         r.action || '|' || r.target || '|' || r.detail::text, 'UTF8')), 'hex') THEN
      RETURN r.id;
    END IF;
    expected_prev := r.hash;
  END LOOP;
  RETURN NULL;
END $$;

-- ---------------------------------------------------------------------
-- 10. Evaluation
-- ---------------------------------------------------------------------
CREATE TABLE eval_cases (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite       text NOT NULL,                           -- golden_counts, rubric_network, injection, ...
  name        text NOT NULL,
  input       jsonb NOT NULL,                          -- user request + simulated time + user tz
  fixtures_uri text NOT NULL,                          -- recorded source API responses
  expected    jsonb NOT NULL,                          -- counts, ids, labels, must/must-not claims
  tags        text[] NOT NULL DEFAULT '{}',
  origin      text NOT NULL DEFAULT 'authored' CHECK (origin IN ('authored', 'production_incident', 'synthetic', 'onboarding')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (suite, name)
);

CREATE TABLE eval_runs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite           text NOT NULL,
  harness_version text NOT NULL,                       -- git sha of prompts + harness
  model_routing   jsonb NOT NULL,
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  metrics         jsonb
);

CREATE TABLE eval_results (
  run_id     uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  case_id    uuid NOT NULL REFERENCES eval_cases(id),
  trial      integer NOT NULL DEFAULT 1 CHECK (trial >= 1),
  passed     boolean NOT NULL,
  scores     jsonb NOT NULL,
  transcript_uri text,
  PRIMARY KEY (run_id, case_id, trial)
);

-- ---------------------------------------------------------------------
-- 11. Convenience views
-- ---------------------------------------------------------------------
-- Coverage of a map phase: every member record must have exactly one final label.
CREATE OR REPLACE FUNCTION itsm.label_coverage(p_result_set uuid, p_job uuid, p_rubric text)
RETURNS TABLE (members bigint, labelled bigint, missing bigint, needs_review bigint)
LANGUAGE sql STABLE AS $$
  SELECT count(*),
         count(a.record_id),
         count(*) - count(a.record_id),
         count(*) FILTER (WHERE a.needs_human_review)
  FROM result_set_members m
  LEFT JOIN record_annotations a
         ON a.record_id = m.record_id AND a.job_id = p_job AND a.rubric_id = p_rubric AND a.is_final
  WHERE m.result_set_id = p_result_set;
$$;

-- Pending approvals for the approval UI.
CREATE OR REPLACE VIEW itsm.v_pending_approvals AS
SELECT a.id AS action_id, a.tool_name, a.risk_tier, a.required_approvals,
       (SELECT count(*) FROM approvals p WHERE p.action_id = a.id AND p.decision = 'approve') AS approvals,
       j.user_id AS requester_id, a.preview, a.expires_at
FROM actions a JOIN jobs j ON j.id = a.job_id
WHERE a.status = 'pending_approval' AND a.expires_at > now();
