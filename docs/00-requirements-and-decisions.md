# 00 — Requirements & Decisions

## 1. Problem statement

IT operations teams ask questions and request actions that span several systems:

- *"List every network change done in the last 24 hours and flag the ones without an approved CAB record."*
- *"Which Zendesk tickets this week mention VPN failures, and is there a linked Jira incident for each?"*
- *"Summarise the runbook in SharePoint for firewall rule changes and check whether yesterday's changes followed it."*
- *"Close the 40 duplicate tickets you found and link them to the parent."* (write — needs approval)

The system is an **agent** (model-directed, multi-step, tool-using) with a **Claude-Code-like harness**:
a permissioned tool loop, sub-agents, memory, skills/playbooks, hooks, and approvals — specialised
for ITSM rather than code.

## 2. Functional requirements

| ID | Requirement |
|---|---|
| FR-1 | Natural-language questions over Jira, Zendesk, SharePoint, ServiceNow, Confluence, Postgres, Databricks Genie, Azure Monitor |
| FR-2 | Handle result sets of 100–2,000 records per question with exact counts and full coverage |
| FR-3 | Cross-system joins (e.g. Zendesk ticket ↔ Jira incident ↔ change record) |
| FR-4 | Write actions (comment, transition, link, create, close, notify) behind an approval policy |
| FR-5 | Add new MCP servers without code changes: discover, probe, risk-score, admin approval, enable |
| FR-6 | Remember organisational knowledge (project keys, team ownership, glossary, user preferences) across sessions |
| FR-7 | Every answer carries citations to source records (system + id + URL) |
| FR-8 | Long jobs survive process restarts (resume from checkpoint, never re-run an approved write) |
| FR-9 | Full audit trail of every model call, tool call, approval and write |

## 3. Non-functional requirements (targets)

| ID | Target | How it is measured (see doc 11) |
|---|---|---|
| NFR-ACC-1 | Count accuracy **100 %** for counts over structured criteria, and 100 % arithmetic correctness for counts over final labels (counts are computed by code, never by the model). Semantic-label error is governed by NFR-ACC-4 and is never hidden: records needing review are counted and disclosed | Exact-match vs fixture ground truth |
| NFR-ACC-2 | Record coverage **100 %** — every fetched record is classified exactly once | Coverage validator, zero tolerance |
| NFR-ACC-3 | Claim grounding ≥ 99 % of claims supported by cited evidence; 0 unsupported numeric claims | Verifier + human-labelled sample |
| NFR-ACC-4 | Per-record classification F1 ≥ 0.95 on labelled set (e.g. "is this a network change?") | Labelled golden set |
| NFR-SCALE-1 | 2,000-record job end-to-end p95 ≤ 10 min (fetch dominated by source API rate limits) | Load test with recorded fixtures |
| NFR-SCALE-2 | Orchestrator context stays ≤ 120k tokens for a 2,000-record job | Token telemetry per step |
| NFR-SAFE-1 | 0 writes without a matching approved approval record | DB constraint + audit test |
| NFR-SAFE-2 | 0 credentials reachable from model-generated code or model context | Architecture (doc 10) + red-team test |
| NFR-REL-1 | Crash at any step → resume without duplicate writes | Chaos test (kill worker mid-job) |

## 4. Out of scope (v1)

- Multi-tenant SaaS isolation (schema keeps `org_id` for a future move).
- Real-time streaming ingestion of every ticket into a warehouse (we query sources live; Genie/Postgres cover analytical stores).
- Autonomous writes without approval policy.

## 5. Assumptions to confirm

| # | Assumption | Impact if wrong |
|---|---|---|
| A1 | "Azure Genie" = **Azure Databricks AI/BI Genie** (managed MCP endpoint) | Replace server 5 in doc 07 |
| A2 | "Analyser" = an **in-house analysis server** that runs deterministic SQL/pandas over the record store | Doc 05/07 change only in naming |
| A3 | Jira is **Jira Cloud** (REST v3, enhanced JQL search). Jira Data Center uses a different search API | Connector pagination logic in doc 07 §1 |
| A4 | "Network change" is identifiable via fields (project/component/category/CI class) **or** semantic text; both are supported | Classification step in doc 05 |
| A5 | ServiceNow, Confluence, Microsoft Teams and Azure Monitor are acceptable fillers for the 10-server set | Swap any one; the onboarding pipeline (doc 08) is generic |
| A6 | Users authenticate via the org IdP (Entra ID / Okta, OIDC) and connectors use per-user delegated OAuth where the source supports it | Permission model in doc 10 |
| A7 | Model provider: Anthropic Claude API, reached from the self-hosted deployment (or via Amazon Bedrock / Microsoft Foundry if data-residency requires) | Model IDs and some beta features differ by platform (doc 02 §7) |

## 6. Key design decisions (ADR summary)

| ADR | Decision | Rationale (source in doc 01) |
|---|---|---|
| ADR-01 | **Data plane / model plane split.** Bulk tool results go to a Postgres record store; the model receives a *handle* + stats + small sample | Code-execution-with-MCP & advanced-tool-use posts: intermediate results through context cost tokens and cause copy errors |
| ADR-02 | **Counts, sums, group-bys are computed by code (Analyzer), never by the model** | Models are unreliable at counting long lists; deterministic code is exact |
| ADR-03 | **Map-reduce with fixed-size batches + coverage validation** for per-record semantic work | Orchestrator–worker pattern (multi-agent research post); clean sub-contexts |
| ADR-04 | **Triple-check**: deterministic validators → independent verifier agent → reconciliation & continuous eval | Evaluator–optimizer pattern; harness-design post: separate evaluator outperforms self-evaluation |
| ADR-05 | **Append-only session event log** is the source of truth; context is a *view* built from it | Managed Agents post: separate durable storage from context management |
| ADR-06 | **Own harness on the Messages API** (Python SDK) rather than a hosted agent runtime | Self-hosted requirement; we need custom gating of the data plane, approval flow and DB-backed memory |
| ADR-07 | **Tool search + deferred loading** once total tool definitions exceed ~10k tokens / 10+ tools | Advanced-tool-use post (85 % context reduction, higher tool-selection accuracy) |
| ADR-08 | **MCP gateway** in front of every server: auth, policy, result offloading, injection scanning, audit | One choke point for heterogeneous (official, third-party, auto-added) servers |
| ADR-09 | **Memory writes require provenance and are never derived directly from untrusted ticket text** | Prevent memory pollution / persistent prompt injection |
| ADR-10 | **Relative time is resolved to absolute UTC windows at planning time** and frozen in the query spec | "Last 24 hours" must mean the same window in fetch, verify, and report |
