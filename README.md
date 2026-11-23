# ITSM Agent Platform — Agentic Architecture

A "Claude Code for ITSM": an agent that searches, analyses, and (with human approval) acts across
Jira, Zendesk, SharePoint, ServiceNow, Confluence, PostgreSQL, Azure Databricks Genie and any
MCP server added later.

**The two non-negotiable goals**

1. **Accuracy** — every number and every claim in an answer is traceable to source records and
   is checked three times (deterministic validators → independent verifier → reconciliation/eval).
2. **Bulk result processing** — questions like *"every network change done in the last 24 hours"*
   return 100–2,000 tickets. The architecture never asks the model to count, enumerate, or
   remember 2,000 records from its context window; records live in a data plane and the model
   works with handles, computed facts, and batched map-reduce.

## Decisions taken (from the requirements Q&A)

| Topic | Decision |
|---|---|
| Deliverable | Markdown architecture docs (+ a runnable SQL schema and validation scripts that prove the schema and tool contracts are sound) |
| Implementation language (target) | Python 3.12+ (Anthropic Python SDK, official MCP Python SDK, FastAPI, PostgreSQL 16 + pgvector) |
| Result volume per job | Typical 100–500, hard design ceiling 2,000 records (enforced in the schema; tested in `docs/13`) |
| Write access | Writes allowed **only** through an approval policy: low-risk auto, everything else human-approved, all audited |
| New MCP servers | Discovered automatically, sandbox-probed and risk-scored, **admin approves** before enablement |
| Tenancy | Single organisation, self-hosted, per-user RBAC (schema keeps an `org_id` seam but no multi-tenant RLS) |
| MCP servers named | SharePoint, Jira, Zendesk, PostgreSQL, Azure (Databricks) Genie, Analyzer — plus Confluence, ServiceNow, Microsoft Teams, Azure Monitor to make ten |

Assumptions that you should confirm are listed in [`docs/00-requirements-and-decisions.md`](docs/00-requirements-and-decisions.md#5-assumptions-to-confirm).

## Reading order

| # | Document | What it answers |
|---|---|---|
| 00 | [Requirements & decisions](docs/00-requirements-and-decisions.md) | Scope, NFRs, assumptions, open questions |
| 01 | [Research basis](docs/01-research-basis.md) | What Anthropic / harness-engineering writing says, and which design choice each finding drives |
| 02 | [System architecture](docs/02-system-architecture.md) | Components, request lifecycle, harness loop, agent roles, model routing |
| 03 | [Context management](docs/03-context-management.md) | Context budget, handles-not-data, compaction, context editing, caching, isolation |
| 04 | [Memory management](docs/04-memory-management.md) | Memory tiers, write/read policy, anti-pollution, decay, retrieval |
| 05 | [Bulk result processing](docs/05-bulk-result-processing.md) | The 100–2,000 record pipeline: fetch → completeness proof → map → reduce → report |
| 06 | [Accuracy & anti-hallucination](docs/06-accuracy-and-anti-hallucination.md) | The triple-check, grounding rules, injection & pollution defences |
| 07 | [MCP servers & tools](docs/07-mcp-servers-and-tools.md) | The 10 servers, every tool, annotations, risk tier, limits |
| 08 | [MCP auto-onboarding](docs/08-mcp-auto-onboarding.md) | Discover → probe → score → approve → enable → monitor drift |
| 09 | [Database schema](docs/09-database-schema.md) + [`schema/schema.sql`](schema/schema.sql) | Every table, why it exists, invariants enforced in SQL |
| 10 | [Security, permissions, HITL](docs/10-security-permissions-hitl.md) | RBAC, approvals, credential isolation, audit chain |
| 11 | [Evaluation & testing](docs/11-evaluation-and-testing.md) | Test pyramid, golden sets, metrics, release gates |
| 12 | [Operations & deployment](docs/12-operations-and-deployment.md) | Self-hosted topology, observability, cost, failure recovery |
| 13 | [Validation report](docs/13-validation-report.md) | What was actually executed to check this architecture, and the results |

## Repository layout

```
README.md
docs/                     architecture documents (00–13)
schema/schema.sql         PostgreSQL 16 + pgvector DDL (executed in validation)
schema/seed_and_tests.sql invariant tests run against the schema
contracts/tools/*.json    JSON-Schema tool contracts for the 10 MCP servers (validated)
contracts/policies/       tool risk policy + model routing config examples
validation/               scripts used to validate the above (see docs/13)
```
