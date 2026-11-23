# 13 — Research Basis

Explains where the design comes from. Each published finding is mapped to the concrete mechanism
it drives in ARCHITECTURE.md. Numbers are quoted from the sources; they are
the sources' own measurements, not ours.

## 1. Anthropic engineering posts

| Source | Key finding | Where it lands in this design |
|---|---|---|
| [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | Start with the simplest pattern; workflows (chaining, routing, parallelisation, orchestrator–workers, evaluator–optimizer) before free-form agents. Invest in the agent–computer interface; "poka-yoke" tools. | Hybrid: a **deterministic pipeline** for bulk jobs (doc 04) wrapped by a model-directed orchestrator. Tools designed so mistakes are hard (absolute time windows, typed enums, handle IDs). |
| [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Context is a finite "attention budget"; *context rot* — recall degrades as the window fills. Just-in-time retrieval via lightweight references; compaction; structured note-taking; sub-agents return condensed 1–2k-token summaries. | Doc 01: per-layer budgets, *handles not data*, epochs with structured handoff, sub-agent isolation; doc 03 scratchpad (structured note-taking). |
| [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) | Consolidate around workflows, namespace tools (`service_resource_verb`), return high-signal fields not UUIDs, offer `response_format` concise/detailed, paginate/truncate with sane defaults (Claude Code caps tool responses at 25k tokens), actionable error messages, evaluate tools with realistic tasks. | Doc 06 tool contracts: namespaced names, `response_format`, default limits, resolved human-readable fields, error messages with a fix; gateway 25k hard cap (doc 06 §3). |
| [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) | Orchestrator–worker with parallel sub-agents beat single-agent by 90.2 % on their eval; multi-agent uses ~15× chat tokens. Delegation needs objective, output format, tool guidance, boundaries. Explicit effort-scaling rules. Plan saved to memory. Dedicated citation agent. LLM-as-judge rubric (factual accuracy, citation accuracy, completeness, source quality, tool efficiency). Checkpoints + resume; rainbow deploys. | ARCHITECTURE §6 roles and effort scaling (§6.3); doc 05 citation/claim checking; doc 10 judge rubric; doc 11 checkpoint/resume and staged rollout. |
| [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) | Loading every tool definition and routing intermediate results through the model is costly and error-prone (example: 150k → 2k tokens, 98.7 % reduction). Filter data in the execution environment; tokenise PII so it flows between tools without entering the model. | ADR 01 data plane: results land in the record store; the Analyzer runs deterministic queries; optional PII tokenisation in the gateway (doc 09 §6). |
| [Introducing advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use) | **Tool Search**: 58 tools ≈ 55k tokens; deferring reduced context ~85 % and raised MCP-eval accuracy (Opus 4.5: 79.5 % → 88.1 %). Use when definitions > ~10k tokens or 10+ tools. **Programmatic tool calling** −37 % tokens on complex tasks. **Tool use examples** raised complex-parameter accuracy 72 % → 90 %. | The *problem* (too many tool definitions) is solved by our own tool router with an 8k budget and `request_tools` (doc 06 §2); worked tool-input examples in descriptions (doc 06 §7). Server-side tool search and PTC are not used (A1). |
| [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | Each new session starts with no memory; use an initializer that writes a structured feature/task list (JSON preferred over Markdown because models overwrite it less), a progress log, and git checkpoints; one unit of work per session; verify end-to-end before marking done. | Task ledger as **structured JSON in Postgres** (`job_steps`), progress notes, resume-from-checkpoint, "only status/notes are mutable by the model" (doc 03 §2.3, doc 08). |
| [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps) | Planner / generator / evaluator split; "sprint contracts" agreed before work; out-of-the-box Claude is a lenient self-evaluator — a separately prompted, calibrated evaluator with hard thresholds works better. Every harness component encodes an assumption about model limits; re-test and remove components when models improve. | Doc 05 verifier is a separate agent with hard pass/fail thresholds and few-shot calibration; acceptance contract generated at plan time; doc 10 ablation testing of harness components. |
| [Scaling Managed Agents: decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents) | Split into **session** (append-only durable event log), **harness** (stateless loop, restartable via `wake(sessionId)`), **sandbox** (cattle, not pets). Credentials never reachable from the sandbox; a proxy injects them. Keep durable storage separate from context management since compaction is irreversible. | Append-only `session_events`; stateless workers resume from the log; credential vault + gateway injection (doc 09 §2); context is a rebuildable view and every request a replayable snapshot (doc 01, doc 02). |

## 2. Harness-engineering writing (non-Anthropic)

| Source | Finding | Design consequence |
|---|---|---|
| OpenAI — *Harness engineering: leveraging Codex in an agent-first world* (Feb 2026) | The environment (docs, linters, tests, feedback loops) determines agent reliability as much as the model. | Treat the harness (validators, contracts, eval) as the product; doc 10 makes harness components testable. |
| LangChain — [Improving Deep Agents with harness engineering](https://langchain.com/blog/improving-deep-agents-with-harness-engineering) | Same model, harness-only changes moved Terminal-Bench 2.0 from 52.8 % to 66.5 % (as reported). | Harness changes are versioned and evaluated like model changes (doc 10 §6). |
| Addy Osmani — [Agent Harness Engineering](https://addyosmani.com/blog/agent-harness-engineering/) | Scaffolding is an artifact that tightens every time the agent slips. | Every production failure becomes a regression eval case (doc 10 §5). |

> Note: the harness-engineering figures in §2 were taken from search snippets and secondary write-ups;
> they motivate process, not any numeric target in this design.

## 3. Protocol and vendor facts the design depends on

| Fact | Source | Consequence |
|---|---|---|
| MCP `tools/list` is cursor-paginated; servers that declare `tools.listChanged` emit `notifications/tools/list_changed`; results may carry `structuredContent` validated against `outputSchema`; execution errors return `isError: true`; **annotations are untrusted unless the server is trusted** | MCP spec 2025-11-25, *Server › Tools* | Doc 07: re-list on change, diff and re-review; never trust a new server's `readOnlyHint` — the gateway assigns risk itself. |
| The official MCP Registry (`registry.modelcontextprotocol.io`) publishes `server.json` metadata; its API froze at v0.1 (Oct 2025); host apps are expected to consume via downstream/sub-registries | MCP Registry docs | Doc 07: discovery reads the official registry **through an internal mirror/sub-registry** with an allow-list. |
| Jira Cloud legacy `/rest/api/3/search` is removed; enhanced `/rest/api/3/search/jql` uses `nextPageToken` and returns **no total**; a separate `/search/approximate-count` exists | Atlassian KB & community | Doc 04 completeness proof uses page-exhaustion as truth and approximate-count only as a sanity bound. |
| Atlassian Rovo MCP server (`https://mcp.atlassian.com/v2/mcp`) exposes `searchJiraIssuesUsingJql`; a public issue reports pagination tokens not returned | Atlassian docs; [atlassian-mcp-server#118](https://github.com/atlassian/atlassian-mcp-server/issues/118) | One reason all core servers are custom: our `jira_issues_search_to_resultset` paginates to exhaustion with a completeness proof (doc 06 §1). |
| Zendesk Search API returns ≤ 1,000 results (≤ 100/page); `/api/v2/search/export` is cursor-based, one object type per request, cursor expires ~1 h, separate rate limit | Zendesk developer docs (via search) | Zendesk connector switches to export search whenever `count` > 1,000 or always for bulk mode (doc 04 §2). |
| Zendesk had **no generally available first-party MCP server** as of Oct 2026 (announced May 2026) | Vendor comparison write-ups | Zendesk server is custom-built (doc 06 §6.2). |
| Microsoft's original SharePoint/OneDrive MCP servers deprecated from 13 Mar 2026; new Microsoft SharePoint MCP server is preview; file ops ≤ 5 MB | Microsoft Learn (via search) | SharePoint server is custom-built on Microsoft Graph (doc 06 §6.3). |
| Azure Databricks Genie managed MCP: `https://<workspace>/api/2.0/mcp/genie/{space_id}` (per-space) and a Genie One endpoint; Beta; Conversation API throughput is limited (best-effort, ~5 questions/min on the free tier; only POSTs count) | Azure Databricks docs (via search) | Custom Genie server over the Conversation API; low-QPS queue in the gateway (doc 06 §6.5). |
| Postgres MCP Pro offers a restricted (read-only transaction + time limit) mode | crystaldba/postgres-mcp | Same safety model in our custom server: read-only role, READ ONLY transactions, timeout (doc 06 §6.4). |

## 4. Claude API capabilities: used vs. replaced by custom code

The design uses the Claude API for inference only (ARCHITECTURE §0, A1).

| Capability | Decision | Custom replacement / note |
|---|---|---|
| Messages API, streaming, adaptive thinking, `output_config.effort` | **Used** | Core inference |
| Structured outputs (`output_config.format`), `strict` tools | **Used** | Fallback: schema-in-prompt + code validation + repair retry (doc 02 §9) |
| Prompt caching (`cache_control`) | **Used** | Pure cost/latency feature; requests work without it |
| Mid-conversation `system` messages | Used where supported | Fallback: tagged user text |
| Message Batches API | Optional (scheduled reports) | Inference only; synchronous fallback |
| `count_tokens` endpoint | Used sparingly | Calibrated local estimator is the fast path (doc 01 §9) |
| Server-side compaction | **Not used** | Epochs + structured handoff + custom compressor (doc 01 §4–6) |
| Context editing (tool-result clearing) | **Not used** | Gateway offloading before append; epoch reset (doc 01 §4) |
| Tool search / deferred loading | **Not used** | Custom tool router + `request_tools` (doc 06 §2) |
| Memory tool | **Not used** | Memory service with typed tools and provenance (doc 03) |
| Programmatic tool calling / code execution | **Not used** | Analyzer computes in SQL inside our boundary (doc 04 §7) |
| Agent SDK / hosted agents | **Not used** | Custom harness (ARCHITECTURE §6) |

Two model behaviours the harness is designed around: forced `tool_choice` (`any`/`tool`) is rejected
by the current Opus/Sonnet models, and thinking blocks are bound to an unchanged history (editing
earlier turns makes later requests fail on enforced accounts or drops the reasoning). Both shaped
ARCHITECTURE §6.4 and the epoch design.
