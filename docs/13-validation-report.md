# 13 — Validation Report

What was actually executed to check this architecture, what it found, and what remains unverified.
All commands are reproducible from the repository.

## 1. Summary

| # | Check | Tool / command | Result |
|---|---|---|---|
| 1 | Schema loads on PostgreSQL 16.15 + pgvector 0.6.0 | `psql -f schema/schema.sql` | **Pass**: 41 tables, 0 errors |
| 2 | Schema invariant tests | `schema/seed_and_tests.sql` | **70 / 70 pass** |
| 3 | Concurrency (queue claims, approval race) | `validation/concurrency_test.sh` | **2 / 2 pass** |
| 4 | Map-phase fault simulation (2,000 records, injected model faults) | `validation/simulate_map_phase.py` | **Pass** on 5 seeds at 2,000 records, plus 100 and 500 |
| 5 | Tool contracts (10 servers, 50 tools) | `validation/check_contracts.py` | **Pass** (C1–C8) |
| 6 | Doc integrity: links, anchors, 93 cross-references, test ids, tool and table names | `validation/check_docs.py` | **Pass** (K1–K6) |
| 7 | All 10 Mermaid diagrams render | `validation/render_mermaid.sh` (mermaid-cli 11.4.2, headless Chromium) | **10 / 10 render** |
| 8 | Config files parse | `yaml.safe_load` on `contracts/policies/*.yaml` | **Pass** |
| 9 | API facts against the current Claude API reference | Manual review against the Claude API skill reference | **Pass** after 2 corrections (§4) |

## 2. Schema invariant tests (70)

| Group | What is proven | Ids |
|---|---|---|
| Event log | Append-only; duplicate sequence rejected; retention purge only with an explicit opt-in | T1.1–T1.4 |
| Query spec | Inverted windows and future windows are rejected | T2.1–T2.2 |
| Result sets | `proven` completeness requires an exact count match; `max_records` ≤ 2,000; a fetch needs a spec; a repeated cursor (pagination loop) is rejected; overlapping pages dedupe to **exactly 2,000** records; content hash format | T3.1–T3.7 |
| Annotations | Rubric must be approved; batch ≤ 50 records; label must be in the rubric; a label against a stale record version is rejected; ungrounded final model labels are rejected; one final label per record; **coverage 2,000/2,000** | T4.0–T4.6 |
| Facts | Exact count over 2,000 records (666) and a compound filter (13); facts immutable; key format | T5.1–T5.4 |
| Verification gate | An answer can't be `verified` if a claim lacks evidence, a numeric claim is backed only by a record, the verifier layer is missing, or any check is contradicted; evidence cites exactly one source; ≤ 2 revisions; a downgraded answer needs the right banner | T6.1–T6.8, T6.7b |
| Approvals | Critical needs ≥ 2 approvals; intent binding required; no execution without approval; requester can't self-approve high-risk; approver role required; payload immutable; approve → executing → succeeded; no re-execution; idempotency key unique; 2 distinct approvers for critical; reject path; no approval after rejection; decisions append-only | T7.1–T7.15 |
| MCP registry | `https` only; local servers digest-pinned; no inline secrets; no enablement without an approver; lifecycle transitions logged; tool names namespaced; no enablement without an approved schema hash; **drift auto-suspends**; lowering a risk tier needs re-approval | T8.1–T8.9 |
| Memory | Provenance required; semantic facts need review; user scope needs an owner; confidence bounds; one active version per subject; conflicting proposals park in review; vector (HNSW, cosine) and full-text retrieval work | T9.1–T9.8 |
| Audit | Hash chain verifies; rows can't be edited; tampering by a privileged actor who disables the trigger is **detected** | T10.1–T10.3 |
| Queue | Ready steps are claimed with `SKIP LOCKED`; attempts are capped | T11.1–T11.2 |

Concurrency (`concurrency_test.sh`):
- **C1:** 4 workers × 10 claims over 40 ready steps gives 40 distinct claims and 0 double-claims.
- **C2:** two approvers race on a 1-approval action. Exactly one decision is recorded and the action is `approved`, because the approval trigger takes a row lock on the action.

## 3. Map-phase fault simulation

The stub "model" injects faults at these rates:
- 3 % dropped ids
- 2 % duplicated ids with conflicting labels
- 1 % invented ids
- 3 % fabricated evidence quotes
- 2 % truncated batches (`max_tokens`)
- 1 % flipped labels

Results at 2,000 records (seed 7):

| Stage | Outcome |
|---|---|
| Validators V1–V5 + retries (smaller batches) | Coverage **100 %** (1,998 final + 2 `needs_human_review`). **Zero surviving false positives**: V2 grounding removes every positive label without real evidence |
| Residual after validators | 18 false negatives (0.9 %). A negative label needs no evidence, so validators can't catch it |
| Recall net + second opinion + adjudication | Residual label errors **0**. Pipeline count = ground truth minus the disclosed `needs_human_review` records |

Across seeds 1, 7, 11, 42 and 99, residual errors after stage 2 were 0–1 per 2,000 (≤ 0.05 %).

The simulation exposed a design gap. In the first draft, second opinions covered only low- and
medium-confidence labels plus a 5 % sample of high-confidence ones, so false negatives survived. The
**recall net** (doc 05 §6.3) was added as a result.

The simulation checks the *mechanism*: coverage, grounding and retry logic. It does not check real
model accuracy, which the golden suites in doc 11 measure.

## 4. Defects found and fixed during validation

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | Schema test T8.7 | `CHECK (status <> 'enabled' OR approved_schema_hash = schema_hash)` passed when `approved_schema_hash` was NULL, because a CHECK evaluating to NULL passes in SQL. A tool could have been enabled without an approved hash | Rewritten with `IS NOT NULL` / `IS NOT DISTINCT FROM`. Two similar `answers.banner` checks were fixed the same way, and T6.7b was added |
| 2 | Contract check C3 | Three Azure tools were named `azure_*`, not `azure_monitor_*`, breaking namespacing | Renamed; doc tables regenerated from the contracts |
| 3 | Cross-doc review | The contracts gave the verifier two analyzer tools, while docs 06 and 10 said the verifier has none | Verifier has **no tools**; fact re-computation (D5) is done by code; contracts and policy updated |
| 4 | API reference review | The first draft trimmed old tool results on the client. Current models (Opus 5.5, Sonnet 5.5, Haiku 5.5) run a history-editing check on replayed thinking blocks, so editing earlier turns causes 400s (where enforced) or lost reasoning | The **append-only rule** (doc 03 §2.1): offload before appending, server-side clearing only, ledger reset into a new conversation, and CI that runs with the check in `error` mode |
| 5 | API reference review | Forced `tool_choice` (`any`/`tool`) returns 400 on Opus 5.5 and Sonnet 5.5 | Harness uses `auto` + `strict` tools + structured outputs (doc 02 §4) |
| 6 | Simulation | False negatives survived the validators | Recall net (above) |
| 7 | Review | Count-accuracy NFR overstated what code alone can guarantee for semantic labels | NFR-ACC-1 and the `golden_counts` criterion made precise |

## 5. What is NOT verified (and how to close each gap)

| Item | Status | How to close |
|---|---|---|
| Vendor API details (Jira enhanced search and approximate-count request shape, Zendesk export/count endpoints and `page[size]` maximum, ServiceNow encoded-query time zone, Graph list paging, Genie MCP tool names and limits) | Taken from search results. The vendor doc sites (`developer.atlassian.com`, `developer.zendesk.com`, `learn.microsoft.com`) and `modelcontextprotocol.io` were **not reachable from this session's network** | Connector L3 tests against sandbox tenants (doc 11 §1) before build sign-off |
| Real model accuracy (rubric F1, verifier catch rate, tool selection) | Not run (no model calls in validation) | Golden suites in doc 11 §3, starting with `rubric_network_change` (600 labelled records) |
| Latency and cost estimates (doc 05 §9, doc 12 §6) | Planning estimates | Scale-2000 load test (doc 11 §4) |
| Approval UX, Teams cards, gateway, offloader, renderer code | Specified, not implemented (docs-only deliverable) | Implementation + L2/L4 tests |
| Official Zendesk MCP server availability | "Announced, not GA" as of Oct 2026 (search) | Re-check at build time; the gateway can front it once it ships |

## 6. How to re-run

```bash
# 1. Schema + invariants + concurrency (needs PostgreSQL 16 with pgvector)
PGHOST=... PGPORT=... PGUSER=... validation/run_schema_tests.sh

# 2. Contracts and docs
python3 validation/check_contracts.py
python3 validation/check_docs.py

# 3. Diagrams (needs mermaid-cli + Chromium)
CHROME=/path/to/chromium MMDC=./node_modules/.bin/mmdc validation/render_mermaid.sh

# 4. Map-phase simulation
python3 validation/simulate_map_phase.py 2000 7
```
