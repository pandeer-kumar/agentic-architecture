#!/usr/bin/env bash
# Render every mermaid block extracted by check_docs.py (validation/out/*.mmd) with mermaid-cli.
# Requires: npm i @mermaid-js/mermaid-cli@11.4.2 and a Chromium binary (set CHROME to its path).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MMDC="${MMDC:-npx mmdc}"
CFG="$(mktemp)"; echo "{\"executablePath\":\"${CHROME:-/usr/bin/chromium}\",\"args\":[\"--no-sandbox\",\"--headless\"]}" > "$CFG"
python3 "$ROOT/validation/check_docs.py" >/dev/null || true
fail=0
for f in "$ROOT"/validation/out/*.mmd; do
  if $MMDC -p "$CFG" -i "$f" -o "${f%.mmd}.svg" >/dev/null 2>&1; then echo "OK   $(basename "$f")"; else echo "FAIL $(basename "$f")"; fail=1; fi
done
exit $fail
