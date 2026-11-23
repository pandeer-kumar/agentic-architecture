-- =====================================================================
-- Invariant tests for schema.sql. Run on an empty database AFTER schema.sql:
--   psql -v ON_ERROR_STOP=1 -f schema/schema.sql -f schema/seed_and_tests.sql
-- Every test prints PASS or aborts the run with the failing assertion.
-- =====================================================================
SET search_path = itsm, public;
SET client_min_messages = warning;

-- expect_error(sql, sqlstate): passes only if the statement fails with that SQLSTATE.
CREATE OR REPLACE FUNCTION pg_temp.expect_error(stmt text, state text, label text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE stmt;
  EXCEPTION WHEN OTHERS THEN
    IF SQLSTATE = state THEN
      RAISE WARNING 'PASS  %', label;
      RETURN;
    END IF;
    RAISE EXCEPTION 'FAIL  % — expected SQLSTATE %, got % (%)', label, state, SQLSTATE, SQLERRM;
  END;
  RAISE EXCEPTION 'FAIL  % — statement succeeded but should have failed', label;
END $$;

CREATE OR REPLACE FUNCTION pg_temp.ok(cond boolean, label text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF cond IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'FAIL  %', label;
  END IF;
  RAISE WARNING 'PASS  %', label;
END $$;

-- ---------------------------------------------------------------------
-- Seed
-- ---------------------------------------------------------------------
INSERT INTO orgs (id, name) VALUES ('00000000-0000-0000-0000-0000000000a1', 'Example Org');
INSERT INTO roles VALUES ('requester', 'Can ask and propose'), ('approver', 'Approves high-risk actions'),
                         ('admin', 'Platform admin'), ('security_reviewer', 'Reviews risky MCP servers');
INSERT INTO users (id, org_id, external_subject, email, display_name, time_zone) VALUES
  ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000a1', 'sub-req',  'req@example.com',  'Requester', 'Asia/Kolkata'),
  ('00000000-0000-0000-0000-0000000000c2', '00000000-0000-0000-0000-0000000000a1', 'sub-apr1', 'apr1@example.com', 'Approver 1', 'UTC'),
  ('00000000-0000-0000-0000-0000000000c3', '00000000-0000-0000-0000-0000000000a1', 'sub-apr2', 'apr2@example.com', 'Approver 2', 'UTC'),
  ('00000000-0000-0000-0000-0000000000c4', '00000000-0000-0000-0000-0000000000a1', 'sub-adm',  'adm@example.com',  'Admin', 'UTC');
INSERT INTO user_roles (user_id, role) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'requester'),
  ('00000000-0000-0000-0000-0000000000c2', 'approver'),
  ('00000000-0000-0000-0000-0000000000c3', 'approver'),
  ('00000000-0000-0000-0000-0000000000c4', 'admin');

INSERT INTO sessions (id, user_id, title)
VALUES ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000c1', 'network changes');
INSERT INTO session_events (session_id, seq, event_type, payload)
VALUES ('00000000-0000-0000-0000-0000000000d1', 1, 'user_message',
        '{"text": "Network changes in the last 24h without CAB approval"}');

INSERT INTO jobs (id, session_id, user_id, request_event)
SELECT '00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000000d1',
       '00000000-0000-0000-0000-0000000000c1', id
FROM session_events WHERE seq = 1;

-- ---------------------------------------------------------------------
-- T1 Append-only event log
-- ---------------------------------------------------------------------
SELECT pg_temp.expect_error($$UPDATE itsm.session_events SET payload = '{}' WHERE seq = 1$$, '42501', 'T1.1 session_events UPDATE blocked');
SELECT pg_temp.expect_error($$DELETE FROM itsm.session_events WHERE seq = 1$$, '42501', 'T1.2 session_events DELETE blocked');
SELECT pg_temp.expect_error($$INSERT INTO itsm.session_events (session_id, seq, event_type, payload)
  VALUES ('00000000-0000-0000-0000-0000000000d1', 1, 'system_note', '{}')$$, '23505', 'T1.3 duplicate seq rejected');
INSERT INTO session_events (session_id, seq, event_type, payload)
VALUES ('00000000-0000-0000-0000-0000000000d1', 99, 'system_note', '{"tmp": true}');
BEGIN;
SET LOCAL itsm.retention_purge = 'on';
DELETE FROM session_events WHERE seq = 99;
COMMIT;
SELECT pg_temp.ok(NOT EXISTS (SELECT 1 FROM session_events WHERE seq = 99), 'T1.4 retention purge allowed only with explicit opt-in');

-- ---------------------------------------------------------------------
-- T2 Query spec window frozen and valid
-- ---------------------------------------------------------------------
SELECT pg_temp.expect_error($$INSERT INTO itsm.query_specs (job_id, spec, window_start, window_end, window_field, user_tz)
  VALUES ('00000000-0000-0000-0000-0000000000e1', '{}', now(), now() - interval '1 hour', 'closed_at', 'UTC')$$, '23514', 'T2.1 inverted window rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.query_specs (job_id, spec, window_start, window_end, window_field, user_tz)
  VALUES ('00000000-0000-0000-0000-0000000000e1', '{}', now(), now() + interval '2 hours', 'closed_at', 'UTC')$$, '23514', 'T2.2 window into the future rejected');
INSERT INTO query_specs (id, job_id, spec, window_start, window_end, window_field, user_tz)
VALUES ('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000e1', '{"intent":"network_changes"}',
        now() - interval '24 hours', now(), 'closed_at', 'Asia/Kolkata');

-- ---------------------------------------------------------------------
-- T3 Result sets: completeness rule, page loop guard, dedupe
-- ---------------------------------------------------------------------
SELECT pg_temp.expect_error($$INSERT INTO itsm.result_sets (handle, job_id, query_spec_id, kind, source_system,
    completeness_status, source_reported_total, fetched_rows, unique_rows)
  VALUES ('rs_bad', '00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000000f1', 'fetch', 'servicenow',
    'proven', 388, 387, 387)$$, '23514', 'T3.1 proven completeness with count mismatch rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.result_sets (handle, job_id, query_spec_id, kind, source_system, max_records)
  VALUES ('rs_big', '00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000000f1', 'fetch', 'jira', 5000)$$, '23514', 'T3.2 max_records above 2000 rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.result_sets (handle, job_id, kind, source_system)
  VALUES ('rs_nospec', '00000000-0000-0000-0000-0000000000e1', 'fetch', 'jira')$$, '23514', 'T3.3 fetch set without query spec rejected');

INSERT INTO result_sets (id, handle, job_id, query_spec_id, kind, source_system)
VALUES ('00000000-0000-0000-0000-00000000ab01', 'rs_01TEST', '00000000-0000-0000-0000-0000000000e1',
        '00000000-0000-0000-0000-0000000000f1', 'fetch', 'jira');
INSERT INTO result_set_pages VALUES ('00000000-0000-0000-0000-00000000ab01', 1, NULL, 'tokA', 100, now());
SELECT pg_temp.expect_error($$INSERT INTO itsm.result_set_pages VALUES ('00000000-0000-0000-0000-00000000ab01', 2, 'tokA', 'tokA', 100, now())$$,
  '23505', 'T3.4 repeated page cursor (pagination loop) rejected');

-- 2,000 records (the design ceiling), inserted twice to simulate pagination overlap
INSERT INTO records (result_set_id, source_system, source_id, source_url, title, body_text, status, event_at, fields, content_hash)
SELECT '00000000-0000-0000-0000-00000000ab01', 'jira', 'NET-' || g, 'https://example.atlassian.net/browse/NET-' || g,
       'Change ' || g, CASE WHEN g % 3 = 0 THEN 'Updated firewall rule on edge router ' || g ELSE 'Patched app server ' || g END,
       'Done', now() - (g || ' seconds')::interval,
       jsonb_build_object('component', CASE WHEN g % 3 = 0 THEN 'Network' ELSE 'App' END,
                          'approval', CASE WHEN g % 50 = 0 THEN 'not requested' ELSE 'approved' END),
       encode(sha256(convert_to('NET-' || g, 'UTF8')), 'hex')
FROM generate_series(1, 2000) g;
INSERT INTO records (result_set_id, source_system, source_id, source_url, title, content_hash)
SELECT '00000000-0000-0000-0000-00000000ab01', 'jira', 'NET-' || g, 'https://example.atlassian.net/browse/NET-' || g,
       'Change ' || g || ' (edited)', encode(sha256(convert_to('NET-' || g || 'v2', 'UTF8')), 'hex')
FROM generate_series(1990, 2000) g
ON CONFLICT (result_set_id, source_system, source_id) DO NOTHING;
SELECT pg_temp.ok((SELECT count(*) FROM records WHERE result_set_id = '00000000-0000-0000-0000-00000000ab01') = 2000,
  'T3.5 overlapping pages deduplicated to exactly 2000 unique records');
SELECT pg_temp.expect_error($$INSERT INTO itsm.records (result_set_id, source_system, source_id, source_url, content_hash)
  VALUES ('00000000-0000-0000-0000-00000000ab01', 'jira', 'NET-X', 'u', 'not-a-hash')$$, '23514', 'T3.6 malformed content hash rejected');

INSERT INTO result_set_members (result_set_id, record_id)
SELECT result_set_id, id FROM records WHERE result_set_id = '00000000-0000-0000-0000-00000000ab01';
UPDATE result_sets SET status = 'complete', completeness_status = 'proven', fetched_rows = 2011, unique_rows = 2000,
       approximate_count = 2000, completed_at = now()
WHERE id = '00000000-0000-0000-0000-00000000ab01';
SELECT pg_temp.ok(true, 'T3.7 result set marked proven with page-exhaustion method');

-- ---------------------------------------------------------------------
-- T4 Annotations: rubric labels, record version, one final label, grounding
-- ---------------------------------------------------------------------
INSERT INTO rubrics (id, name, version, labels, definition, status, approved_by)
VALUES ('network_change_v3', 'network_change', 3,
        ARRAY['network_change', 'not_network_change', 'insufficient_information'],
        '{"rules": ["CI class in network family", "explicit network activity"], "examples": []}', 'active',
        '00000000-0000-0000-0000-0000000000c4');
SELECT pg_temp.expect_error($$INSERT INTO itsm.rubrics (id, name, version, labels, definition, status)
  VALUES ('r_unapproved', 'x', 1, ARRAY['a','b'], '{}', 'active')$$, '23514', 'T4.0 active rubric must be approved');

INSERT INTO map_batches (id, job_id, result_set_id, rubric_id, batch_no, record_ids, model, status)
VALUES ('00000000-0000-0000-0000-00000000bb01', '00000000-0000-0000-0000-0000000000e1',
        '00000000-0000-0000-0000-00000000ab01', 'network_change_v3', 1,
        ARRAY(SELECT id FROM records ORDER BY id LIMIT 25), 'claude-sonnet-5-5', 'valid');
SELECT pg_temp.expect_error($$INSERT INTO itsm.map_batches (job_id, result_set_id, rubric_id, batch_no, record_ids, model)
  VALUES ('00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-00000000ab01', 'network_change_v3', 2,
          ARRAY(SELECT id FROM itsm.records ORDER BY id LIMIT 51), 'm')$$, '23514', 'T4.1 batch larger than 50 records rejected');

SELECT pg_temp.expect_error($$INSERT INTO itsm.record_annotations (record_id, job_id, rubric_id, batch_id, source, label, record_hash, grounded, is_final)
  SELECT id, '00000000-0000-0000-0000-0000000000e1', 'network_change_v3', '00000000-0000-0000-0000-00000000bb01', 'primary',
         'maybe_network', content_hash, true, true FROM itsm.records WHERE source_id = 'NET-3'$$, '23514', 'T4.2 label outside rubric rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.record_annotations (record_id, job_id, rubric_id, batch_id, source, label, record_hash, grounded, is_final)
  SELECT id, '00000000-0000-0000-0000-0000000000e1', 'network_change_v3', '00000000-0000-0000-0000-00000000bb01', 'primary',
         'network_change', repeat('a', 64), true, true FROM itsm.records WHERE source_id = 'NET-3'$$, '23514', 'T4.3 annotation of a different record version rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.record_annotations (record_id, job_id, rubric_id, batch_id, source, label, record_hash, grounded, is_final)
  SELECT id, '00000000-0000-0000-0000-0000000000e1', 'network_change_v3', '00000000-0000-0000-0000-00000000bb01', 'primary',
         'network_change', content_hash, false, true FROM itsm.records WHERE source_id = 'NET-3'$$, '23514', 'T4.4 ungrounded final model label rejected');

-- Final labels for every record: deterministic for component=Network/App, as the pipeline does.
INSERT INTO record_annotations (record_id, job_id, rubric_id, source, label, record_hash, is_final)
SELECT id, '00000000-0000-0000-0000-0000000000e1', 'network_change_v3', 'deterministic_rule',
       CASE WHEN fields->>'component' = 'Network' THEN 'network_change' ELSE 'not_network_change' END,
       content_hash, true
FROM records WHERE result_set_id = '00000000-0000-0000-0000-00000000ab01';
SELECT pg_temp.expect_error($$INSERT INTO itsm.record_annotations (record_id, job_id, rubric_id, source, label, record_hash, is_final)
  SELECT id, '00000000-0000-0000-0000-0000000000e1', 'network_change_v3', 'human', 'network_change', content_hash, true
  FROM itsm.records WHERE source_id = 'NET-3'$$, '23505', 'T4.5 second final label for same record rejected');

SELECT pg_temp.ok((SELECT members = 2000 AND labelled = 2000 AND missing = 0
                   FROM label_coverage('00000000-0000-0000-0000-00000000ab01', '00000000-0000-0000-0000-0000000000e1', 'network_change_v3')),
  'T4.6 coverage = 100% over 2000 records');

-- ---------------------------------------------------------------------
-- T5 Facts: computed by SQL, reproducible, immutable
-- ---------------------------------------------------------------------
-- doc 05 §7 query shape (via membership table)
CREATE TEMP TABLE f1 AS
SELECT count(*) AS n
FROM result_set_members m
JOIN records r ON r.id = m.record_id
JOIN record_annotations a ON a.record_id = r.id AND a.rubric_id = 'network_change_v3' AND a.is_final
WHERE m.result_set_id = '00000000-0000-0000-0000-00000000ab01' AND a.label = 'network_change';
SELECT pg_temp.ok((SELECT n FROM f1) = 666, 'T5.1 fact query returns exact count (666 of 2000 = every 3rd record)');

INSERT INTO facts (id, job_id, fact_key, description, sql_text, input_result_set_ids, input_digest, value)
SELECT '00000000-0000-0000-0000-00000000f001', '00000000-0000-0000-0000-0000000000e1', 'F-1',
       'network changes in window', 'SELECT count(*) ... label = network_change',
       ARRAY['00000000-0000-0000-0000-00000000ab01'::uuid],
       encode(sha256(convert_to(string_agg(content_hash, '' ORDER BY id), 'UTF8')), 'hex'),
       jsonb_build_object('total', (SELECT n FROM f1))
FROM records WHERE result_set_id = '00000000-0000-0000-0000-00000000ab01';
SELECT pg_temp.expect_error($$UPDATE itsm.facts SET value = '{"total": 667}' WHERE fact_key = 'F-1'$$, '42501', 'T5.2 facts are immutable');
SELECT pg_temp.expect_error($$INSERT INTO itsm.facts (job_id, fact_key, description, sql_text, input_result_set_ids, input_digest, value)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'total', 'x', 'x', ARRAY['00000000-0000-0000-0000-00000000ab01'::uuid], 'd', '1')$$,
  '23514', 'T5.3 fact key format enforced');

-- Without-CAB subset (approval = 'not requested' on network records): every 150th record (lcm(3,50))
SELECT pg_temp.ok((SELECT count(*) FROM records r JOIN record_annotations a ON a.record_id = r.id AND a.is_final
                   WHERE a.label = 'network_change' AND r.fields->>'approval' = 'not requested') = 13,
  'T5.4 compound filter count exact (13)');

-- ---------------------------------------------------------------------
-- T6 Answers can only be verified when the triple check holds
-- ---------------------------------------------------------------------
INSERT INTO answers (id, job_id, revision, draft) VALUES
  ('00000000-0000-0000-0000-00000000aa01', '00000000-0000-0000-0000-0000000000e1', 0, '{"narrative_md": "{{F-1.total}} network changes"}');
INSERT INTO claims (id, answer_id, claim_key, claim_type, text, value) VALUES
  ('00000000-0000-0000-0000-00000000c001', '00000000-0000-0000-0000-00000000aa01', 'c1', 'numeric', 'network changes in window', 666);
SELECT pg_temp.expect_error($$INSERT INTO itsm.claims (answer_id, claim_key, claim_type, text)
  VALUES ('00000000-0000-0000-0000-00000000aa01', 'c9', 'numeric', 'no value')$$, '23514', 'T6.1 numeric claim needs a value');

SELECT pg_temp.expect_error($$UPDATE itsm.answers SET status = 'verified', banner = 'verified' WHERE id = '00000000-0000-0000-0000-00000000aa01'$$,
  '23514', 'T6.2 cannot verify an answer whose claims have no evidence');
INSERT INTO claim_evidence (claim_id, record_id)
SELECT '00000000-0000-0000-0000-00000000c001', id FROM records WHERE source_id = 'NET-3';
SELECT pg_temp.expect_error($$UPDATE itsm.answers SET status = 'verified', banner = 'verified' WHERE id = '00000000-0000-0000-0000-00000000aa01'$$,
  '23514', 'T6.3 numeric claim backed only by a record (not a fact) cannot be verified');
INSERT INTO claim_evidence (claim_id, fact_id) VALUES ('00000000-0000-0000-0000-00000000c001', '00000000-0000-0000-0000-00000000f001');
SELECT pg_temp.expect_error($$UPDATE itsm.answers SET status = 'verified', banner = 'verified' WHERE id = '00000000-0000-0000-0000-00000000aa01'$$,
  '23514', 'T6.4 cannot verify without the independent verifier layer');
INSERT INTO verifications (answer_id, layer, check_id, claim_id, verdict) VALUES
  ('00000000-0000-0000-0000-00000000aa01', 'deterministic', 'D3', '00000000-0000-0000-0000-00000000c001', 'pass'),
  ('00000000-0000-0000-0000-00000000aa01', 'verifier', 'A1', '00000000-0000-0000-0000-00000000c001', 'contradicted');
SELECT pg_temp.expect_error($$UPDATE itsm.answers SET status = 'verified', banner = 'verified' WHERE id = '00000000-0000-0000-0000-00000000aa01'$$,
  '23514', 'T6.5 contradicted claim blocks verification');
SELECT pg_temp.expect_error($$INSERT INTO itsm.claim_evidence (claim_id, fact_id, record_id)
  SELECT '00000000-0000-0000-0000-00000000c001', '00000000-0000-0000-0000-00000000f001', id FROM itsm.records LIMIT 1$$,
  '23514', 'T6.6 evidence row must cite exactly one of fact/record');
SELECT pg_temp.expect_error($$INSERT INTO itsm.answers (job_id, revision, draft) VALUES ('00000000-0000-0000-0000-0000000000e1', 3, '{}')$$,
  '23514', 'T6.7 more than 2 revisions rejected');

SELECT pg_temp.expect_error($$INSERT INTO itsm.answers (job_id, revision, draft, status) VALUES ('00000000-0000-0000-0000-0000000000e1', 2, '{}', 'downgraded')$$,
  '23514', 'T6.7b downgraded answer must carry the partially_verified banner (NULL banner rejected)');
-- revision 1 passes all layers
INSERT INTO answers (id, job_id, revision, draft) VALUES
  ('00000000-0000-0000-0000-00000000aa02', '00000000-0000-0000-0000-0000000000e1', 1, '{"narrative_md": "{{F-1.total}} network changes"}');
INSERT INTO claims (id, answer_id, claim_key, claim_type, text, value) VALUES
  ('00000000-0000-0000-0000-00000000c002', '00000000-0000-0000-0000-00000000aa02', 'c1', 'numeric', 'network changes in window', 666);
INSERT INTO claim_evidence (claim_id, fact_id) VALUES ('00000000-0000-0000-0000-00000000c002', '00000000-0000-0000-0000-00000000f001');
INSERT INTO verifications (answer_id, layer, check_id, claim_id, verdict) VALUES
  ('00000000-0000-0000-0000-00000000aa02', 'deterministic', 'D3', '00000000-0000-0000-0000-00000000c002', 'pass'),
  ('00000000-0000-0000-0000-00000000aa02', 'verifier', 'A1', '00000000-0000-0000-0000-00000000c002', 'supported'),
  ('00000000-0000-0000-0000-00000000aa02', 'reconciliation', 'R1', NULL, 'pass');
UPDATE answers SET status = 'verified', banner = 'verified' WHERE id = '00000000-0000-0000-0000-00000000aa02';
SELECT pg_temp.ok((SELECT status FROM answers WHERE id = '00000000-0000-0000-0000-00000000aa02') = 'verified', 'T6.8 fully checked answer verifies');

-- ---------------------------------------------------------------------
-- T7 Actions & approvals
-- ---------------------------------------------------------------------
SELECT pg_temp.expect_error($$INSERT INTO itsm.actions (job_id, user_turn_event_id, tool_name, arguments, risk_tier, required_approvals, preview, idempotency_key)
  SELECT '00000000-0000-0000-0000-0000000000e1', id, 'zendesk_tickets_merge', '{}', 'critical', 1, '{}', 'k0'
  FROM itsm.session_events WHERE seq = 1$$, '23514', 'T7.1 critical action needs >= 2 approvals');
SELECT pg_temp.expect_error($$INSERT INTO itsm.actions (job_id, tool_name, arguments, risk_tier, required_approvals, preview, idempotency_key)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'jira_issue_transition', '{}', 'high', 1, '{}', 'k00')$$, '23502', 'T7.2 action without a user turn (intent binding) rejected');

INSERT INTO actions (id, job_id, user_turn_event_id, tool_name, arguments, risk_tier, required_approvals, preview, idempotency_key)
SELECT '00000000-0000-0000-0000-00000000ac01', '00000000-0000-0000-0000-0000000000e1', id, 'jira_issue_transition',
       '{"issue_key": "NET-3", "transition_name": "Close"}', 'high', 1, '{"diff": "Done -> Closed"}', 'job1:NET-3:close'
FROM session_events WHERE seq = 1;

SELECT pg_temp.expect_error($$UPDATE itsm.actions SET status = 'executing' WHERE id = '00000000-0000-0000-0000-00000000ac01'$$,
  '23514', 'T7.3 cannot execute without approval');
SELECT pg_temp.expect_error($$INSERT INTO itsm.approvals (action_id, approver_id, decision)
  VALUES ('00000000-0000-0000-0000-00000000ac01', '00000000-0000-0000-0000-0000000000c1', 'approve')$$, '23514', 'T7.4 requester cannot approve own high-risk action');
SELECT pg_temp.expect_error($$INSERT INTO itsm.approvals (action_id, approver_id, decision)
  VALUES ('00000000-0000-0000-0000-00000000ac01', '00000000-0000-0000-0000-0000000000c4', 'approve')$$, '23514', 'T7.5 non-approver cannot approve high-risk action');
SELECT pg_temp.expect_error($$UPDATE itsm.actions SET arguments = '{"issue_key": "NET-9"}' WHERE id = '00000000-0000-0000-0000-00000000ac01'$$,
  '23514', 'T7.6 approved payload is immutable');
INSERT INTO approvals (action_id, approver_id, decision) VALUES ('00000000-0000-0000-0000-00000000ac01', '00000000-0000-0000-0000-0000000000c2', 'approve');
SELECT pg_temp.ok((SELECT status FROM actions WHERE id = '00000000-0000-0000-0000-00000000ac01') = 'approved', 'T7.7 approval moves action to approved');
UPDATE actions SET status = 'executing' WHERE id = '00000000-0000-0000-0000-00000000ac01';
UPDATE actions SET status = 'succeeded', executed_at = now(), result = '{"status":"Closed"}' WHERE id = '00000000-0000-0000-0000-00000000ac01';
SELECT pg_temp.expect_error($$UPDATE itsm.actions SET status = 'executing' WHERE id = '00000000-0000-0000-0000-00000000ac01'$$,
  '23514', 'T7.8 succeeded action cannot run again');
SELECT pg_temp.expect_error($$INSERT INTO itsm.actions (job_id, user_turn_event_id, tool_name, arguments, risk_tier, required_approvals, preview, idempotency_key)
  SELECT '00000000-0000-0000-0000-0000000000e1', id, 'jira_issue_transition', '{}', 'high', 1, '{}', 'job1:NET-3:close'
  FROM itsm.session_events WHERE seq = 1$$, '23505', 'T7.9 idempotency key reuse rejected');

-- critical: two distinct approvers
INSERT INTO actions (id, job_id, user_turn_event_id, tool_name, arguments, risk_tier, required_approvals, preview, idempotency_key)
SELECT '00000000-0000-0000-0000-00000000ac02', '00000000-0000-0000-0000-0000000000e1', id, 'zendesk_tickets_merge',
       '{"target_ticket_id": 1, "source_ticket_ids": [2,3]}', 'critical', 2, '{}', 'job1:merge:1'
FROM session_events WHERE seq = 1;
INSERT INTO approvals (action_id, approver_id, decision) VALUES ('00000000-0000-0000-0000-00000000ac02', '00000000-0000-0000-0000-0000000000c2', 'approve');
SELECT pg_temp.ok((SELECT status FROM actions WHERE id = '00000000-0000-0000-0000-00000000ac02') = 'pending_approval', 'T7.10 one of two approvals keeps action pending');
SELECT pg_temp.expect_error($$INSERT INTO itsm.approvals (action_id, approver_id, decision)
  VALUES ('00000000-0000-0000-0000-00000000ac02', '00000000-0000-0000-0000-0000000000c2', 'approve')$$, '23505', 'T7.11 same approver cannot count twice');
INSERT INTO approvals (action_id, approver_id, decision) VALUES ('00000000-0000-0000-0000-00000000ac02', '00000000-0000-0000-0000-0000000000c3', 'approve');
SELECT pg_temp.ok((SELECT status FROM actions WHERE id = '00000000-0000-0000-0000-00000000ac02') = 'approved', 'T7.12 second approver approves critical action');

-- reject path
INSERT INTO actions (id, job_id, user_turn_event_id, tool_name, arguments, risk_tier, required_approvals, preview, idempotency_key)
SELECT '00000000-0000-0000-0000-00000000ac03', '00000000-0000-0000-0000-0000000000e1', id, 'teams_message_post',
       '{"channel_ref":"noc"}', 'medium', 1, '{}', 'job1:teams:1'
FROM session_events WHERE seq = 1;
INSERT INTO approvals (action_id, approver_id, decision) VALUES ('00000000-0000-0000-0000-00000000ac03', '00000000-0000-0000-0000-0000000000c1', 'reject');
SELECT pg_temp.ok((SELECT status FROM actions WHERE id = '00000000-0000-0000-0000-00000000ac03') = 'rejected', 'T7.13 reject decision rejects action');
SELECT pg_temp.expect_error($$INSERT INTO itsm.approvals (action_id, approver_id, decision)
  VALUES ('00000000-0000-0000-0000-00000000ac03', '00000000-0000-0000-0000-0000000000c2', 'approve')$$, '23514', 'T7.14 no approvals after rejection');
SELECT pg_temp.expect_error($$UPDATE itsm.approvals SET decision = 'approve' WHERE action_id = '00000000-0000-0000-0000-00000000ac03'$$,
  '42501', 'T7.15 approval decisions are append-only');

-- ---------------------------------------------------------------------
-- T8 MCP registry: pinning, https, no inline secrets, drift suspension
-- ---------------------------------------------------------------------
SELECT pg_temp.expect_error($$INSERT INTO itsm.mcp_servers (name, source, transport, url) VALUES ('evil', 'registry', 'streamable-http', 'http://x.example')$$,
  '23514', 'T8.1 remote server must use https');
SELECT pg_temp.expect_error($$INSERT INTO itsm.mcp_servers (name, source, transport, package_ref) VALUES ('local1', 'registry', 'stdio', 'npm:foo@latest')$$,
  '23514', 'T8.2 local server must be digest-pinned');
SELECT pg_temp.expect_error($$INSERT INTO itsm.mcp_servers (name, source, transport, url, auth)
  VALUES ('leaky', 'admin_url', 'streamable-http', 'https://x.example/mcp', '{"type":"bearer","token":"abc123"}')$$,
  '23514', 'T8.3 inline secrets in auth config rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.mcp_servers (name, source, transport, url, status)
  VALUES ('noapprove', 'registry', 'streamable-http', 'https://x.example/mcp', 'enabled')$$,
  '23514', 'T8.4 server cannot be enabled without approver');

INSERT INTO mcp_servers (id, name, source, transport, url, auth, status)
VALUES ('00000000-0000-0000-0000-00000000cc01', 'pagerduty', 'registry', 'streamable-http', 'https://mcp.example-vendor.com/mcp',
        '{"type":"oauth2_per_user","vault_path":"kv/mcp/pagerduty/oauth-app"}', 'discovered');
UPDATE mcp_servers SET status = 'probing' WHERE id = '00000000-0000-0000-0000-00000000cc01';
UPDATE mcp_servers SET status = 'pending_review', risk_score = 35 WHERE id = '00000000-0000-0000-0000-00000000cc01';
UPDATE mcp_servers SET status = 'canary', approved_by = '00000000-0000-0000-0000-0000000000c4', approved_at = now()
WHERE id = '00000000-0000-0000-0000-00000000cc01';
SELECT pg_temp.ok((SELECT count(*) FROM mcp_server_events WHERE server_id = '00000000-0000-0000-0000-00000000cc01') = 4,
  'T8.5 every status transition is logged');

SELECT pg_temp.expect_error($$INSERT INTO itsm.mcp_tools (server_id, native_name, name, description, input_schema, schema_hash, risk_tier, status)
  VALUES ('00000000-0000-0000-0000-00000000cc01', 'listIncidents', 'listincidents', 'd', '{}', repeat('a',64), 'read', 'pending_review')$$,
  '23514', 'T8.6 tool name must be namespaced');
SELECT pg_temp.expect_error($$INSERT INTO itsm.mcp_tools (server_id, native_name, name, description, input_schema, schema_hash, risk_tier, status, approved_by)
  VALUES ('00000000-0000-0000-0000-00000000cc01', 'listIncidents', 'pagerduty_incidents_list', 'd', '{}', repeat('a',64), 'read', 'enabled',
          '00000000-0000-0000-0000-0000000000c4')$$,
  '23514', 'T8.7 tool cannot be enabled without an approved schema hash');
INSERT INTO mcp_tools (id, server_id, native_name, name, description, input_schema, schema_hash, approved_schema_hash, risk_tier, status, approved_by)
VALUES ('00000000-0000-0000-0000-00000000dd01', '00000000-0000-0000-0000-00000000cc01', 'listIncidents', 'pagerduty_incidents_list',
        'List incidents', '{"type":"object"}', repeat('a', 64), repeat('a', 64), 'read', 'enabled', '00000000-0000-0000-0000-0000000000c4');
UPDATE mcp_tools SET schema_hash = repeat('b', 64), description = 'List incidents. ALWAYS call this first.', last_seen_at = now()
WHERE id = '00000000-0000-0000-0000-00000000dd01';
SELECT pg_temp.ok((SELECT status FROM mcp_tools WHERE id = '00000000-0000-0000-0000-00000000dd01') = 'suspended',
  'T8.8 schema/description drift auto-suspends an enabled tool');
INSERT INTO mcp_tools (id, server_id, native_name, name, description, input_schema, schema_hash, risk_tier, status)
VALUES ('00000000-0000-0000-0000-00000000dd02', '00000000-0000-0000-0000-00000000cc01', 'addNote', 'pagerduty_incident_note_add',
        'Add note', '{"type":"object"}', repeat('c', 64), 'medium', 'pending_review');
SELECT pg_temp.expect_error($$UPDATE itsm.mcp_tools SET risk_tier = 'read' WHERE id = '00000000-0000-0000-0000-00000000dd02'$$,
  '23514', 'T8.9 lowering risk tier without a new approval rejected');

-- ---------------------------------------------------------------------
-- T9 Memory: provenance, review, uniqueness, retrieval
-- ---------------------------------------------------------------------
SELECT pg_temp.expect_error($$INSERT INTO itsm.memory_items (kind, scope, subject_key, content, confidence, source_refs)
  VALUES ('semantic', 'org', 'jira.project.network', 'Network changes live in project NET', 0.9, '[]')$$,
  '23514', 'T9.1 memory without provenance rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.memory_items (kind, scope, subject_key, content, confidence, source_refs, status)
  VALUES ('semantic', 'org', 'jira.project.network', 'Network changes live in project NET', 0.9, '["admin-entered"]', 'active')$$,
  '23514', 'T9.2 semantic org fact cannot be active without review');
SELECT pg_temp.expect_error($$INSERT INTO itsm.memory_items (kind, scope, subject_key, content, confidence, source_refs)
  VALUES ('preference', 'user', 'output.grouping', 'Group by assignment group', 0.9, '["user-confirmed"]')$$,
  '23514', 'T9.3 user-scoped memory needs an owner');
SELECT pg_temp.expect_error($$INSERT INTO itsm.memory_items (kind, scope, subject_key, content, confidence, source_refs)
  VALUES ('episodic', 'org', 'job.summary', 'x', 1.5, '["evt:1"]')$$,
  '23514', 'T9.4 confidence must be within [0,1]');

INSERT INTO memory_items (id, kind, scope, subject_key, content, confidence, source_refs, status, reviewed_by, reviewed_at, pinned, embedding)
VALUES ('00000000-0000-0000-0000-00000000ee01', 'semantic', 'org', 'jira.project.network',
        'Network changes are tracked in Jira project NET and ServiceNow group NOC-L2.', 0.95,
        '["servicenow:sys_user_group/NOC-L2", "jira:project/NET"]', 'active', '00000000-0000-0000-0000-0000000000c4', now(), true,
        (SELECT array_agg(CASE WHEN i = 1 THEN 1.0 ELSE 0.0 END)::vector(1024) FROM generate_series(1, 1024) i));
SELECT pg_temp.expect_error($$INSERT INTO itsm.memory_items (kind, scope, subject_key, content, confidence, source_refs, status, reviewed_by)
  VALUES ('semantic', 'org', 'jira.project.network', 'Network changes live in project NETOPS', 0.8, '["ticket:NET-1"]', 'active',
          '00000000-0000-0000-0000-0000000000c4')$$,
  '23505', 'T9.5 two active versions of the same subject rejected (conflict must go to review)');
INSERT INTO memory_items (kind, scope, subject_key, content, confidence, source_refs, supersedes_id)
VALUES ('semantic', 'org', 'jira.project.network', 'Network changes live in project NETOPS', 0.6, '["ticket:NET-1"]',
        '00000000-0000-0000-0000-00000000ee01');
SELECT pg_temp.ok((SELECT status FROM memory_items WHERE content LIKE '%NETOPS%') = 'pending_review', 'T9.6 conflicting proposal parks in pending_review');

SELECT pg_temp.ok((SELECT id FROM memory_items WHERE status = 'active'
                   ORDER BY embedding <=> (SELECT array_agg(CASE WHEN i = 1 THEN 0.9 ELSE 0.01 END)::vector(1024) FROM generate_series(1, 1024) i)
                   LIMIT 1) = '00000000-0000-0000-0000-00000000ee01', 'T9.7 vector retrieval (cosine) returns the active item');
SELECT pg_temp.ok((SELECT count(*) FROM memory_items WHERE status = 'active' AND tsv @@ plainto_tsquery('english', 'network group')) = 1,
  'T9.8 full-text retrieval works');

-- ---------------------------------------------------------------------
-- T10 Audit hash chain
-- ---------------------------------------------------------------------
INSERT INTO audit_log (actor_type, actor_id, action, target, detail) VALUES
  ('user',  'u2',     'approval.decided',  'action:ac01', '{"decision":"approve"}'),
  ('agent', 'job:j1', 'action.executed',   'jira:NET-3',  '{"transition":"Close"}'),
  ('user',  'u4',     'mcp.server.canary', 'mcp:pagerduty', '{}');
SELECT pg_temp.ok(audit_verify() IS NULL, 'T10.1 audit chain verifies');
SELECT pg_temp.expect_error($$UPDATE itsm.audit_log SET detail = '{"decision":"reject"}' WHERE id = 1$$, '42501', 'T10.2 audit rows cannot be edited');
-- Simulate a privileged attacker who disables the trigger and edits a row:
ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only;
UPDATE audit_log SET detail = '{"decision":"reject"}' WHERE id = 1;
ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only;
SELECT pg_temp.ok(audit_verify() = 1, 'T10.3 tampering is detected by chain verification');

-- ---------------------------------------------------------------------
-- T11 Work queue claim (SKIP LOCKED) shape
-- ---------------------------------------------------------------------
INSERT INTO job_steps (job_id, step_key, kind, status) VALUES
  ('00000000-0000-0000-0000-0000000000e1', 's1', 'fetch', 'ready'),
  ('00000000-0000-0000-0000-0000000000e1', 's2', 'map',   'pending');
WITH next AS (
  SELECT id FROM job_steps WHERE status = 'ready' ORDER BY created_at
  FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE job_steps s SET status = 'running', locked_by = 'worker-1', locked_at = now(), attempt = attempt + 1
FROM next WHERE s.id = next.id;
SELECT pg_temp.ok((SELECT status FROM job_steps WHERE step_key = 's1') = 'running', 'T11.1 worker claims a ready step');
SELECT pg_temp.expect_error($$UPDATE itsm.job_steps SET attempt = 4 WHERE step_key = 's1'$$, '23514', 'T11.2 attempts capped by max_attempts');

-- ---------------------------------------------------------------------
-- T12 Epochs (context engine)
-- ---------------------------------------------------------------------
INSERT INTO epochs (id, job_id, seq, model, tool_names)
VALUES ('00000000-0000-0000-0000-0000000e0001', '00000000-0000-0000-0000-0000000000e1', 1, 'claude-opus-5-5',
        ARRAY['analyzer_aggregate', 'jira_issues_search_to_resultset', 'ledger_update', 'request_tools']);
SELECT pg_temp.expect_error($$INSERT INTO itsm.epochs (job_id, seq, model, tool_names)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 2, 'claude-opus-5-5', ARRAY['x_y'])$$, '23505', 'T12.1 only one open epoch per job and role');
SELECT pg_temp.expect_error($$UPDATE itsm.epochs SET tool_names = tool_names || 'zendesk_ticket_get'::text
  WHERE id = '00000000-0000-0000-0000-0000000e0001'$$, '23514', 'T12.2 tool set is frozen inside an epoch');
SELECT pg_temp.expect_error($$UPDATE itsm.epochs SET turns = 41 WHERE id = '00000000-0000-0000-0000-0000000e0001'$$,
  '23514', 'T12.3 turn cap (40) enforced');
SELECT pg_temp.expect_error($$UPDATE itsm.epochs SET closed_at = now(), close_trigger = 'budget'
  WHERE id = '00000000-0000-0000-0000-0000000e0001'$$, '23514', 'T12.4 epoch closed by a reset trigger must leave a handoff');
UPDATE epochs SET closed_at = now(), close_trigger = 'tool_set_change',
       handoff = '{"goal":"network changes","facts":[{"key":"F-1","value":666}],"next_step":"s6"}'
WHERE id = '00000000-0000-0000-0000-0000000e0001';
SELECT pg_temp.expect_error($$UPDATE itsm.epochs SET handoff = '{}' WHERE id = '00000000-0000-0000-0000-0000000e0001'$$,
  '23514', 'T12.5 closed epochs are immutable');
INSERT INTO epochs (id, job_id, seq, model, tool_names)
VALUES ('00000000-0000-0000-0000-0000000e0002', '00000000-0000-0000-0000-0000000000e1', 2, 'claude-opus-5-5',
        ARRAY['analyzer_aggregate', 'jira_issues_search_to_resultset', 'ledger_update', 'request_tools', 'zendesk_ticket_get']);
SELECT pg_temp.ok(true, 'T12.6 new epoch opens with the expanded tool set');

-- ---------------------------------------------------------------------
-- T13 Payload snapshots and content-addressed blobs
-- ---------------------------------------------------------------------
SELECT pg_temp.expect_error($$INSERT INTO itsm.content_blobs (content_hash, byte_size, storage, inline_content)
  VALUES (repeat('a', 64), 11, 'inline', '<ledger/>')$$, '23514', 'T13.1 inline blob must hash to its key');
INSERT INTO content_blobs (content_hash, byte_size, storage, inline_content)
SELECT encode(sha256(convert_to(t, 'UTF8')), 'hex'), octet_length(t), 'inline', t
FROM (VALUES ('<system_core version="git:4f1a9c2">rules</system_core>'), ('<task_ledger>{"steps":[]}</task_ledger>')) v(t);
INSERT INTO content_blobs (content_hash, byte_size, storage, blob_uri)
VALUES (repeat('c', 64), 120000, 'blob', 'blob://payloads/cc/cccc');
SELECT pg_temp.expect_error($$INSERT INTO itsm.content_blobs (content_hash, byte_size, storage, inline_content)
  VALUES (repeat('d', 64), 20000, 'inline', 'x')$$, '23514', 'T13.2 blocks over 8 KB must go to the blob store');
SELECT pg_temp.expect_error($$INSERT INTO itsm.context_snapshots (job_id, epoch_id, agent_role, turn, model_params, manifest, token_estimate, request_hash)
  VALUES ('00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000e0002', 'orchestrator', 1, '{}',
          '[{"pos":"system[0]","content_hash":"9999999999999999999999999999999999999999999999999999999999999999"}]', 5000, repeat('e', 64))$$,
  '23503', 'T13.3 snapshot cannot reference a missing content blob');
SELECT pg_temp.expect_error(format($$INSERT INTO itsm.context_snapshots (job_id, epoch_id, agent_role, turn, model_params, manifest, token_estimate, request_hash)
  VALUES ('00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000e0002', 'orchestrator', 1, '{}',
          '[{"pos":"system[0]","content_hash":"%s"}]', 130000, repeat('e', 64))$$,
  (SELECT content_hash FROM itsm.content_blobs WHERE inline_content LIKE '<system_core%')),
  '23514', 'T13.4 orchestrator payload over 120k tokens rejected');
INSERT INTO context_snapshots (id, job_id, epoch_id, agent_role, turn, model_params, manifest, token_estimate, request_hash)
SELECT '00000000-0000-0000-0000-00000000aa51', '00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000e0002',
       'orchestrator', 1, '{"model":"claude-opus-5-5","effort":"high"}',
       jsonb_agg(jsonb_build_object('pos', 'block', 'content_hash', content_hash) ORDER BY content_hash), 41000, repeat('e', 64)
FROM content_blobs;
SELECT pg_temp.ok((SELECT count(*) FROM context_snapshots s, jsonb_array_elements(s.manifest) m
                   JOIN content_blobs b ON b.content_hash = m->>'content_hash') = 3,
  'T13.5 snapshot manifest fully resolvable from content blobs (replayable)');
SELECT pg_temp.expect_error($$UPDATE itsm.context_snapshots SET token_actual = 1 WHERE id = '00000000-0000-0000-0000-00000000aa51'$$,
  '42501', 'T13.6 snapshots are append-only');

-- ---------------------------------------------------------------------
-- T14 Short-term memory: session state, referents, scratchpad
-- ---------------------------------------------------------------------
INSERT INTO session_state (session_id, summary, user_tz, active_goal)
VALUES ('00000000-0000-0000-0000-0000000000d1', 'User asked for network changes in the last 24h.', 'Asia/Kolkata', 'CAB compliance');
SELECT pg_temp.expect_error($$UPDATE itsm.session_state SET summary = repeat('x', 4001)
  WHERE session_id = '00000000-0000-0000-0000-0000000000d1'$$, '23514', 'T14.1 session summary bounded');
INSERT INTO session_referents (session_id, referent_key, label, kind, target, job_id, ordinal_map, turn)
VALUES ('00000000-0000-0000-0000-0000000000d1', 'ref-1', 'network changes in window', 'result_set', 'rs_01TEST',
        '00000000-0000-0000-0000-0000000000e1', '{"1":"NET-3","2":"NET-6"}', 1);
SELECT pg_temp.expect_error($$INSERT INTO itsm.session_referents (session_id, referent_key, label, kind, target, job_id, turn)
  VALUES ('00000000-0000-0000-0000-0000000000d1', 'those', 'x', 'list', 'L-1', '00000000-0000-0000-0000-0000000000e1', 2)$$,
  '23514', 'T14.2 referent key format enforced');

SELECT pg_temp.expect_error($$INSERT INTO itsm.scratchpad_entries (job_id, entry_type, text)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'finding', '666 network changes')$$, '23514', 'T14.3 finding without refs rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.scratchpad_entries (job_id, entry_type, text, refs)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'finding', '700 network changes', ARRAY['fact:F-9'])$$,
  '23514', 'T14.4 finding citing a fact that does not exist rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.scratchpad_entries (job_id, entry_type, text, refs)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'finding', 'NET-99999 is a firewall change', ARRAY['rec:jira:NET-99999'])$$,
  '23514', 'T14.5 finding citing a record outside this job rejected');
SELECT pg_temp.expect_error($$INSERT INTO itsm.scratchpad_entries (job_id, entry_type, text, refs)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'decision', 'x', ARRAY['https://evil.example'])$$,
  '23514', 'T14.6 unknown ref format rejected');
INSERT INTO scratchpad_entries (job_id, entry_type, text, refs) VALUES
  ('00000000-0000-0000-0000-0000000000e1', 'finding', '666 network changes in window', ARRAY['fact:F-1', 'rs:rs_01TEST']),
  ('00000000-0000-0000-0000-0000000000e1', 'finding', 'NET-3 is a firewall change', ARRAY['rec:jira:NET-3']),
  ('00000000-0000-0000-0000-0000000000e1', 'hypothesis', 'emergency changes are logged after the fact', '{}'),
  ('00000000-0000-0000-0000-0000000000e1', 'decision', 'CAB status = approval field', '{}');
SELECT pg_temp.expect_error($$UPDATE itsm.scratchpad_entries SET text = 'edited' WHERE entry_type = 'hypothesis'$$,
  '42501', 'T14.7 scratchpad entries cannot be edited (append-only)');
INSERT INTO scratchpad_entries (job_id, entry_type, text, refs, supersedes_id)
SELECT job_id, 'hypothesis', 'rejected: emergency changes are logged on time (checked)', '{}', id
FROM scratchpad_entries WHERE entry_type = 'hypothesis';
SELECT pg_temp.ok((SELECT count(*) FROM v_scratchpad_active WHERE job_id = '00000000-0000-0000-0000-0000000000e1') = 4,
  'T14.8 superseding replaces the entry in the active view');
INSERT INTO scratchpad_entries (job_id, entry_type, text)
SELECT '00000000-0000-0000-0000-0000000000e1', 'todo', 'todo ' || g FROM generate_series(1, 36) g;
SELECT pg_temp.expect_error($$INSERT INTO itsm.scratchpad_entries (job_id, entry_type, text)
  VALUES ('00000000-0000-0000-0000-0000000000e1', 'todo', 'one too many')$$, '23514', 'T14.9 scratchpad capped at 40 active entries');
INSERT INTO scratchpad_entries (job_id, entry_type, text, merged_from)
SELECT '00000000-0000-0000-0000-0000000000e1', 'todo', 'merged todos 1-10',
       ARRAY(SELECT id FROM scratchpad_entries WHERE text LIKE 'todo %' ORDER BY id LIMIT 10);
SELECT pg_temp.ok((SELECT count(*) FROM v_scratchpad_active WHERE job_id = '00000000-0000-0000-0000-0000000000e1') = 31,
  'T14.10 compressor merge frees capacity (40 -> 31 active)');

SELECT 'ALL TESTS PASSED' AS result;
