# 10 — Evaluation & Testing

Explains ARCHITECTURE §16 (quality assurance).

"Functioning and tested" means every layer has an automated test that can fail. Model-dependent
behaviour is tested statistically (multiple trials), deterministic behaviour exactly.

## 1. Test pyramid

| Level | What | Tooling | Runs |
|---|---|---|---|
| L0 Schema invariants | DB constraints & triggers (approvals, drift, verification gate, memory review, audit chain) | `schema/seed_and_tests.sql` (92 assertions) + concurrency tests (doc 14) | Every commit |
| L1 Contract tests | Tool JSON Schemas valid & strict-compatible; names/namespaces; doc ↔ contract sync; example inputs validate | `validation/check_contracts.py` | Every commit |
| L2 Unit tests | Query-spec window resolution (DST, time zones, half-open ranges), batch validators V1–V5, D-checks, renderer, offloader, sanitiser | pytest | Every commit |
| L3 Connector tests | Pagination, loop guards, rate-limit backoff, cursor expiry, dedupe against **recorded fixtures** (VCR-style) and a sandbox tenant per source | pytest + recorded HTTP | Every commit (fixtures), nightly (sandbox) |
| L2b Context-engine tests | Payload determinism (same state → same `request_hash`); layer budgets; fencing escapes; epoch triggers; handoff validators incl. fallback; snapshot replay byte-identity; append-only check run in `error` mode | pytest + recorded snapshots | Every commit |
| L4 Pipeline tests | End-to-end with fixtures and a **stub model** returning scripted outputs (incl. malformed ones) — proves the harness catches missing ids, ungrounded quotes, wrong numbers | pytest | Every commit |
| L5 Model evals | Golden suites against real models (§3) | eval runner → `eval_runs` | Nightly + on every prompt/model/harness change |
| L6 Load & chaos | 2,000-record jobs, worker kills, API 429 storms, cursor expiry | k6/locust + fault injection | Weekly + before release |
| L7 Red team | Prompt injection, malicious MCP server, approval bypass attempts | Scripted + manual | Before release, quarterly |

Example L4 cases with a stub model (deterministic, no API cost):

| Stub behaviour | Expected harness behaviour |
|---|---|
| Map output omits 2 of 25 rids | V1 fails → only those 2 re-run → coverage 100 % |
| Map output duplicates a rid with two labels | V1 fails → re-run → one final label (DB unique index backs this) |
| Evidence quote not in record text | V2 → `ungrounded` → individual re-run → `needs_human_review` after cap |
| Draft says 410, fact says 409 | D3 fails → revision with specific feedback |
| Draft mentions `NET-9999` not in any result set | D4 fails |
| Draft claims "no CAB approval" for a record whose approval field wasn't fetched | Verifier A3 → `not_enough_info` → claim removed or field fetched |
| Model calls a write tool directly | Gateway converts to `pending_approval`; no upstream call recorded |
| Model output hits `max_tokens` mid-batch | V4 → batch split and re-run |

## 2. Fixture strategy

- **Recorded fixtures** of real (anonymised) API responses for each source, including pagination
  edge cases: duplicates across pages, last page with `has_more=false`, empty page with `has_more=true`
  (loop guard), 429 with `Retry-After`, expired cursor.
- **Synthetic ITSM datasets** with ground truth: 100, 500, 2,000 records with a known number of
  network changes, known CAB status, known duplicates, cross-system links, injected adversarial text,
  and boundary timestamps (exactly at window start/end, DST transitions).
- **Frozen clock**: every eval case fixes "now" and the user's time zone so "last 24 hours" is deterministic.

## 3. Golden suites (model evals)

| Suite | Cases (initial) | Graded by | Pass criterion |
|---|---|---|---|
| `golden_counts` | 40 questions over synthetic datasets (100–2,000 records) | Exact match vs ground truth | Structured-criteria questions: 100 % exact. Semantic questions: count error ≤ 0.5 % of records and every `needs_human_review` record disclosed. 100 % coverage in both |
| `rubric_network_change` | 600 labelled records (balanced, incl. hard negatives like "network printer") | Precision/recall vs labels | F1 ≥ 0.95; per-class recall ≥ 0.93 |
| `cross_system` | 25 reconciliation questions (Jira ↔ ServiceNow ↔ Azure Activity Log) | Exact sets | matched/only-A/only-B sets exact |
| `grounding` | 50 drafts with seeded errors (wrong number, invented id, unsupported absence claim, causal leap) + 50 correct drafts | Verifier verdicts vs labels | Verifier catches ≥ 98 % of seeded errors; false-reject ≤ 5 % |
| `injection` | 40 records with embedded instructions (direct, encoded, multilingual, in attachments' text) | No unintended tool calls / proposals; flag raised | 0 unintended proposals |
| `tool_selection` | 60 requests across all servers | Router includes the needed tools in L0 (or the model obtains them via `request_tools`), and the model calls the right one | Router recall ≥ 98 %; tool choice ≥ 95 %; regression gate for MCP onboarding |
| `memory_followups` | 30 multi-turn sessions ("those", "the second one", corrections) | Referents resolved to the right handle/record; no refetch when unnecessary | ≥ 97 % correct resolution |
| `scratchpad_continuity` | 20 jobs forced through 2–3 epoch resets | Decisions and findings carried across epochs; no contradictory re-decisions | 100 % of recorded decisions honoured |
| `abstention` | 20 unanswerable questions (missing permission, missing field, out-of-scope) | Says it can't verify | ≥ 95 % correct abstention, 0 fabricated answers |
| `end_to_end` | 30 realistic requests incl. writes (approval stubbed) | LLM judge rubric + exact checks | judge ≥ 0.85 mean; all exact checks pass |

**LLM-as-judge** (for `end_to_end` and online R4): single call, structured output, scores 0–1 and
pass/fail on: factual accuracy, citation accuracy, completeness, source quality, tool efficiency
(rubric from the multi-agent research post). The judge itself is validated against 50 human-graded
answers (agreement ≥ 90 %) before it can gate releases.

**Trials**: model-graded suites run 3 trials per case; a case passes if all exact checks pass in all
trials (accuracy must be reliable, not occasional). Report mean and worst-trial metrics.

## 4. Load & chaos tests (must pass before go-live)

| Test | Setup | Pass criterion |
|---|---|---|
| Scale-2000 | 2,000-record job, all records needing judgement (worst case), recorded-fixture sources, real models | p95 ≤ 10 min; orchestrator context ≤ 120k tokens; exact counts; coverage 100 % |
| Concurrency | 10 simultaneous 500-record jobs | No cross-job leakage (handles scoped to job), rate limits respected, no 429 storm |
| Kill-9 | Kill the worker at random points (mid-fetch, mid-map, after approval before execution) | Job resumes from checkpoint; no duplicate upstream writes (idempotency keys); final answer identical to an uninterrupted run |
| Cursor expiry | Zendesk export cursor expires mid-fetch | Restart from last checkpoint; dedupe keeps count exact |
| API degradation | 30 % 5xx + 429s from a source | Backoff; completeness `incomplete` reported honestly if retries exhausted |
| Model refusal / overload | Inject `refusal` stop reasons and 529s | Fallback model used and recorded; answer still verified |

## 5. Production feedback loop

1. Every downgraded answer, verifier contradiction, human-audit miss (R3) and thumbs-down is triaged.
2. Confirmed failures become eval cases (`eval_cases.origin = 'production_incident'`) — the harness
   "tightens every time the agent slips".
3. Rubric and prompt changes are versioned (`rubrics.version`, prompt files in git) and only ship through §6.

## 6. Release gate

A change to any of: system prompts, rubrics, playbooks, tool descriptions, model routing, harness
code, or MCP server set must pass:

| Gate | Requirement |
|---|---|
| L0–L4 | 100 % pass |
| `golden_counts`, `cross_system`, `injection` | 100 % pass (zero tolerance) |
| Other suites | No metric regresses more than 1 point vs the current production baseline (95 % CI excludes a drop) |
| Cost & latency | p95 latency and cost per job within +15 % of baseline (or explicitly accepted) |
| Ablation (quarterly) | Remove one harness component at a time (e.g. second opinion, verifier, deterministic pre-filter) and measure; drop components that no longer move accuracy — every component is an assumption about model limits |

## 7. Acceptance criteria mapping

| NFR (ARCHITECTURE) | Proven by |
|---|---|
| NFR-ACC-1 count accuracy | `golden_counts` + schema tests T5.x |
| NFR-ACC-2 coverage | Batch validator V1 + `label_coverage()` + T4.6 + Scale-2000 |
| NFR-ACC-3 grounding | `grounding` suite + verification gate trigger (T6.x) |
| NFR-ACC-4 classification F1 | `rubric_network_change` + daily R3 audit |
| NFR-SCALE-1/2 | Scale-2000 load test |
| NFR-SAFE-1 no unapproved writes | T7.x + L4 write-tool stub + red team |
| NFR-SAFE-2 no credentials in context | Red team + log scanning for secret patterns |
| NFR-REL-1 resume without duplicates | Kill-9 chaos test |
