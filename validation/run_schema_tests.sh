#!/usr/bin/env bash
# Load schema.sql into a fresh database and run the invariant tests + concurrency test.
# Usage: PGHOST=... PGPORT=... PGUSER=... validation/run_schema_tests.sh [dbname]
set -euo pipefail
DB="${1:-itsm_schema_test}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
psql -q -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
psql -q -d "$DB" -v ON_ERROR_STOP=1 -f "$ROOT/schema/schema.sql" -f "$ROOT/schema/seed_and_tests.sql" 2>&1 \
  | grep -E "PASS|FAIL|ERROR|ALL TESTS" | sed 's/.*WARNING:  //'
"$ROOT/validation/concurrency_test.sh" "$DB"
