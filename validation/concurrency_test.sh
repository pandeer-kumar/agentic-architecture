#!/usr/bin/env bash
# Concurrency checks against a database that already ran seed_and_tests.sql:
#  1. Two workers claiming with FOR UPDATE SKIP LOCKED never get the same step.
#  2. Two approvers racing on a 1-approval action: exactly one approval row is accepted
#     (the second sees status 'approved' and is rejected by the trigger).
set -euo pipefail
DB="$1"
P="psql -X -q -tA -d $DB -v ON_ERROR_STOP=1"
$P -c "SET search_path=itsm; INSERT INTO job_steps (job_id, step_key, kind, status)
       SELECT '00000000-0000-0000-0000-0000000000e1', 'c' || g, 'map', 'ready' FROM generate_series(1,40) g;"
CLAIM="SET search_path=itsm;
BEGIN;
WITH next AS (SELECT id FROM job_steps WHERE status='ready' ORDER BY created_at, step_key FOR UPDATE SKIP LOCKED LIMIT 1)
UPDATE job_steps s SET status='running', locked_by='\$W', locked_at=now(), attempt=attempt+1 FROM next WHERE s.id=next.id;
SELECT pg_sleep(0.02);
COMMIT;"
for w in A B C D; do
  ( for i in $(seq 1 10); do $P -c "${CLAIM//\$W/$w}" >/dev/null; done ) &
done
wait
RES=$($P -c "SET search_path=itsm; SELECT count(*), count(DISTINCT id), count(*) FILTER (WHERE attempt > 1) FROM job_steps WHERE step_key LIKE 'c%' AND status='running';")
echo "skip-locked claims (claimed, distinct, double-claimed): $RES"
[ "$RES" = "40|40|0" ] && echo "PASS  C1 40 steps claimed by 4 concurrent workers, no double claims" || { echo "FAIL  C1"; exit 1; }

$P -c "SET search_path=itsm; INSERT INTO actions (id, job_id, user_turn_event_id, tool_name, arguments, risk_tier, required_approvals, preview, idempotency_key)
       SELECT '00000000-0000-0000-0000-00000000ac09', '00000000-0000-0000-0000-0000000000e1', id, 'jira_issue_transition', '{}', 'high', 1, '{}', 'race:1'
       FROM session_events WHERE seq = 1;"
APPROVE="SET search_path=itsm; BEGIN; INSERT INTO approvals (action_id, approver_id, decision) VALUES ('00000000-0000-0000-0000-00000000ac09', '\$U', 'approve'); SELECT pg_sleep(0.2); COMMIT;"
( $P -c "${APPROVE//\$U/00000000-0000-0000-0000-0000000000c2}" >/dev/null 2>&1 || true ) &
( $P -c "${APPROVE//\$U/00000000-0000-0000-0000-0000000000c3}" >/dev/null 2>&1 || true ) &
wait
RES=$($P -c "SET search_path=itsm; SELECT (SELECT count(*) FROM approvals WHERE action_id='00000000-0000-0000-0000-00000000ac09'), (SELECT status FROM actions WHERE id='00000000-0000-0000-0000-00000000ac09');")
echo "approval race (rows, status): $RES"
[ "$RES" = "1|approved" ] && echo "PASS  C2 concurrent approvers serialised; exactly one decision recorded" || { echo "FAIL  C2"; exit 1; }
echo "CONCURRENCY OK"
