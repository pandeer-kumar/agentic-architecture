# 12 — Design Considerations (architecture review checklist)

Explains ARCHITECTURE §18. Everything that has to be considered before calling this a good
architecture: each item has the decision taken, its status, and where it is designed.

Status: **Decided** = designed and, where possible, tested · **Validate** = designed, needs
measurement in build/pilot · **Open** = needs a decision from you or a stakeholder.

## A. Accuracy and meaning

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| A1 | What counts as a "network change"? | Versioned rubric (`network_change_v3`) + structured criteria from org profile (project keys, CI classes, assignment groups); semantic labelling only for the remainder | Validate (rubric owner must label the golden set) | doc 04 §5–6 |
| A2 | Which timestamp defines "done in the last 24 h" (created, resolved, implemented, closed)? | `window_field` is explicit in the QuerySpec; default per playbook; ambiguous → ask the user | Decided | doc 04 §1 |
| A3 | Time zones and DST | Relative time resolved once into a half-open UTC window from the user's IANA zone; source-native zone conversion per connector; out-of-window rows excluded and counted | Decided; DST cases in golden set | doc 04 §1, §3 |
| A4 | Source freshness (search indexes lag minutes) | Window end ≤ plan time; answer shows `as_of`; Zendesk search lag noted as a caveat for windows ending < 5 min ago | Decided | doc 04 §1 |
| A5 | Permission-limited visibility | Results reflect the user's delegated access; caveat C6 on every answer | Decided | doc 04 §3, doc 09 §1 |
| A6 | Same change recorded in two systems | Union with a dedupe key (normalised change number / linked ids); duplicates reported, not hidden | Decided | doc 06 §6.6 |
| A7 | Missing or permission-hidden fields vs real "no" | Typed nulls; absence claims require a fetched, typed value (verifier rule A3) | Decided | doc 05 §2.3 |
| A8 | Partial failures (one source down) | Completeness `incomplete` per source, named in the answer; never silently partial | Decided | doc 04 §3 |
| A9 | When the agent should refuse or ask | Abstention is a correct answer; open questions from the scratchpad surface to the user | Decided; `abstention` suite | doc 05 §2.5, doc 03 §3 |
| A10 | Label errors the validators can't see (false negatives) | Recall net + second opinion + adjudication; daily human audit | Decided; simulated | doc 04 §6.3, doc 14 |
| A11 | Numbers inside free text (e.g. "affects 3 sites") | Treated as claims; must be supported by evidence or removed | Decided | doc 05 §2.2 (D3) |

## B. Scale and performance

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| B1 | More than 2,000 records | Over-limit stops early with the count; user narrows, or analytical mode (Genie/Postgres aggregates) | Decided | doc 04 §2 |
| B2 | Source API rate limits (Genie low QPS, Zendesk export 100/min) | Per-source token buckets, queueing, async mode, early count pre-flight | Validate (load test) | doc 06 §3 |
| B3 | Model rate limits and fairness across users | LLM gateway token buckets per user and job class | Validate | ARCHITECTURE §12 |
| B4 | Latency the user perceives | Progress events over SSE per stage; first useful output (counts) before the full list | Decided | doc 11 §1 |
| B5 | Cost per job | Pre-filter, caching, effort per role, per-job token budget with pause-and-ask | Validate (cost dashboards) | doc 11 §5 |
| B6 | Many concurrent bulk jobs | Map concurrency per job (8) and global cap; queue depth autoscaling | Validate | doc 11 §1 |
| B7 | Long-running scheduled reports | Async mode with batch inference; results stored as exports | Decided | doc 04 §6.4 |

## C. Context and memory

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| C1 | Context rot in long jobs | 120k budget, epochs, structured handoff | Decided | doc 01 |
| C2 | Editing history breaks thinking blocks | Append-only epochs; never trim a live conversation | Decided | doc 01 §4 |
| C3 | Follow-up questions ("those", "the second one") | Referents in short-term memory | Decided | doc 03 §2.2 |
| C4 | Losing working knowledge at reset | Typed scratchpad re-injected every epoch | Decided | doc 03 §3 |
| C5 | Memory poisoning (from tickets or users) | Provenance, review for org facts, no memory from untrusted text alone | Decided; tested in schema | doc 03 §4.2 |
| C6 | Stale long-term memory | Decay, confidence updates, weekly re-validation, live data wins | Decided | doc 03 §4.4 |
| C7 | Right to erasure for personal data in memory/snapshots | PII excluded from semantic memory; per-user deletion job over preferences, sessions, snapshots | Open (confirm legal requirements) | doc 08 §3 |
| C8 | Reproducing "why did it answer that" months later | Content-addressed snapshots + memory version refs | Decided | doc 02 §8 |

## D. Tools and integrations

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| D1 | Vendor API changes (e.g. Jira search endpoint replacement) | Contract tests against sandboxes; field catalogue validation | Validate (needs sandbox tenants) | doc 10 §1 |
| D2 | Custom fields differ per instance | `*_fields_list` tools + org profile mapping of meaning → field id | Decided | doc 06 §6.1 |
| D3 | Attachments (screenshots, PDFs, logs) in tickets | v1: metadata only; v2: optional extraction behind size limits and scanning | Open (needed for v1?) | — |
| D4 | Very long comment threads | Concise/detailed variants; last N comments by default; full thread on request | Decided | doc 06 §4 |
| D5 | OAuth token expiry mid-job | Gateway refreshes per call; long jobs re-acquire; failure → step paused, user asked to re-auth | Decided | doc 09 §1 |
| D6 | Live query vs replicated warehouse | Live queries for freshness; Genie/Postgres for heavy analytics | Decided | ARCHITECTURE §6.3 |
| D7 | Adding many more MCP servers | Auto-onboarding with admin approval, tool router keeps L0 ≤ 8k | Decided | doc 07, doc 06 §2 |
| D8 | Tool confusion as the catalog grows | Router relevance + role filters; tool-selection eval as onboarding gate | Validate | doc 07 §7 |

## E. Safety and security

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| E1 | Prompt injection via ticket text | Fencing, scanner, no-tool workers, intent binding, approvals | Decided; red-team suite | doc 05 §5 |
| E2 | Data exfiltration through writes or notifications | Egress allow-lists, allow-listed Teams channels, approval previews | Decided | doc 09 §5 |
| E3 | Credentials | Vault + gateway injection; never in context or snapshots | Decided | doc 09 §2 |
| E4 | Agent sees more than the user | Delegated per-user tokens | Decided | doc 09 §1 |
| E5 | Separation of duties on writes | DB-enforced: requester ≠ approver for high/critical; two approvers for critical | Decided; tested | doc 09 §4 |
| E6 | Malicious or drifting MCP servers | Probe, scan, admin approval, schema-hash drift suspension | Decided; tested | doc 07 |
| E7 | Audit tampering | Hash chain + external checkpoints | Decided; tested | doc 09 §7 |
| E8 | PII sent to the model | Optional gateway tokenisation of e-mails/phones/names | Open (enable by default?) | doc 05 §5 |

## F. People and product

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| F1 | Approval fatigue | Batched proposals, per-item exclude, tiers, auto for low risk, metrics | Validate (pilot) | doc 09 §4 |
| F2 | Trust: users need to see why | Method notes (decisions from scratchpad), citations per row, completeness banner | Decided | doc 05 §2 |
| F3 | Cancel / redirect a running job | Cancel endpoint marks job cancelled; workers check between steps; partial results kept, not presented as final | Decided | doc 11 §4 |
| F4 | User corrections ("that one isn't a network change") | Human label annotation (source `human`), feeds rubric eval and audit | Decided | doc 08 §2.3 |
| F5 | Output formats (chat, CSV/XLSX, Teams card) | Renderer from DB; exports with expiry | Decided | doc 04 §8 |
| F6 | Languages other than English in tickets | Rubrics include multilingual examples; eval cases per language | Open (which languages?) | doc 10 §3 |

## G. Operations

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| G1 | Versioning prompts, rubrics, playbooks, routing | Git/DB versions; every snapshot records versions; eval gate | Decided | doc 10 §6 |
| G2 | Model deprecation / upgrades | Pinned model IDs, eval gate, ablation, rollout by % of new jobs | Decided | doc 11 §4 |
| G3 | Running jobs during deploys | Jobs finish on the harness version they started with | Decided | doc 11 §4 |
| G4 | Backup and disaster recovery | Managed Postgres PITR; blob storage geo-redundancy; RPO 15 min / RTO 4 h | Validate (confirm targets) | doc 11 §4 |
| G5 | Observability | OTel traces per job; accuracy/cost/safety/context dashboards | Decided | doc 11 §3 |
| G6 | Model provider outage | Pause and resume; optional secondary platform for the same model family | Open (second platform needed?) | ARCHITECTURE §12 |

## H. Governance

| # | Consideration | Decision | Status | Where |
|---|---|---|---|---|
| H1 | Who owns rubrics, memory review, MCP approvals | Rubric owner per domain; memory reviewers per domain; platform admin + security reviewer for MCP | Open (name owners) | doc 07 §6 |
| H2 | Data retention and legal hold | Retention table; legal hold flag suspends purge per session | Validate with legal | doc 08 §3 |
| H3 | Data residency for model calls | Endpoint/region configured; payload snapshots stay in-region | Open (region requirement?) | doc 09 §6 |
| H4 | Transparency that answers are AI-generated | Banner + method notes on every answer | Decided | doc 05 §2.5 |

## I. Delivery plan (recommended phasing)

| Phase | Scope | Exit criterion |
|---|---|---|
| 1 | Read-only Q&A over Jira + ServiceNow (+ Zendesk); bulk pipeline; triple check; context engine; short-term memory; approved writes | **Built** (doc 15). Remaining exit criteria need real data: rubric F1 on labelled records, live acceptance check |
| 2 | + Zendesk, SharePoint, Confluence, Azure Monitor, Postgres, Genie; long-term memory with review | Cross-system suite passes; memory review workflow live |
| 3 | Writes with approvals; Teams notifications | Red-team suite passes; 0 unapproved writes in pilot |
| 4 | MCP auto-onboarding; scheduled reports | First third-party server onboarded through the full lifecycle |

## J. Open decisions for you

1. Confirm assumptions A1–A7 in ARCHITECTURE §0 (custom scope, platform, load, channels, Genie/analyser meaning).
2. Attachments in v1? (D3) · PII tokenisation default? (E8) · Languages? (F6)
3. Second model platform for outages? (G6) · Data residency region? (H3) · Erasure requirements? (C7)
4. Owners for rubrics, memory review and MCP approvals (H1).
