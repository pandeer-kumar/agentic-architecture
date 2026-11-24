# 14 — Validation Report

Explains ARCHITECTURE §19: what was executed to check this architecture, what it found, and what
is still unverified. Every check is reproducible from the repository.

## 1. Summary

| # | Check | Command | Result |
|---|---|---|---|
| 1 | Schema loads on PostgreSQL 16.15 + pgvector 0.6.0 | `psql -f schema/schema.sql` | **Pass**: 47 tables |
| 2 | Database invariant tests | `schema/seed_and_tests.sql` | **92 / 92 pass** |
| 3 | Concurrency (queue claims, approval race) | `validation/concurrency_test.sh` | **2 / 2 pass** |
| 4 | Map-phase fault-injection simulation, 2,000 records | `validation/simulate_map_phase.py` | **Pass** on 5 seeds |
| 5 | Tool contracts: 10 servers / 50 MCP tools + 8 harness tools | `validation/check_contracts.py` | **Pass** (C1–C9) |
| 6 | Docs: links, anchors, 215 cross-references, test ids, tool names, images, back-links, no text diagrams, context budgets | `validation/check_docs.py` | **Pass** (K1–K8) |
| 7 | All 14 diagrams generated as SVG and rendered to PNG; each inspected visually | `diagrams/src/diagrams.py`, `render.js` | **14 / 14** |
| 8 | Context budgets: ARCHITECTURE §7.2 = doc 01 §3.1 = diagram, total 120k | `check_docs.py` K8 | **Pass** |
| 9 | Config files parse | `yaml.safe_load` on `contracts/policies/*.yaml` | **Pass** |
| 10 | Phase-1 implementation test suite (real SDK → fake Claude API, real connectors → fake vendor APIs, real PostgreSQL) | `pytest` | **40 / 40 pass** |
| 11 | Container: image build, `docker compose up`, health check, demo answer inside the container | `docker compose` | **Pass** (228 / 37, verified) |

## 2. Database invariant tests (92)

| Group | What is proven | Ids |
|---|---|---|
| Event log | Append-only; duplicate sequence rejected; purge only with explicit opt-in | T1.1–T1.4 |
| Query spec | Inverted and future windows rejected | T2.1–T2.2 |
| Result sets | `proven` needs an exact count match; `max_records` ≤ 2,000; fetch needs a spec; repeated cursor rejected; overlapping pages dedupe to exactly 2,000; hash format | T3.1–T3.7 |
| Annotations | Approved rubric; batch ≤ 50; label in rubric; stale record version rejected; ungrounded final labels rejected; one final label; coverage 2,000/2,000 | T4.0–T4.6 |
| Facts | Exact counts (666; 13); immutable; key format | T5.1–T5.4 |
| Verification gate | No `verified` answer without evidence for every claim, a fact behind every number, the verifier layer, and zero failed checks; ≤ 2 revisions; banners | T6.1–T6.8, T6.7b |
| Approvals | Tier floors; intent binding; no execution without approval; separation of duties; approver role; immutable payload; no re-execution; idempotency; two approvers for critical; reject path; append-only decisions | T7.1–T7.15 |
| MCP registry | https only; digest pinning; no inline secrets; approval before enablement; lifecycle logged; namespacing; approved schema hash; **drift auto-suspends**; tier lowering needs approval | T8.1–T8.9 |
| Long-term memory | Provenance; review for org facts; owner for user scope; confidence bounds; one active version; conflicts park in review; vector + full-text retrieval | T9.1–T9.8 |
| Audit | Hash chain verifies; edits blocked; privileged tampering detected | T10.1–T10.3 |
| Queue | SKIP LOCKED claim; attempt cap | T11.1–T11.2 |
| **Epochs** | One open epoch per job; **tool set frozen**; 40-turn cap; reset requires a handoff; closed epochs immutable; new epoch with expanded tools | T12.1–T12.6 |
| **Payload snapshots** | Inline blobs must hash to their key; > 8 KB goes to blob store; manifest can't reference missing blobs; orchestrator payload ≤ 120k; manifest fully resolvable (replayable); append-only | T13.1–T13.6 |
| **Short-term memory** | Session summary bounded; referent key format; findings need refs; refs must exist **in the same job** (fact, record); unknown ref formats rejected; append-only; supersede; **40-entry cap**; compressor merge frees capacity | T14.1–T14.10 |

Concurrency:
- **C1:** 4 workers × 10 claims over 40 ready steps → 40 distinct claims, 0 double-claims.
- **C2:** two approvers racing on a 1-approval action → exactly one decision recorded, action `approved`.

## 3. Map-phase simulation (2,000 records)

Injected faults: 3 % dropped ids, 2 % duplicates with conflicting labels, 1 % invented ids, 3 %
fabricated evidence, 2 % truncated batches, 1 % flipped labels.

| Seed | Final / needs review | Label errors after validators | Second opinions (disagreements) | Errors after recall net + adjudication |
|---|---|---|---|---|
| 1 | 1,993 / 7 | 4 | 102 (6) | 0 |
| 7 | 1,998 / 2 | 18 | 117 (20) | 0 |
| 11 | 1,999 / 1 | 8 | 87 (8) | 0 |
| 42 | 1,999 / 1 | 8 | 104 (8) | 1 |
| 99 | 2,000 / 0 | 8 | 87 (9) | 0 |

Coverage was 100 % in every run: every record ended up either labelled or flagged. Validators
eliminated every false positive; the recall net removed the false negatives that validators can't see.
This checks the *mechanism*, not real model accuracy (doc 10 §3 measures that).

## 4. Defects found by validation and fixed

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | Schema test T8.7 | A SQL `CHECK` whose expression is NULL passes, so a tool could be enabled without an approved hash | `IS NOT NULL` / `IS NOT DISTINCT FROM`; two banner checks fixed the same way (T6.7b) |
| 2 | Contract check C3 | Three Azure tools were missing the `azure_monitor` namespace prefix | Renamed; tables regenerated from contracts |
| 3 | Cross-doc review | Verifier had tools in contracts but "no tools" in docs | Verifier has no tools; code re-runs facts |
| 4 | API behaviour review | Client-side trimming of a live conversation breaks thinking-block binding | Append-only epochs with structured handoff (doc 01 §4) |
| 5 | API behaviour review | Moving a cache breakpoint each turn would alter earlier messages | Fixed breakpoints + top-level automatic caching for the tail (doc 02 §6) |
| 6 | API behaviour review | Forced `tool_choice` is rejected by current Opus/Sonnet models | `auto` + strict schemas + structured outputs (ARCHITECTURE §6.4) |
| 7 | Simulation | False negatives survive validators | Recall net + second opinion (doc 04 §6.3) |
| 8 | Doc check K5 | An onboarding example named a non-existent tool | Example changed |
| 9 | Diagram review | Arrows crossing boxes/labels in 6 diagrams | Re-routed through gutters; re-inspected |

## 5. Not verified (and how to close each gap)

| Item | Status | How to close |
|---|---|---|
| Vendor API details (Jira search/approximate-count shapes, Zendesk export/count limits, ServiceNow encoded-query time zone, Graph paging, Genie Conversation API limits) | From search results; vendor doc sites were not reachable from the validation environment | Connector contract tests against sandbox tenants (doc 10 §1) |
| Real model accuracy (rubric F1, verifier catch rate, router recall, referent resolution) | Not run (no model calls during validation) | Golden suites (doc 10 §3) |
| Latency and cost (doc 04 §9, doc 11 §6) | Estimates | Scale-2000 load test (doc 10 §4) |
| Context engine, gateway, router, renderer code | Specified; the DB rules they rely on are tested | Build + L2/L2b/L4 tests |
| Open decisions in doc 12 §J | Need your input | — |

## 6. How to re-run

```bash
python3 diagrams/src/diagrams.py && node diagrams/src/render.js
PGHOST=... PGPORT=... PGUSER=... validation/run_schema_tests.sh
python3 validation/check_contracts.py
python3 validation/check_docs.py
for s in 1 7 11 42 99; do python3 validation/simulate_map_phase.py 2000 $s; done
```
