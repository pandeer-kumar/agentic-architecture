# 15 — Implementation (Phase 1, working)

Explains how ARCHITECTURE.md is implemented in `src/itsm_agent/`, how to run it, what is built,
what is not yet built, and the checklist for the first run against your real systems.

## 1. Run it

```bash
# 1. PostgreSQL 16 + pgvector and the app (fixture mode: built-in fake Jira/ServiceNow/Zendesk)
cp .env.example .env            # add ANTHROPIC_API_KEY
docker compose up -d --build    # migrates the schema and seeds tools, rubric, playbook on start
curl localhost:8080/healthz

# 2. Ask a question (HTTP)
SID=$(curl -s -X POST localhost:8080/v1/sessions -H 'X-User-Subject: me' -H 'X-User-Tz: Asia/Kolkata' | jq -r .session_id)
curl -s -X POST localhost:8080/v1/sessions/$SID/messages -H 'X-User-Subject: me' -H 'Content-Type: application/json' \
     -d '{"text":"Which network changes were done in the last 24 hours without CAB approval?"}' | jq -r .answer_md

# 3. Or the CLI
docker compose exec app itsm-agent demo                 # real Claude, fixture data with known answers
docker compose exec app itsm-agent demo --fake-model    # no API key: built-in fake Claude API
docker compose exec app itsm-agent approvals            # pending writes
docker compose exec app itsm-agent approve <action_id>  # as an approver
docker compose exec app itsm-agent execute              # run approved writes (exactly once)
```

Without Docker: `pip install -e ".[dev]"`, point `ITSM_DATABASE_URL` at PostgreSQL 16 with pgvector,
then `itsm-agent init-db` and `itsm-agent serve`.

Tests: `pytest` (needs a PostgreSQL superuser DSN in `ITSM_TEST_ADMIN_URL`; each run creates and drops
its own database). They use the fake vendor APIs and the fake Claude API, so no credentials are needed.

## 2. Code map

| Architecture (ARCHITECTURE.md) | Module | Notes |
|---|---|---|
| ① Agent runtime — orchestrator loop, revisions, crash resume | `harness/orchestrator.py` | `ask()`, `resume()` |
| Triple check — D-checks, verifier, persistence | `harness/verify.py` | DB trigger is the final gate |
| ② Context engine — payload builder, layers, budgets, fencing | `context/builder.py` | prompt in `prompts/orchestrator.md` |
| Epochs and handoff | `context/epochs.py` | deterministic handoff, validated |
| Snapshots and replay | `context/snapshots.py` | content-addressed blobs, `replay_request()` |
| Time windows | `context/timewindow.py` | resolved once, half-open UTC |
| ③ Short-term memory — session state, referents, scratchpad | `memory/stm.py` | rules enforced by DB triggers |
| Long-term memory — search, promotion gate, review | `memory/ltm.py` | |
| ④ Record store, completeness proof, handle summaries | `dataplane/recordstore.py` | |
| Analyzer — filter, union, join, aggregate → facts, lists, CSV | `dataplane/analyzer.py` | the only source of numbers |
| Map phase — pre-filter, batches, V1–V5, recall net, adjudication | `dataplane/map_phase.py` | |
| Renderer — placeholders, footer | `dataplane/render.py` | |
| ⑤ Tool registry, router, gateway, implementations, executor | `tools/*.py` | contracts in `contracts/` |
| Connectors — Jira, ServiceNow, Zendesk (paging, windows, writes) | `connectors/*.py` | `factory.py` picks live or fixture |
| MCP servers (stdio) | `mcp_servers/server.py` | `python -m itsm_agent.mcp_servers servicenow` |
| ⑦ LLM gateway | `llm.py` | streaming, structured outputs, refusal fallback |
| API / CLI | `api.py`, `cli.py` | |
| Fixtures | `testing/fake_vendors.py`, `testing/fake_claude.py` | known ground truth |

## 3. What is built and tested

| Capability | Evidence (tests) |
|---|---|
| Exact answer to the headline question at 513 and at 1,964 changes, verified | `test_e2e`, `test_scale` |
| Wrong number in a draft caught (D3), revised, verified | `test_e2e::test_wrong_number_is_caught_and_revised` |
| Map phase: injected drops, duplicates, invented ids, fake evidence, truncation, flips → coverage 100 %, no ungrounded positives | `test_map_phase` |
| Paging: overlap dedupe, lossy source flagged not proven, 429 retry, loop guard, over-limit | `test_connectors`, `test_dataplane` |
| Same state → byte-identical payload; snapshot replay; layer budgets; untrusted text never in system layers | `test_context` |
| Epoch resets mid-job with handoff; answer still exact | `test_e2e::test_epoch_reset_keeps_working` |
| Crash mid-job → `resume()` → exact answer, completed fetches not redone | `test_resume` |
| Writes → pending approval → approve → executed exactly once; idempotent proposals | `test_e2e`, `test_gateway` |
| Scratchpad and memory rules (refs must exist in the job; ticket-only org facts wait for review) | `test_memory` |
| Real MCP over stdio, gateway policy applies to MCP calls | `test_mcp` |
| HTTP API flow incl. ownership checks | `test_api` |
| 92 database invariant tests + 2 concurrency tests | `validation/run_schema_tests.sh` |
| Container build and `docker compose up`, demo answer inside the container | run manually during validation (doc 14) |

## 4. Not built yet (honest list)

| Item | State | Next step |
|---|---|---|
| Connectors for SharePoint, Confluence, Postgres, Genie, Azure Monitor | Contracts only; tools disabled so the router never offers them | Same template as `connectors/servicenow.py` + entries in `tools/impl.py` and `registry.IMPLEMENTED` |
| Teams posting | Logged only (`mode: logged`) | Graph `ChannelMessage.Send` call in `tools/executor.py` |
| MCP auto-onboarding (discover → probe → score → approve) | Schema, rules and drift trigger exist; built-in servers registered by `registry.sync_to_db` | Probe runner + admin UI |
| Background workers | A question runs inside the HTTP request; crash recovery is `Orchestrator.resume(job_id)` | Move `ask()` behind a worker that claims jobs; return job id immediately |
| Embedding retrieval for long-term memory | Full-text + trigram search; `embedding` column unused | Pick an embedding provider, fill the column, add cosine to the ranking |
| LLM compressor | Handoff and session summary are deterministic (the doc's fallback path) | Optional: model-written progress notes, validated by the same checks |
| Auth | Dev header `X-User-Subject` | OIDC middleware (Entra ID) replacing `api.current_user` |
| Per-user model fairness, cost budgets | SDK retries + per-source semaphores | Token buckets in `llm.py`, enforce `jobs.token_budget` |
| Delegated per-user tokens | One credential per connector from env | OAuth on-behalf-of per user in `connectors/factory.py` |

## 5. First run against your systems (checklist)

1. **Prompts with the real model, fixture data:** set `ANTHROPIC_API_KEY`, run `itsm-agent demo`. The fixture
   answer is known (228 / 37 at the pinned clock `ITSM_FIXED_NOW=2026-11-23T09:00:00+00:00`), so you see at once
   whether the real model follows the playbook.
2. **Map your fields:** `JIRA_CAB_FIELD`, `JIRA_LINKED_CHANGE_FIELD`, `JIRA_TIMEZONE`, `SERVICENOW_TIMEZONE`;
   adjust the rubric pre-filter (`seed.py` → `RUBRIC.prefilter`) and the playbook's JQL / encoded query to your projects.
3. **Live mode:** `ITSM_CONNECTOR_MODE=live` with read-only API users first.
4. **Acceptance check:** pick one past 24-hour window; compare the agent's counts with a manual JQL /
   ServiceNow list. Counts must match exactly; investigate any difference before trusting other answers.
5. **Rubric quality:** label 150–200 real records, measure F1 (doc 10 §3) before relying on semantic labels.
6. **Writes:** enable one write tool at a time; keep approvals on.

If the API rejects a tool schema as too complex, set `ITSM_STRICT_TOOLS=false` (the gateway still validates
every argument). If your platform lacks server-side refusal fallback, set `ITSM_SERVER_SIDE_FALLBACK=false`.
